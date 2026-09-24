#!/usr/bin/env python3
"""把集成改动落进 vendor/ 下的上游代码（构建期执行一次）。

===== 为什么需要本脚本 =====
项目的硬约束是「vendor/ 下的上游代码一字不改」（见 UPSTREAMS.md），
所以容器化集成所需的差异**全部在构建期施加**，vendor 快照本身与上游
逐字节一致 —— 只有这样 `scripts/sync-upstreams.sh` 才能是一条命令的事。

===== 四类改动（按侵入程度从轻到重）=====
能用的就用轻的。当前用量：文本补丁 4 组（6 处替换）、JSON 键合并 1、文件新增 2、文件覆写 1。

  1. **文本补丁**：锚定上游某段代码原位替换，保留上游其余逻辑。
     4 组分别是：出口代理支持（2 组）、注册套件路由（1 组）、
     移除不该出现的会话弹窗（1 组）。
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
# 文本补丁 4：manager —— 去掉顶部的"发现新版本"会话弹窗
# ─────────────────────────────────────────────────────────────
# 上游位置：vendor/manager/web/components/common/layout/ManagementBar.tsx
#
# 上游每次会话检测一次新版本，有更新就弹一个 toast。本发行版必须去掉它，因为
# 那条提示**两处都不成立**：
#   1. 它显示的版本号是**上游 workbuddy-manager** 的版本（读的是上游
#      /api/system/check-update），不是本套件的版本 —— 用户会误以为
#      "管理端 v1.0.60" 说的是自己这个项目。这个误读已经真实发生过。
#   2. 它的文案是「到设置 → 系统更新 一键升级」，但本发行版的「系统更新」页
#      **不提供升级上游的按钮**（上游更新必须走宿主机 sync-upstreams.sh +
#      重建镜像），所以它指向的操作根本不存在。
# 版本信息统一看「系统更新」页：本套件与两个上游分三段列出，各自标明能否更新。
#
# 注：这属于"去掉不该出现的行为"，不是"改措辞"——后者是本项目明确不再做的事
# （见文件头「曾经打过、现已删除的补丁」）。
MANAGEMENT_BAR_OLD = """  // 每个浏览器会话检测一次新版本，有更新则弹出提醒（避免打扰不重复提示）
  useEffect(() => {
    if (!mounted || typeof window === 'undefined') return;
    const KEY = 'workbuddy-manager:update-notified';
    if (window.sessionStorage.getItem(KEY) === '1') return;
    window.sessionStorage.setItem(KEY, '1');

    (async () => {
      try {
        const c = await systemApi.checkUpdate();
        if (!c.has_any) return;
        const parts: string[] = [];
        if (c.manager.has_update) parts.push(t('update.managerVersion', {v: c.manager.latest}));
        if (c.upstream.has_update) parts.push(t('update.upstreamVersion', {v: c.upstream.latest}));
        notify.warn(t('update.newVersion'), t('update.notice', {targets: parts.join(' · ')}));
      } catch {
        /* 检测失败静默：不打扰用户（如服务器访问 GitHub 受限） */
      }
    })();
  }, [mounted, t]);"""

MANAGEMENT_BAR_NEW = """  // 【集成补丁】本发行版**移除了**上游的"发现新版本"会话弹窗。
  //
  // 为什么移除（那条提示在这里两处都不成立）：
  //   1. 它显示的版本号是**上游 workbuddy-manager** 的版本，不是本套件的版本
  //      —— 会让人误以为"管理端 vX"说的是本项目。
  //   2. 它的文案指向「设置 → 系统更新 一键升级」，但本发行的该页并不提供
  //      升级上游的按钮（上游更新必须走宿主机同步 + 重建镜像），
  //      也就是说它提示的那个操作不存在。
  //
  // 版本信息统一看「系统更新」页：本套件与两个上游分三段列出，各自标明
  // 能否更新，且不弹窗打扰。见 docker/patches/apply.py。"""


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


# ─────────────────────────────────────────────────────────────
# 补丁 5：wb2api —— 设备标识派生盐可轮换
# ─────────────────────────────────────────────────────────────
# 上游位置：vendor/wb2api/internal/upstream/headers.go 的 deriveAccountStableID()
#
# 背景：`X-Machine-ID` / `X-Session-ID` = sha256("<盐>" + purpose + ":" + uid)[:36]，
# 盐是硬编码常量 `"wb2a:"`。按 uid 稳定派生是**有意设计**（对标官方桌面端
# "每账号一台固定虚拟设备"，缺了反而会被按设备指纹异常关联风控）。
# 但它同时意味着：一旦某账号的设备标识被上游打标，**重新登录也换不掉**
# （uid 不变 → 标识不变），上游没有提供任何轮换手段。
#
# 本补丁把盐变成可用环境变量 `WB2A_DEVICE_ID_SALT` 覆盖：
#   * 默认仍取 `"wb2a:"` → **行为与打补丁前完全一致**（有测试断言该派生值，
#     默认不变才安全）；
#   * 设了才换 → 提供一个"确有需要时"的后手。
#
# ⚠️ 换盐会让**所有账号**的设备标识同时变化（在平台看是"全体换设备"），
# 只在确有必要时使用。见 README「设备标识与轮换」。
GO_IMPORT_OLD = """import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"strings\""""

GO_IMPORT_NEW = """import (
	"crypto/sha256"
	"encoding/hex"
	"net/http"
	"os"
	"strings\""""

GO_SALT_OLD = """func deriveAccountStableID(uid, purpose string) string {
	sum := sha256.Sum256([]byte("wb2a:" + purpose + ":" + uid))
	return hex.EncodeToString(sum[:18]) // 36 hex chars
}"""

GO_SALT_NEW = """// deviceIDSalt 设备/会话标识的派生盐。
//
// 【本发行版集成补丁】默认沿用历史常量 "wb2a:"（行为不变）；可用环境变量
// WB2A_DEVICE_ID_SALT 覆盖，以便在设备标识被上游打标时轮换
// （见 README「设备标识与轮换」）。上游已删除，该变量属运维级参数、不进设置页。
func deviceIDSalt() string {
	if v := strings.TrimSpace(os.Getenv("WB2A_DEVICE_ID_SALT")); v != "" {
		return v
	}
	return "wb2a:"
}

func deriveAccountStableID(uid, purpose string) string {
	sum := sha256.Sum256([]byte(deviceIDSalt() + purpose + ":" + uid))
	return hex.EncodeToString(sum[:18]) // 36 hex chars
}"""


# ─────────────────────────────────────────────────────────────
# 补丁 6：wb2api —— max_in_flight=0（不限流）时启动告警
# ─────────────────────────────────────────────────────────────
# 上游位置：cmd/server/main.go 的池初始化
#
# `pool.max_in_flight` 的 0 表示**不限**单账号在途数（见 internal/pool/pool.go 的
# Acquire/inFlightLimit）。这个值只能写在 config.json 里，误设不会有任何报错，
# 而"单账号并发无上限"会显著提高账号被风控的概率 —— 因此显式告警。
GO_MAXINFLIGHT_OLD = '	p.SetMaxInFlight(cfg.Pool.MaxInFlight)'

GO_MAXINFLIGHT_NEW = """	// 【本发行版集成补丁】0 = 单账号在途**不限**；误设无报错但会显著提高
	// 账号风控风险，故显式告警。见 README「风控相关配置」。
	if cfg.Pool.MaxInFlight == 0 {
		log.Printf("WARN: [pool] max_in_flight=0 表示单账号在途不限；并发无上限会显著提高账号风控风险，建议保持 3 左右")
	}
	p.SetMaxInFlight(cfg.Pool.MaxInFlight)"""


# ─────────────────────────────────────────────────────────────
# 补丁 7：wb2api —— 合成任务的固定间隔加抖动
# ─────────────────────────────────────────────────────────────
# 上游位置：internal/scheduler/travel.go（常量所在处）与 scheduler.go（调用点）
#
# 上报间隔是硬编码的 0.8s / 1.5s —— 精确等距的节奏是"机器特征"，真人操作不会
# 这么匀。加 0..+50% 抖动后，时间密度不再落在固定周期上。
#
# 注意 `base <= 0` 必须原样返回：测试用「把基准置 0」来跳过等待
# （internal/scheduler/activity_test.go、parallel_test.go），抖动也得跟着跳过，
# 否则会拖慢甚至拖挂测试。
GO_JITTER_ANCHOR = 'var activityReportGap = 1500 * time.Millisecond'

GO_JITTER_IMPORT_OLD = """import (
	"context"
	"log"
	"time\""""

GO_JITTER_IMPORT_NEW = """import (
	"context"
	"log"
	"math/rand"
	"time\""""

GO_JITTER_HELPER = """

// jitteredDelay 在基准延迟上加 0..+50% 的抖动。
//
// 【本发行版集成补丁】固定间隔是"机器节奏"，真人不会这么匀；抖动让上报的时间
// 密度不再落在固定周期上，降低按节奏做聚类的特征。
//
// base<=0 时**原样返回**：测试把基准置 0 来跳过等待，抖动必须一起跳过。
//
// 用 math/rand 而不是 time.Now().UnixNano() 取熵：后者在时钟粒度粗的平台上
// （实测 Windows）短时间内的低位几乎不变，抖动会退化成常数 —— 这是写这段时
// 被测试抓到的真实缺陷。Go 1.20+ 的全局 rand 已自动播种，无需手动 Seed。
func jitteredDelay(base time.Duration) time.Duration {
	if base <= 0 {
		return base
	}
	span := int64(base) / 2
	if span <= 0 {
		return base
	}
	return base + time.Duration(rand.Int63n(span))
}"""

GO_TRAVEL_DELAY_OLD = 'if !sleepCtx(ctx, travelAccountDelay) {'
GO_TRAVEL_DELAY_NEW = 'if !sleepCtx(ctx, jitteredDelay(travelAccountDelay)) {'

GO_ACT_DELAY_OLD = 'if !sleepCtx(ctx, activityAccountDelay) {'
GO_ACT_DELAY_NEW = 'if !sleepCtx(ctx, jitteredDelay(activityAccountDelay)) {'

GO_ACT_GAP_OLD = 'if !sleepCtx(ctx, activityReportGap) {'
GO_ACT_GAP_NEW = 'if !sleepCtx(ctx, jitteredDelay(activityReportGap)) {'


def build_text_edits(root: Path) -> list[tuple[Path, str, str, str]]:
    """全部文本补丁：(文件, 锚点, 替换成, 说明)。

    抽成函数是为了让测试能在**原始 vendor** 上做预检（见 tests/test_patch_preflight.py）——
    上游一旦重构、锚点失效，本地就能先发现，不必等构建或 CI。
    """
    go_headers = root / 'wb2api' / 'internal' / 'upstream' / 'headers.go'
    go_main = root / 'wb2api' / 'cmd' / 'server' / 'main.go'
    go_travel = root / 'wb2api' / 'internal' / 'scheduler' / 'travel.go'
    go_sched = root / 'wb2api' / 'internal' / 'scheduler' / 'scheduler.go'

    return [
        (root / 'manager' / 'server' / 'config.py',
         MANAGER_ANCHOR, MANAGER_HELPER + MANAGER_ANCHOR,
         'manager: 注入 _internal_direct_mounts()'),
        (root / 'manager' / 'server' / 'config.py',
         MANAGER_OLD, MANAGER_NEW,
         'manager: 内网请求直连（mounts）'),
        (root / 'wb2api' / 'internal' / 'upstream' / 'transport.go',
         GO_OLD, GO_NEW,
         'wb2api: Transport 读代理环境变量'),
        (root / 'manager' / 'server' / 'main.py',
         MAIN_IMPORT_OLD, MAIN_IMPORT_NEW,
         'manager: 导入套件路由'),
        (root / 'manager' / 'server' / 'main.py',
         MAIN_ROUTER_OLD, MAIN_ROUTER_NEW,
         'manager: 注册套件路由'),
        (root / 'manager' / 'web' / 'components' / 'common' / 'layout' / 'ManagementBar.tsx',
         MANAGEMENT_BAR_OLD, MANAGEMENT_BAR_NEW,
         'manager: 移除"发现新版本"会话弹窗'),
        # ── 以下三组是「上游删除后由本项目维护」的风控加固，见 UPSTREAMS.md ──
        (go_headers, GO_IMPORT_OLD, GO_IMPORT_NEW,
         'wb2api: 设备标识盐支持环境变量覆盖（补 import）'),
        (go_headers, GO_SALT_OLD, GO_SALT_NEW,
         'wb2api: 设备标识盐可轮换（WB2A_DEVICE_ID_SALT）'),
        (go_main, GO_MAXINFLIGHT_OLD, GO_MAXINFLIGHT_NEW,
         'wb2api: max_in_flight=0（不限）时启动告警'),
        (go_travel, GO_JITTER_ANCHOR, GO_JITTER_ANCHOR + GO_JITTER_HELPER,
         'wb2api: 新增上报延迟抖动助手'),
        (go_travel, GO_JITTER_IMPORT_OLD, GO_JITTER_IMPORT_NEW,
         'wb2api: 抖动助手需要 math/rand（补 import）'),
        (go_travel, GO_TRAVEL_DELAY_OLD, GO_TRAVEL_DELAY_NEW,
         'wb2api: 旅行上报延迟加抖动'),
        (go_sched, GO_ACT_DELAY_OLD, GO_ACT_DELAY_NEW,
         'wb2api: 活跃上报账号间延迟加抖动'),
        (go_sched, GO_ACT_GAP_OLD, GO_ACT_GAP_NEW,
         'wb2api: 活跃上报账号内间隔加抖动'),
    ]


def main() -> int:
    root = Path(sys.argv[1] if len(sys.argv) > 1 else '/src/vendor')
    overlay = _overlay_dir(sys.argv[2] if len(sys.argv) > 2 else None)

    text_edits = build_text_edits(root)

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
