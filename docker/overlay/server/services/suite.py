"""套件自身的版本查询与一键更新。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

职责
----
1. **套件版本**：当前值由构建期从 git tag 写入镜像（见 docker/Dockerfile 的
   SUITE_VERSION），运行期从环境变量或 /opt/suite/version 读；"最新正式版"
   查 GitHub tag。两者比较给出「是否有新版可更新」。
2. **两个上游的版本**：只报告「是否有更新」，**不提供更新动作** ——
   上游更新必须走宿主机 `./scripts/sync-upstreams.sh` + 重建镜像。
3. **一键更新**：转发给 updater 侧车（它持有 docker socket），并读取进度。

为什么上游的「当前版本」读 /opt/suite/upstreams.json 而不是 git
----------------------------------------------------------------
上游的 updater.py 用 `git rev-parse HEAD` 判断本地上游版本，但**容器里没有
上游的 git 仓库**，那个函数恒返回空串，于是 `upstream.has_update` 永远为
False —— 表现为「上游明明发了新版，面板却说不更新」。镜像里烤好的
upstreams.json 记录了构建时锁定的确切 commit，才是容器内的正确事实来源。

关于「只认正式 tag」
--------------------
CI 在 `v*` tag 上产出 `v1.2.3` 这类版本，日常 push main 产出的是 `sha-xxxxxxx`。
混在一起比会把「每次提交」都变成"新版本"，纯噪音。因此这里**只用
vX.Y.Z 形式的 tag** 作为"最新正式版"，当前是 sha-*/dev 时如实标为
`is_dev`，由界面显示为「开发构建」而不是伪造一个版本号。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

from .. import config
from . import updater

# ── 路径与配置 ───────────────────────────────────────────
# 都做成可覆盖的，便于测试与本地调试。
SUITE_VERSION_FILE = Path(os.environ.get('WB_SUITE_VERSION_FILE') or '/opt/suite/version')
UPSTREAMS_FILE = Path(os.environ.get('WB_SUITE_UPSTREAMS_FILE') or '/opt/suite/upstreams.json')
STATUS_FILE = config.DATA_DIR / 'suite-update-status.json'
CHECK_CACHE_FILE = config.DATA_DIR / 'suite-check.json'

# GitHub 未认证 API 限流为 60 次/小时/IP，与上游一致做 6 小时缓存
CHECK_TTL = 6 * 3600

# updater 侧车（同一 compose 网络，不发布到宿主）
UPDATER_BASE = (os.environ.get('WB_SUITE_UPDATER') or 'http://updater:7865').rstrip('/')
UPDATER_TOKEN = os.environ.get('SUITE_UPDATER_TOKEN') or ''

# 状态文件被认为"仍在进行中"的最长时间，超过即视为异常遗留
RUNNING_TTL = 3600

_SEMVER = re.compile(r'^v?(\d+(?:\.\d+)*)$')
_GH_HEADERS = {
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'workbuddy2api-suite',
}


# ── 版本工具 ─────────────────────────────────────────────
def current_version() -> str:
    """本套件的版本号。构建期写入，取不到时返回 'unknown'。"""
    v = (os.environ.get('WB_SUITE_VERSION') or '').strip()
    if not v:
        try:
            v = SUITE_VERSION_FILE.read_text(encoding='utf-8').strip()
        except Exception:  # noqa: BLE001
            v = ''
    return v or 'unknown'


def parse_semver(value: str) -> tuple[int, ...] | None:
    """v1.2.3 → (1,2,3)。非版本形式（sha-xxx / dev / unknown）返回 None。"""
    m = _SEMVER.match(str(value or '').strip())
    if not m:
        return None
    parts = [int(p) for p in m.group(1).split('.') if p != '']
    return tuple(parts) or None


def is_dev_version(value: str) -> bool:
    """是否是"没有版本号"的构建（main 分支产出）。"""
    return parse_semver(value) is None


def _padded_gt(a: tuple[int, ...], b: tuple[int, ...]) -> bool:
    """按"缺位补 0"的语义比较版本元组：1.2 > 1.2.0 为假，1.2.1 > 1.2 为真。"""
    n = max(len(a), len(b))
    return a + (0,) * (n - len(a)) > b + (0,) * (n - len(b))


def version_newer(remote: str, current: str) -> bool:
    """remote 是否**严格新于** current。两端有一个解析不了就返回 False。

    必须是「大于」而非「不等于」：否则本地领先或降级时会冒出
    「v1.2.4 → v1.2.3」这种把降级当更新的提示。
    """
    r = parse_semver(remote)
    c = parse_semver(current)
    if r is None or c is None:
        return False
    return _padded_gt(r, c)


def highest_release_tag(tags: list[str]) -> str:
    """从 tag 列表里挑出最大的正式版；没有正式版 tag 则返回空串。

    GitHub 的 /tags 是**按提交时间**倒序返回的，不是按版本号，所以必须自己
    逐个比较，不能取第一个。
    """
    best_v: tuple[int, ...] | None = None
    best_tag = ''
    for raw in tags:
        tag = str(raw or '').strip()
        v = parse_semver(tag)
        if v is None:
            continue
        if best_v is None or _padded_gt(v, best_v):
            best_v, best_tag = v, tag
    return best_tag


# ── 上游仓库地址（镜像内烤好的 upstreams.json）───────────────
def pinned_upstreams() -> dict:
    """镜像里记录的上游仓库信息，按名字给出 `{name: {'repo': ...}}`。

    读不到时返回空结构（不抛异常）。

    这里**只取 repo**：它用于向 GitHub 查"最新正式版"。历史上的 commit / 缩写 /
    提交说明曾用于面板的 commit 比对，但 wb2api 上游删除后那一行已从面板移除，
    比对逻辑也一并删掉了 —— 保留没有消费方的字段只会变成死数据。
    完整的锁定记录仍以 `upstreams.json` 与 [`UPSTREAMS.md`](../UPSTREAMS.md) 为准。
    """
    out: dict = {}
    try:
        data = json.loads(UPSTREAMS_FILE.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return out
    for name, info in (data.get('upstreams') or {}).items():
        if not isinstance(info, dict):
            continue
        out[name] = {'repo': str(info.get('repo') or '')}
    return out


# ── HTTP 客户端 ──────────────────────────────────────────
def _direct_client(timeout: float):
    """直连客户端：**刻意不用** config.http_client。

    侧车是同一 compose 网络内的内部服务。而 config.http_client 在配了
    WB_HTTP_PROXY 时会把**所有**请求都送去代理（内网的也不例外），那样连不上
    侧车。上游对 wb2api 的内网请求是靠 docker/patches/apply.py 的 mounts
    补丁直连的；这里我们自己控制客户端，不需要也不该走代理。
    """
    import httpx

    return httpx.AsyncClient(timeout=timeout, trust_env=False)


async def _gh_get(path: str) -> object:
    """调 GitHub API。外网请求，**走** WB_HTTP_PROXY（如果有配）。"""
    async with config.http_client(15) as client:
        resp = await client.get(f'https://api.github.com{path}', headers=_GH_HEADERS)
        resp.raise_for_status()
        return resp.json()


def _gh_reason(exc: Exception) -> str:
    """把 GitHub 请求异常翻成可读原因。

    为什么要区分：以前这里把所有异常吞掉、统一报「仓库里没有正式版 tag」，
    而真实原因可能是「仓库私有/不存在」或「触发了未认证限流」—— 与"确实没有 tag"
    完全是两回事，会把人引到错误的方向（这个假警报真实出现过）。
    """
    code = getattr(getattr(exc, 'response', None), 'status_code', None)
    if code == 404:
        return '仓库不可访问（不存在或为私有）'
    if code == 403:
        return 'GitHub 拒绝（未认证接口每小时 60 次，可能已限流）'
    if code:
        return f'GitHub 返回 HTTP {code}'
    return f'{type(exc).__name__}: {str(exc)[:80]}'


async def _fetch_release_tag(slug: str) -> tuple[str, str]:
    """仓库里最新的正式版 tag，返回 (tag, 失败原因)。

    先看 tag（最可靠：CI 的镜像标签就直接来自 tag），没有再退回 Release。
    两者都取不到时**带上原因**返回，供界面如实显示。
    """
    if not slug:
        return '', '未配置仓库地址'
    reason = ''
    try:
        tags = await _gh_get(f'/repos/{slug}/tags?per_page=100')
        if isinstance(tags, list):
            found = highest_release_tag(
                [t.get('name') for t in tags if isinstance(t, dict)]
            )
            if found:
                return found, ''
    except Exception as exc:  # noqa: BLE001
        reason = _gh_reason(exc)
    try:
        rel = await _gh_get(f'/repos/{slug}/releases/latest')
        if isinstance(rel, dict) and rel.get('tag_name'):
            return str(rel['tag_name']), ''
    except Exception as exc:  # noqa: BLE001
        reason = reason or _gh_reason(exc)
    return '', reason or '仓库里没有正式版 tag'


# ── 版本检查 ─────────────────────────────────────────────
def _read_cache() -> dict:
    try:
        data = json.loads(CHECK_CACHE_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _write_cache(data: dict) -> None:
    try:
        config.DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = CHECK_CACHE_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(CHECK_CACHE_FILE)
    except Exception:  # noqa: BLE001
        pass


async def _fetch_all() -> dict:
    """向 GitHub 查套件与上游管理端的最新版本（不做缓存判断）。

    **只查这两项。** wb2api 的仓库已被删除，面板上那一行也一并移除了 ——
    继续查它只会得到 404，而下面的"保留上次成功结果"逻辑会把它**永久**留成
    "有新版本可更新"的假象（仓库都不存在了，谈不上更新）。
    """
    pins = pinned_upstreams()
    suite_repo = (os.environ.get('WB_SUITE_REPO') or '').strip()
    mg_repo = str((pins.get('manager') or {}).get('repo') or 'ithtelab/workbuddy-manager')

    result: dict = {
        'checked_at': int(time.time()),
        'suite': {'latest': '', 'error': '', 'repo': suite_repo},
        'manager': {'latest': '', 'error': '', 'repo': mg_repo},
    }

    if not suite_repo:
        result['suite']['error'] = '未配置套件仓库（WB_SUITE_REPO）'
    else:
        result['suite']['latest'], result['suite']['error'] = await _fetch_release_tag(suite_repo)

    result['manager']['latest'], result['manager']['error'] = await _fetch_release_tag(mg_repo)
    return result


async def check(force: bool = False) -> dict:
    """版本检查：套件 + 两个上游。结果缓存 6 小时（GitHub 限流较严）。"""
    cache = _read_cache()
    age = time.time() - float(cache.get('checked_at') or 0)
    if force or not cache or age > CHECK_TTL:
        fresh = await _fetch_all()
        # 保留上次成功的结果：**临时**网络故障不该让界面从"有新版本"掉成"未知"。
        # 但必须说清"这个版本号是上次检测的" —— 否则界面会同时显示一个版本号和
        # 一条报错却不解释两者关系（"仓库里没有正式版 tag"配上具体 tag 就是这么来的，
        # 真实原因是那一次查询失败）。
        for key in ('suite', 'manager'):
            if (fresh.get(key) or {}).get('error') and (cache.get(key) or {}).get('latest'):
                fresh[key] = {
                    **cache[key],
                    'error': fresh[key]['error'] + '；显示的版本号来自上次成功检测',
                }
        _write_cache(fresh)
        cache = fresh
    else:
        fresh = cache

    cur = current_version()

    s_latest = str((fresh.get('suite') or {}).get('latest') or '')
    s_dev = is_dev_version(cur)
    # 开发构建（无版本号）时，只要仓库存在正式版就算"可切换到该版本"；
    # 有版本号时严格比较，避免把降级当更新。
    s_has = bool(s_latest) and (s_dev or version_newer(s_latest, cur))

    mg = fresh.get('manager') or {}
    mg_cur = updater.current_version()
    mg_latest = str(mg.get('latest') or '')
    mg_has = bool(mg_latest) and version_newer(mg_latest, mg_cur)

    return {
        'checked_at': int(fresh.get('checked_at') or 0),
        'cached': not force and age <= CHECK_TTL,
        'suite': {
            'current': cur,
            'latest': s_latest,
            'has_update': s_has,
            'is_dev': s_dev,
            'repo': str((fresh.get('suite') or {}).get('repo') or ''),
            'error': str((fresh.get('suite') or {}).get('error') or ''),
        },
        'manager': {
            'current': mg_cur,
            'latest': mg_latest,
            'has_update': mg_has,
            'repo': str(mg.get('repo') or ''),
            'error': str(mg.get('error') or ''),
        },
        'has_any': s_has or mg_has,
    }


# ── 一键更新 ─────────────────────────────────────────────
def _read_status_file() -> dict | None:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


async def _updater_call(method: str, path: str, timeout: float) -> tuple[int, dict]:
    """调侧车。返回 (状态码, 响应体)；连接失败抛异常由调用方处理。"""
    headers = {'Authorization': f'Bearer {UPDATER_TOKEN}'} if UPDATER_TOKEN else {}
    async with _direct_client(timeout) as client:
        resp = await client.request(method, f'{UPDATER_BASE}{path}', headers=headers)
        try:
            body = resp.json()
        except Exception:  # noqa: BLE001
            body = {}
        return resp.status_code, (body if isinstance(body, dict) else {})


async def read_status() -> dict:
    """更新状态。

    `running` 以**侧车**的判断为准 —— 它看的是 helper 容器还在不在，比
    "pid 还活着吗"可靠（pid 判断在容器重建场景下会误判）。侧车不可达时退回
    读状态文件，让用户至少还能看到上次的结果。
    """
    local = _read_status_file()
    base = {
        'version': current_version(),
        'sidecar': UPDATER_BASE,
        'status': local,
    }

    if not UPDATER_TOKEN:
        return {
            **base, 'available': False, 'running': False,
            'reason': '未启用一键更新：在 .env 里设置 SUITE_UPDATER_TOKEN 即可启用',
        }

    try:
        code, body = await _updater_call('GET', '/status', 5)
        if code == 200:
            return {
                **base,
                'available': True,
                'reason': '',
                'running': bool(body.get('running')),
                'status': body.get('status') or local,
            }
        reason = str(body.get('message') or f'更新服务返回 HTTP {code}')
    except Exception:  # noqa: BLE001
        reason = f'连不上更新服务（{UPDATER_BASE}）：请确认它已启动（docker compose up -d updater）'

    # 侧车不可达：只把"新鲜且未结束"的状态当作仍在进行，避免陈旧的
    # running=true 永久卡住界面
    running = bool((local or {}).get('running')) and (
        time.time() - float((local or {}).get('started_at') or 0) < RUNNING_TTL
    )
    return {**base, 'available': False, 'running': running, 'reason': reason}


async def trigger_update() -> tuple[bool, str]:
    """触发一键更新。返回 (是否已开始, 说明)。"""
    if not UPDATER_TOKEN:
        return False, '未启用一键更新：在 .env 里设置 SUITE_UPDATER_TOKEN 即可启用'
    try:
        code, body = await _updater_call('POST', '/update', 10)
    except Exception:  # noqa: BLE001
        return False, f'连不上更新服务（{UPDATER_BASE}）：请确认它已启动（docker compose up -d updater）'
    if code in (200, 202):
        return True, str(body.get('message') or '更新已开始')
    return False, str(body.get('message') or f'更新服务返回 HTTP {code}')
