#!/usr/bin/env python3
"""给 vendor/ 下的上游代码打集成补丁（构建期执行一次）。

===== 只保留两处，其余交给上游 =====
本项目的硬约束是「vendor/ 下的上游代码一字不改」（见 UPSTREAMS.md），
因此容器化集成所需的行为差异只能靠构建期补丁。

**本脚本刻意保持最小**：只打两处上游确实没有、且我们实测确认必须的能力。
其余集成需求一律**采用上游官方行为**，不自行改写 —— 理由见下。

保留的两处（都与「出口代理」有关，上游完全没有这个概念）：

  1. **manager**：配了 WB_HTTP_PROXY 后，它连**自己上游** wb2api:7863 的
     请求也会走代理 —— 内网服务绕一圈代理，必然失败。
  2. **wb2api**：Go 的 http.Transport 不读 HTTP_PROXY/HTTPS_PROXY，
     网关出站无法走代理。

===== 为什么不再改界面文案（曾经打过、现已删除）=====
早期版本还打了三处补丁去修正「更新」面板的文案与按钮，理由是本发行版把
部署改成了容器、更新在宿主机执行。**这些补丁后来全部删除了**，因为：

  * 上游 v1.0.32+ 引入了 **`can_update_upstream` 能力驱动**判定：
    它用 `docker info` 能否跑通判断"能不能操作 docker"，比按"是否在容器里"
    判断更准确。本发行版因 socket 代理关闭 `/info` 端点而表现为"不可用"，
    于是界面会**自动禁用**更新按钮并给出替代做法 —— 这正是我们想要的。
  * 上游同时完成了 i18n 重构，文案移到 `web/lib/i18n/locales/*.json`。
    继续锚定硬编码文案意味着**每次上游改字都要跟着改补丁**，
    而收益仅仅是措辞更贴切。

  教训：不要为"文案更准确"维护构建期补丁。上游一旦重构就会失效，
  而失效本身（构建失败）比文案不准更烦人。

===== 设计要点：fail-fast =====
每处补丁都先断言「锚点文本存在且只出现一次」，否则**让构建失败**，
而不是静默跳过。理由：上游一旦重构这两处，静默跳过会让镜像
"能构建、能启动"，但代理行为悄悄失效 —— 这类问题极难排查。
构建失败则是即时、可定位的信号，提示维护者来复核本文件。

补丁清单与上游位置见 UPSTREAMS.md 的「构建期补丁登记」。
"""
from __future__ import annotations

import sys
from pathlib import Path


class PatchError(RuntimeError):
    pass


# ─────────────────────────────────────────────────────────────
# 补丁 1：manager —— 内网请求直连，不走 WB_HTTP_PROXY
# ─────────────────────────────────────────────────────────────
# 上游位置：vendor/manager/server/config.py 的 http_client()
#
# 上游把 WB_HTTP_PROXY 直接当作 httpx 的 proxy= 传入，而该参数
# **作用于所有请求**。官方注释只提醒「宿主机有 Clash TUN 时建议留空」，
# 没覆盖「确实需要代理」的场景：此时连 wb2api:7863 都会被送去代理。
#
# 实测（httpx 0.28 + httpcore 1.0.2）：
#   * 显式传 proxy= 时，no_proxy 环境变量**完全失效**（trust_env 与否都不行）；
#   * 只有 mounts 能按 URL 前缀精确指定直连。
#     → 外网 pool=AsyncSOCKSProxy / 内网 pool=AsyncConnectionPool
MANAGER_HELPER = '''def _internal_direct_mounts() -> dict:
    """内部服务的直连 transport（绕过 WB_HTTP_PROXY）。

    【本发行版集成补丁，见 docker/patches/apply.py】

    为什么必须有：httpx 的 proxy= 参数作用于**所有**请求。配了 SOCKS 代理后，
    连同一 compose 网络内的上游 wb2api:7863 也会被送去代理，结果是管理端
    连不上自己的上游。no_proxy 环境变量在显式 proxy= 下不生效（已实测），
    只能靠 mounts 按 URL 前缀指定直连。
    """
    import httpx
    from urllib.parse import urlsplit

    mounts: dict = {}

    def _add(base: str) -> None:
        parts = urlsplit(base)
        if not parts.hostname:
            return
        port = f':{parts.port}' if parts.port else ''
        mounts[f'{parts.scheme}://{parts.hostname}{port}'] = httpx.AsyncHTTPTransport()

    _add(WB2API_BASE)                # 上游网关（由配置推导，换地址自动跟上）
    _add('http://dockerproxy:2375')  # socket 代理（实际经 docker CLI，防御性添加）
    return mounts


'''

MANAGER_ANCHOR = 'def http_client(timeout, *, connect: float | None = None):'

MANAGER_OLD = """    kwargs = {'timeout': tmo, 'trust_env': False}
    if HTTP_PROXY:
        kwargs['proxy'] = HTTP_PROXY
    return httpx.AsyncClient(**kwargs)"""

MANAGER_NEW = """    kwargs = {'timeout': tmo, 'trust_env': False}
    if HTTP_PROXY:
        kwargs['proxy'] = HTTP_PROXY
        # 【集成补丁】内网服务直连，不经过 WB_HTTP_PROXY。
        # 否则配了 SOCKS 代理后，连 wb2api:7863 也会被送去代理而失败。
        kwargs['mounts'] = _internal_direct_mounts()
    return httpx.AsyncClient(**kwargs)"""


# ─────────────────────────────────────────────────────────────
# 补丁 2：wb2api —— Go transport 读代理环境变量
# ─────────────────────────────────────────────────────────────
# 上游位置：vendor/wb2api/internal/upstream/transport.go 的 newTransport()
#
# 上游的自定义 http.Transport 未设 Proxy 字段，Go 零值 = 恒不使用代理，
# 也不去读 HTTP_PROXY/HTTPS_PROXY。于是网关出站恒为直连。
#
# 注：上游的 cmd/login 等 CLI 用的是 http.DefaultTransport（其 Proxy 默认
# 就是 ProxyFromEnvironment），所以那些工具**本来就支持代理**，
# 只有 internal/upstream 这个自定义 transport 不支持。
#
# ProxyFromEnvironment 原生支持 http/https/socks5（含 socks5h，已实测），
# 并自动遵循 NO_PROXY；**不认 ALL_PROXY**（这是 Go 标准库的行为）。
# 未设置代理变量时返回 nil → 行为与打补丁前完全一致。
#
# 关于 DialContext 与 Proxy 共存（上游新写法同时设了自定义 DialContext）：
# 已实测确认**两者可共存** —— 设了 Proxy 后，连接代理本身仍走 DialContext，
# 请求确实经代理发出（用假代理收包验证过），代理逻辑不会被 dialer 绕过。
# 因此这里只加一个 Proxy 字段即可，无需包装 dialer。
GO_OLD = """func newTransport() *http.Transport {
	dialer := newDialer()
	return &http.Transport{
		DialContext: dialer.DialContext,"""

GO_NEW = """func newTransport() *http.Transport {
	dialer := newDialer()
	return &http.Transport{
		DialContext: dialer.DialContext,
		// 【集成补丁】读 HTTP_PROXY / HTTPS_PROXY 与 NO_PROXY，使网关出站
		// 可走代理（http/https/socks5 均支持；注意 Go 标准库不认 ALL_PROXY）。
		// 上游未设该字段，Go 零值 = 恒不使用代理。
		// 与上面的自定义 DialContext 可共存（已实测）。
		// 见 docker/patches/apply.py。
		Proxy: http.ProxyFromEnvironment,"""


def _validate(path: Path, old: str, new: str, label: str) -> str:
    """校验锚点，返回文件原文。任何异常都抛 PatchError（= 构建失败）。"""
    if not path.is_file():
        raise PatchError(f'{label}: 找不到文件 {path}')
    text = path.read_text(encoding='utf-8')

    if new in text and old not in text:
        raise PatchError(
            f'{label}: 目标文件似乎已打过补丁（{path}）。\n'
            '  可能是上游已自行实现了该能力，或本脚本被重复执行。\n'
            '  请复核 docker/patches/apply.py；确认不再需要后删除对应补丁。'
        )
    if old not in text:
        raise PatchError(
            f'{label}: 锚点文本在上游源码里找不到（{path}）。\n'
            '  几乎可以确定是上游重构了这段代码。请打开该文件，对照本脚本\n'
            '  对应补丁的意图更新锚点，再重新构建。\n'
            '  ⚠️ 不要直接删掉补丁 —— 那会让代理行为静默失效。'
        )
    n = text.count(old)
    if n != 1:
        raise PatchError(f'{label}: 锚点出现 {n} 次（应为 1），无法安全替换（{path}）')
    return text


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '/src/vendor')

    manager_cfg = root / 'manager' / 'server' / 'config.py'
    go_transport = root / 'wb2api' / 'internal' / 'upstream' / 'transport.go'

    edits: list[tuple[Path, str, str, str]] = [
        (manager_cfg, MANAGER_ANCHOR, MANAGER_HELPER + MANAGER_ANCHOR,
         'manager: 注入 _internal_direct_mounts()'),
        (manager_cfg, MANAGER_OLD, MANAGER_NEW,
         'manager: 内网请求直连（mounts）'),
        (go_transport, GO_OLD, GO_NEW,
         'wb2api: Transport 读代理环境变量'),
    ]

    print(f'[patches] 目标 {root}')

    # 先把全部补丁校验一遍（全或无），避免"打了一半"的中间状态
    for path, old, new, label in edits:
        _validate(path, old, new, label)

    # 校验全部通过后再落盘
    for path, old, new, label in edits:
        text = path.read_text(encoding='utf-8')
        path.write_text(text.replace(old, new, 1), encoding='utf-8')
        print(f'  [patch] {label}')

    print(f'[patches] 完成（{len(edits)} 处）')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f'\n[patches][FAIL] {exc}\n', file=sys.stderr)
        raise SystemExit(1) from exc
