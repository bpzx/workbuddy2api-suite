#!/usr/bin/env python3
"""给 vendor/ 下的上游代码打集成补丁（构建期执行一次）。

===== 为什么需要这个文件 =====
本项目的硬约束是「vendor/ 下的上游代码一字不改」（见 UPSTREAMS.md），
这样同步上游才是一条命令的事。但容器化集成确实需要三处上游没有的行为：

  1. **manager**：配了 WB_HTTP_PROXY（含 SOCKS）后，它连**自己上游**
     wb2api:7863 的请求也会走代理 —— 内网服务绕一圈代理，必然失败。
  2. **wb2api**：Go 的 http.Transport 没设 Proxy 字段，零值意味着
     **完全不读** HTTP_PROXY/ALL_PROXY，网关出站无法走代理。
  3. **manager 前端**：「更新」面板的文案描述的是裸机部署（git + systemd）。
     但本项目在构建期已把 deploy/update.py 换成替身（不执行实际更新），
     文案若不改，用户会以为点按钮就能更新 —— 实际什么都发生不了。

折中：**源码保持原样，构建期打补丁**。本文件在 Dockerfile 的 `vpatch`
阶段执行，产物供后续所有阶段使用。

===== 设计要点：fail-fast =====
每个补丁都先断言「锚点文本存在且只出现一次」，否则**让构建失败**，
而不是静默跳过。理由：上游一旦重构这段代码，静默跳过会让镜像
"能构建、能启动"，但补丁意图悄悄失效——这类问题极难排查。
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
# 上游位置：vendor/manager/server/config.py
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

    【本发行版新增的集成补丁，见 docker/patches/apply.py】

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
# 上游位置：vendor/wb2api/internal/upstream/client.go 的 New()
#
# 自定义 http.Transport 未设 Proxy 字段，Go 零值 = 恒不使用代理，
# 也不去读 HTTP_PROXY/HTTPS_PROXY/ALL_PROXY。于是网关出站恒为直连。
#
# 注：上游的 cmd/login 等 CLI 用的是 http.DefaultTransport（其 Proxy 默认
# 就是 ProxyFromEnvironment），所以那些工具**本来就支持代理**，
# 只有 internal/upstream 这个自定义 transport 不支持。
#
# ProxyFromEnvironment 原生支持 http/https/socks5（含 socks5h，已实测），
# 并自动遵循 NO_PROXY。未设置代理变量时返回 nil → 行为与打补丁前完全一致。
GO_OLD = """	tr := &http.Transport{
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,"""

GO_NEW = """	tr := &http.Transport{
		// 【集成补丁】读 HTTP_PROXY / HTTPS_PROXY / ALL_PROXY 与 NO_PROXY，
		// 使网关出站可走代理（http/https/socks5 均支持）。
		// 上游未设该字段，Go 零值 = 恒不使用代理。
		// 见 docker/patches/apply.py。
		Proxy:               http.ProxyFromEnvironment,
		MaxIdleConns:        100,
		MaxIdleConnsPerHost: 20,
		IdleConnTimeout:     90 * time.Second,"""


# ─────────────────────────────────────────────────────────────
# 补丁 3：manager 前端 —— 「更新」文案如实反映容器部署
# ─────────────────────────────────────────────────────────────
# 上游位置：vendor/manager/web/components/common/settings/UpdatePanel.tsx
#
# 上游文案描述的是**裸机部署**（systemd + git + Release 包）。本项目
# 在构建期把 deploy/update.py 换成了替身（不执行实际更新），但界面文案
# 仍是上游的，于是给用户一个假预期：「点下方按钮即可更新」。
#
# 实测过用户视角的后果：面板提示「管理端 v1.0.28 → v1.0.31，点下方按钮
# 即可更新」，点了却什么都不发生——因为替身脚本只写一条提示、不改代码。
#
# 这里把文案改成容器部署的真实操作（docker compose pull && up -d），
# 避免"点了没反应"的困惑。改的是文案，按钮与接口都保留（点击后会在
# 日志里看到替身打印的正确命令，也算一个指引入口）。
UI_FILE = 'manager/web/components/common/settings/UpdatePanel.tsx'


def patch_manager_ui(root: Path) -> None:
    """把「一键更新」相关文案改为容器部署的真实情况。"""
    target = root / UI_FILE
    if not target.is_file():
        raise PatchError(f'补丁 3：找不到 {target}')

    text = target.read_text(encoding='utf-8')

    replacements: list[tuple[str, str, str]] = [
        # 1) 顶部提示行：点按钮不会更新，如实说明
        (
            "                  点下方按钮即可更新，账号与配置会自动保留。",
            "                  本部署由 Docker Compose 管理：按钮只会提示更新命令，\n"
            "                  不会在容器内改动代码。请在宿主机执行\n"
            "                  <code className=\"font-mono\">docker compose pull &amp;&amp; docker compose up -d</code>。\n"
            "                  账号与配置保存在数据目录，不受影响。",
            '提示行改为容器说明',
        ),
        # 2) 面板标题与说明
        (
            '        <div className="mb-1 text-sm font-medium">一键更新</div>\n'
            '        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">\n'
            '          从 GitHub 拉取最新代码并自动重建/重启。\n'
            '          账号授权文件、上游配置、密钥与日志数据都会保留。\n'
            '        </div>',
            '        <div className="mb-1 text-sm font-medium">更新</div>\n'
            '        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">\n'
            '          容器部署下更新在<strong>宿主机</strong>执行（容器内无 git、无 systemd，\n'
            '          镜像层只读）。本面板的按钮不会改动代码，只会在下方日志里打印\n'
            '          正确的更新命令，便于复制：\n'
            '          <code className="mt-1 block font-mono">docker compose pull &amp;&amp; docker compose up -d</code>\n'
            '          账号授权文件、上游配置、密钥与日志数据保存在数据目录，不受影响。\n'
            '        </div>',
            '面板标题与说明改为容器说明',
        ),
        # 3) 三个按钮的 hint（描述的是裸机流程）
        (
            "    hint: '上游会重建容器（保留账号与配置），管理端会重启服务',",
            "    hint: '容器部署：请在宿主机执行 docker compose pull && docker compose up -d',",
            'TARGETS hint 1',
        ),
        (
            "    hint: '拉取上游代码 → 重建容器。端口绑定会重新收敛为仅本机',",
            "    hint: '容器部署：由宿主机的 compose pull 统一更新，不在此处拉取代码',",
            'TARGETS hint 2',
        ),
        (
            "    hint: '下载最新 Release → 替换代码与前端 → 重启服务',",
            "    hint: '容器部署：由宿主机的 compose pull 统一更新，不在此处替换代码',",
            'TARGETS hint 3',
        ),
        # 4) 确认弹窗文案
        (
            'description={`${t.desc}。${t.hint}。更新过程中服务会短暂中断，已完成的任务不受影响。`}',
            'description={`${t.desc}。${t.hint}。`}',
            '确认弹窗去掉"服务会中断"',
        ),
        # 5) 「固定上游版本」整块在容器内不可用（无 git）
        (
            '        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">\n'
            '          默认跟随上游 master。若上游某个提交自身有问题（例如 Dockerfile 引用了\n'
            '          已删除的文件导致重建失败），在这里填上一个可用的<strong>提交号或标签</strong>，\n'
            '          之后「更新上游」就会检出该版本而不是最新代码，用来快速回退。\n'
            '          留空并保存即恢复跟随分支。\n'
            '        </div>',
            '        <div className="mb-3 text-[11px] leading-4 text-muted-foreground">\n'
            '          <strong>容器部署下此功能不可用</strong>：容器内没有上游 git 仓库，无法检出\n'
            '          指定版本。要回退或固定上游版本，请在宿主机本项目目录执行\n'
            '          <code className="font-mono">./scripts/sync-upstreams.sh --ref &lt;commit&gt;</code>\n'
            '          后重新构建镜像。下方输入框仅作记录，不影响实际部署。\n'
            '        </div>',
            '固定上游版本改为容器说明',
        ),
    ]

    for old, new, label in replacements:
        if new in text and old not in text:
            print(f'  [patch] {label}：已应用过，跳过')
            continue
        cnt = text.count(old)
        if cnt != 1:
            raise PatchError(
                f'补丁 3（{label}）：锚点命中 {cnt} 次（应为 1），无法安全替换。\n'
                f'  文件：{target}\n'
                '  上游可能改动了前端文案。请打开该文件，对照 docker/patches/apply.py\n'
                '  里本补丁的意图更新锚点文本，再重新构建。'
            )
        text = text.replace(old, new, 1)
        print(f'  [patch] {label}')

    target.write_text(text, encoding='utf-8')


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
    go_client = root / 'wb2api' / 'internal' / 'upstream' / 'client.go'

    # (path, old, new, label)
    edits: list[tuple[Path, str, str, str]] = [
        (manager_cfg, MANAGER_ANCHOR, MANAGER_HELPER + MANAGER_ANCHOR,
         'manager: 注入 _internal_direct_mounts()'),
        (manager_cfg, MANAGER_OLD, MANAGER_NEW,
         'manager: 内网请求直连（mounts）'),
        (go_client, GO_OLD, GO_NEW,
         'wb2api: Transport 读代理环境变量'),
    ]

    print(f'[patches] 目标 {root}')

    # 先把全部补丁校验一遍（全或无），避免"打了一半"的中间状态
    for path, old, new, label in edits:
        _validate(path, old, new, label)

    # 校验全部通过后再落盘。同一文件的多处补丁按顺序应用：
    # 两处锚点位于文件不同位置，前一处替换不影响后一处锚点存在性。
    for path, old, new, label in edits:
        text = path.read_text(encoding='utf-8')
        path.write_text(text.replace(old, new, 1), encoding='utf-8')
        print(f'  [patch] {label}')

    # 前端文案补丁自带上游重构检测，单独处理
    patch_manager_ui(root)

    print('[patches] 完成')
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f'\n[patches][FAIL] {exc}\n', file=sys.stderr)
        raise SystemExit(1) from exc
