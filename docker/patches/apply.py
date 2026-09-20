#!/usr/bin/env python3
"""把集成改动落进 vendor/ 下的上游代码（构建期执行一次）。

===== 为什么需要本脚本 =====
项目的硬约束是「vendor/ 下的上游代码一字不改」（见 UPSTREAMS.md），
所以容器化集成所需的差异**全部在构建期施加**，vendor 快照本身与上游
逐字节一致 —— 只有这样 `scripts/sync-upstreams.sh` 才能是一条命令的事。

===== 四类改动（按侵入程度从轻到重）=====
能用轻的就不用重的。当前用量：文本补丁 3、JSON 键合并 1、文件新增 2、文件覆写 1。

  1. **文本补丁**：锚定上游某段代码原位替换，保留上游其余逻辑。
     都与「出口代理支持」和「注册套件路由」有关。
  2. **JSON 键合并**：给 i18n 词典**新增**一个文案命名空间，不碰上游已有键。
  3. **文件新增**：放入上游没有的新文件（自研的套件更新后端）。
  4. **文件覆写**：整文件替换上游文件。**代价是放弃该文件的上游后续改进**，
     每次同步上游都要人工复核。只有「要改的是整个渲染结构、打补丁等于
     把整段代码换掉」时才用它。

第 4 类只有一处（更新面板 UI）。详见 UPSTREAMS.md 的「覆写登记」。

===== fail-fast =====
每一处都先断言前提，否则**让构建失败**：
  * 文本补丁：锚点存在、只出现一次、且尚未打过；
  * 文件覆写：目标文件**已存在**（上游改名/删除要失败，而不是静默新建）、尚未覆写；
  * 文件新增：目标**不存在**（上游若自行加了同名文件要失败，提示我们复核）；
  * JSON 合并：语系集合与上游一致、我们自己的译文键集与占位符齐全、
    上游未占用同名键。

理由：上游一旦重构，静默跳过会让镜像"能构建、能启动"，但行为悄悄失真 ——
这类问题极难排查。构建失败是即时、可定位的信号。

⚠️ 构建失败时**不要直接删掉对应条目** —— 那等于静默放弃该能力。
先打开上游文件，对照本脚本里该条目的意图更新锚点，再重新构建。

改动清单与上游位置见 UPSTREAMS.md 的「构建期补丁登记」与「覆写登记」。
"""
from __future__ import annotations

import json
import re
import shutil
import sys
from pathlib import Path


class PatchError(RuntimeError):
    pass


# 我方文件的统一标记：CI 用它做「覆写/新增是否真的落位」的正向断言。
SUITE_MARKER = '【SUITE-OVERLAY】'


# ─────────────────────────────────────────────────────────────
# 文本补丁 1：manager —— 内网请求直连，不走 WB_HTTP_PROXY
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
# 文本补丁 2：wb2api —— Go transport 读代理环境变量
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


# ─────────────────────────────────────────────────────────────
# 文本补丁 3：manager —— 注册套件更新路由
# ─────────────────────────────────────────────────────────────
# 上游位置：vendor/manager/server/main.py
#
# 只加「导入 + 注册」两行，把我们在 overlay 里新增的 /api/system/suite-*
# 端点挂进 app。**不修改任何上游路由实现** —— 上游的 system.py / updater.py
# 一概不动（那是本项目的既定边界，CI 有反向断言守着）。
MAIN_IMPORT_OLD = """    responses, security as security_router, settings, stats, system,
)"""

MAIN_IMPORT_NEW = """    responses, security as security_router, settings, stats, system,
)
# 【集成补丁】本发行版新增的套件自更新路由（实现见 server/routers/suite.py）
from .routers import suite as suite_router"""

MAIN_ROUTER_OLD = 'app.include_router(system.router)'

MAIN_ROUTER_NEW = """app.include_router(system.router)
# 【集成补丁】套件自更新（/api/system/suite-*）
app.include_router(suite_router.router)"""


# ─────────────────────────────────────────────────────────────
# 文件覆写 / 文件新增
# ─────────────────────────────────────────────────────────────
# 元组：(overlay 内相对路径, vendor 根下相对路径, 说明)
#
# 覆写：目标**必须已存在**（上游改名或删除 → 构建失败，提示我们复核），
#       且覆写后必须含 SUITE_MARKER。
OVERLAYS: list[tuple[str, str, str]] = [
    (
        'web/components/common/settings/UpdatePanel.tsx',
        'manager/web/components/common/settings/UpdatePanel.tsx',
        '更新面板 UI 重构为「本套件 + 两个上游」三段式',
    ),
]

# 新增：目标**必须不存在**（上游若自行加了同名文件 → 构建失败，提示我们复核，
#       因为很可能是上游实现了同一件事，我们应该改用上游的）。
ADDITIONS: list[tuple[str, str, str]] = [
    (
        'server/services/suite.py',
        'manager/server/services/suite.py',
        '套件版本比对 / GitHub tag 查询 / 更新状态读取',
    ),
    (
        'server/routers/suite.py',
        'manager/server/routers/suite.py',
        '套件更新端点 /api/system/suite-*',
    ),
]

# i18n 文案：以「新增命名空间」的方式合并，绝不覆盖上游已有键。
I18N_SOURCE = 'i18n/suite-update.json'
I18N_LOCALES_DIR = 'manager/web/lib/i18n/locales'
_HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff]')
_PLACEHOLDER = re.compile(r'\{(\w+)\}')


def _validate(path: Path, old: str, new: str, label: str) -> None:
    """校验文本补丁锚点。任何异常都抛 PatchError（= 构建失败）。"""
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
            '  ⚠️ 不要直接删掉补丁 —— 那会让该能力静默失效。'
        )
    n = text.count(old)
    if n != 1:
        raise PatchError(f'{label}: 锚点出现 {n} 次（应为 1），无法安全替换（{path}）')


def _overlay_dir(explicit: str | None) -> Path:
    """定位 overlay 目录。

    默认取本脚本所在目录的上一级的 `overlay/`：
      * 仓库内运行  → docker/patches/apply.py → docker/overlay/
      * 镜像内运行  → /src/patches/apply.py   → /src/overlay/（Dockerfile 已 COPY）
    """
    d = Path(explicit) if explicit else Path(__file__).resolve().parent.parent / 'overlay'
    if not d.is_dir():
        raise PatchError(
            f'找不到 overlay 目录：{d}\n'
            '  Dockerfile 的 vpatch 阶段需要 COPY docker/overlay/ 到脚本同级，\n'
            '  或显式传入第二个参数指定路径。'
        )
    return d


def _plan_overlays(root: Path, overlay: Path) -> list[tuple[Path, Path, str]]:
    ops: list[tuple[Path, Path, str]] = []
    for rel_src, rel_dst, label in OVERLAYS:
        src = overlay / rel_src
        dst = root / rel_dst
        if not src.is_file():
            raise PatchError(f'{label}: overlay 源文件缺失 {src}')
        if not dst.is_file():
            raise PatchError(
                f'{label}: 上游文件不存在 {dst}。\n'
                '  上游可能改名或删除了这个文件。请复核本脚本 OVERLAYS 里的路径，\n'
                '  并确认上游的对应实现搬到了哪里（别静默新建 —— 那会留下两份实现）。'
            )
        if SUITE_MARKER in dst.read_text(encoding='utf-8'):
            raise PatchError(
                f'{label}: {dst} 已经是我们覆写过的内容了（含 {SUITE_MARKER}）。\n'
                '  说明 vendor/ 目录不是干净的上游快照。请重新执行\n'
                '  scripts/sync-upstreams.sh 还原快照后再构建。'
            )
        ops.append((src, dst, label))
    return ops


def _plan_additions(root: Path, overlay: Path) -> list[tuple[Path, Path, str]]:
    ops: list[tuple[Path, Path, str]] = []
    for rel_src, rel_dst, label in ADDITIONS:
        src = overlay / rel_src
        dst = root / rel_dst
        if not src.is_file():
            raise PatchError(f'{label}: overlay 源文件缺失 {src}')
        if dst.exists():
            raise PatchError(
                f'{label}: 目标已存在 {dst}，拒绝覆盖。\n'
                '  上游很可能自己实现了同一件事。请打开该文件复核：\n'
                '  能用上游的就删掉我们的 ADDITIONS 条目，不要留两份。'
            )
        ops.append((src, dst, label))
    return ops


def _plan_i18n(root: Path, overlay: Path) -> list[tuple[Path, dict, str, dict]]:
    """校验 i18n 合并，返回 [(locale 文件, 解析后的文档, 命名空间, 该语系新增键)]."""
    src = overlay / I18N_SOURCE
    if not src.is_file():
        raise PatchError(f'i18n: 找不到文案源文件 {src}')
    data = json.loads(src.read_text(encoding='utf-8'))
    ns = data.get('namespace')
    locales = data.get('locales')
    if not isinstance(ns, str) or not ns:
        raise PatchError(f'i18n: {src} 缺少 namespace')
    if not isinstance(locales, dict) or not locales:
        raise PatchError(f'i18n: {src} 缺少 locales')
    if 'zh-CN' not in locales:
        raise PatchError('i18n: locales 里必须有 zh-CN（它是上游的源语言与回退语言）')

    loc_dir = root / I18N_LOCALES_DIR
    if not loc_dir.is_dir():
        raise PatchError(f'i18n: 找不到上游语系目录 {loc_dir}')
    on_disk = {p.stem for p in loc_dir.glob('*.json')}
    if on_disk != set(locales):
        raise PatchError(
            'i18n: 语系集合与上游不一致 —— 上游新增/删除了语言。\n'
            f'  磁盘：{sorted(on_disk)}\n'
            f'  我们的翻译：{sorted(locales)}\n'
            f'  请补齐 {src} 里缺失语系的译文后重新构建。'
        )

    base_keys = set(locales['zh-CN'])
    base_ph = {k: set(_PLACEHOLDER.findall(v)) for k, v in locales['zh-CN'].items()}

    for loc, kvs in locales.items():
        if not isinstance(kvs, dict):
            raise PatchError(f'i18n: locales.{loc} 必须是对象')
        if set(kvs) != base_keys:
            missing = sorted(base_keys - set(kvs))
            extra = sorted(set(kvs) - base_keys)
            raise PatchError(
                f'i18n: {loc} 的键集与 zh-CN 不一致（上游 i18n 测试会因此失败）\n'
                f'  缺少：{missing}\n  多余：{extra}'
            )
        for k, v in kvs.items():
            if not isinstance(v, str) or not v.strip():
                raise PatchError(f'i18n: {loc}.{k} 为空')
            # 占位符必须逐语系一致，否则渲染时会把 {xxx} 原样漏给用户
            got = sorted(_PLACEHOLDER.findall(v))
            if got != sorted(base_ph[k]):
                raise PatchError(
                    f'i18n: {loc}.{k} 的占位符与 zh-CN 不一致\n'
                    f'  zh-CN={sorted(base_ph[k])} 本语系={got}'
                )
        if loc == 'en' and _HAN.search(json.dumps(kvs, ensure_ascii=False)):
            bad = [k for k, v in kvs.items() if _HAN.search(v)]
            raise PatchError(f'i18n: en 里不允许出现中日韩字符：{bad}')

    ops: list[tuple[Path, dict, str, dict]] = []
    for loc, kvs in locales.items():
        p = loc_dir / f'{loc}.json'
        doc = json.loads(p.read_text(encoding='utf-8'))
        if ns in doc:
            existing = doc[ns]
            if not isinstance(existing, dict):
                raise PatchError(f'i18n: {p} 里 {ns} 已存在且不是对象，无法合并')
            clash = sorted(set(existing) & set(kvs))
            if clash:
                raise PatchError(
                    f'i18n: 上游已在 {loc} 里自行定义了 {ns} 下的键 {clash}。\n'
                    '  请复核上游文案：若语义相同就用上游的（删掉我们的条目），\n'
                    '  若不同则改名我们的键。**不要静默覆盖上游文案。**'
                )
        ops.append((p, doc, ns, kvs))
    return ops


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '/src/vendor')
    overlay = _overlay_dir(sys.argv[2] if len(sys.argv) > 2 else None)

    manager_cfg = root / 'manager' / 'server' / 'config.py'
    go_transport = root / 'wb2api' / 'internal' / 'upstream' / 'transport.go'
    manager_main = root / 'manager' / 'server' / 'main.py'

    text_edits: list[tuple[Path, str, str, str]] = [
        (manager_cfg, MANAGER_ANCHOR, MANAGER_HELPER + MANAGER_ANCHOR,
         'manager: 注入 _internal_direct_mounts()'),
        (manager_cfg, MANAGER_OLD, MANAGER_NEW,
         'manager: 内网请求直连（mounts）'),
        (go_transport, GO_OLD, GO_NEW,
         'wb2api: Transport 读代理环境变量'),
        (manager_main, MAIN_IMPORT_OLD, MAIN_IMPORT_NEW,
         'manager: 导入套件路由'),
        (manager_main, MAIN_ROUTER_OLD, MAIN_ROUTER_NEW,
         'manager: 注册套件路由'),
    ]

    print(f'[patches] vendor 根 {root}')
    print(f'[patches] overlay  {overlay}')

    # ── 阶段 1：全部校验（全或无，避免"改了一半"的中间状态）──
    for path, old, new, label in text_edits:
        _validate(path, old, new, label)
    overlay_ops = _plan_overlays(root, overlay)
    addition_ops = _plan_additions(root, overlay)
    i18n_ops = _plan_i18n(root, overlay)

    # ── 阶段 2：统一落盘 ──
    for path, old, new, label in text_edits:
        text = path.read_text(encoding='utf-8')
        path.write_text(text.replace(old, new, 1), encoding='utf-8')
        print(f'  [patch] {label}')

    for src, dst, label in overlay_ops:
        shutil.copyfile(src, dst)
        body = dst.read_text(encoding='utf-8')
        if SUITE_MARKER not in body:
            raise PatchError(
                f'{label}: 覆写后的 {dst} 不含标记 {SUITE_MARKER}。\n'
                f'  请确认 overlay 源文件 {src} 头部保留了该标记行。'
            )
        print(f'  [overlay] {label} → {dst.relative_to(root)}')

    for src, dst, label in addition_ops:
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        body = dst.read_text(encoding='utf-8')
        if SUITE_MARKER not in body:
            raise PatchError(f'{label}: 新增的 {dst} 不含标记 {SUITE_MARKER}')
        print(f'  [add] {label} → {dst.relative_to(root)}')

    for path, doc, ns, kvs in i18n_ops:
        merged = dict(doc.get(ns) or {})
        merged.update(kvs)
        doc[ns] = merged
        path.write_text(
            json.dumps(doc, ensure_ascii=False, indent=2) + '\n', encoding='utf-8',
        )
        print(f'  [i18n] {path.stem}: +{len(kvs)} 键到 {ns}')

    total = len(text_edits) + len(overlay_ops) + len(addition_ops) + len(i18n_ops)
    print(
        f'[patches] 完成（文本补丁 {len(text_edits)}、'
        f'覆写 {len(overlay_ops)}、新增 {len(addition_ops)}、i18n {len(i18n_ops)} 个语系，'
        f'共 {total} 处）'
    )
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except PatchError as exc:
        print(f'\n[patches][FAIL] {exc}\n', file=sys.stderr)
        raise SystemExit(1) from exc
