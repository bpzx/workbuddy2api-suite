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

# 上游 commit 展示用的缩写长度。**必须与 upstreams.json 的 commit_short 一致** ——
# 否则面板会同时显示「固定提交 d1023f3 / 远端最新 d1023f37」这种同一个提交的
# 两种写法，看起来就像"有更新"（已真实发生过，见下面的 same_commit）。
SHORT_LEN = 7

_SEMVER = re.compile(r'^v?(\d+(?:\.\d+)*)$')
_GH_HEADERS = {
    'Accept': 'application/vnd.github+json',
    'User-Agent': 'workbuddy2api-suite',
}


def same_commit(a: str, b: str) -> bool:
    """两个 commit 缩写是否指向**同一个提交**。

    用**前缀**比较，而不是全等：拿到的缩写长度未必一致（我们的
    upstreams.json 是 7 位，GitHub API 可能给 8 位或完整 40 位），而同一个
    提交的任意长度前缀必然互为前缀。

    这条曾经写错成 `a != b`，后果是**同一个提交被判成"有更新"**：
    upstreams.json 里的 `d1023f3`（7 位）与 API 返回后截断的 `d1023f37`（8 位）
    不相等，于是面板一直提示上游有更新 —— 而它其实已经是最新。
    上游 updater.py 的判据正是前缀比较
    （`u_latest.startswith(local_head) or local_head.startswith(u_latest)`），
    这里与它保持一致。
    """
    x = str(a or '').strip().lower()
    y = str(b or '').strip().lower()
    if not x or not y:
        return False
    return x.startswith(y) or y.startswith(x)


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


# ── 上游固定版本（镜像内烤好的 upstreams.json）───────────────
def pinned_upstreams() -> dict:
    """镜像里锁定的上游 commit。读不到时返回空结构（不抛异常）。

    这是容器内判断"本套件捆绑的是哪个上游版本"的唯一正确来源 ——
    容器里没有上游 git 仓库，`git rev-parse` 走不通。
    """
    out: dict = {}
    try:
        data = json.loads(UPSTREAMS_FILE.read_text(encoding='utf-8'))
    except Exception:  # noqa: BLE001
        return out
    for name, info in (data.get('upstreams') or {}).items():
        if not isinstance(info, dict):
            continue
        commit = str(info.get('commit') or '')
        out[name] = {
            'commit': commit,
            # 与 API 侧用同一个缩写长度，避免同一个提交显示成两种写法
            'short': str(info.get('commit_short') or commit[:SHORT_LEN])[:SHORT_LEN],
            'subject': str(info.get('subject') or ''),
            'date': str(info.get('commit_date') or ''),
            'repo': str(info.get('repo') or ''),
        }
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


async def _fetch_release_tag(slug: str) -> str:
    """仓库里最新的正式版 tag。

    先看 tag（最可靠：CI 的镜像标签就直接来自 tag），没有再退回 Release。
    两者都取不到就返回空串 —— 版本提示是辅助信息，拿不到不该报错。
    """
    if not slug:
        return ''
    try:
        tags = await _gh_get(f'/repos/{slug}/tags?per_page=100')
        if isinstance(tags, list):
            found = highest_release_tag(
                [t.get('name') for t in tags if isinstance(t, dict)]
            )
            if found:
                return found
    except Exception:  # noqa: BLE001
        pass
    try:
        rel = await _gh_get(f'/repos/{slug}/releases/latest')
        if isinstance(rel, dict):
            return str(rel.get('tag_name') or '')
    except Exception:  # noqa: BLE001
        pass
    return ''


async def _fetch_head_commit(slug: str) -> dict:
    """仓库默认分支的最新提交。"""
    if not slug:
        return {'latest': '', 'date': '', 'subject': ''}
    try:
        commits = await _gh_get(f'/repos/{slug}/commits?per_page=1')
        if isinstance(commits, list) and commits:
            c = commits[0]
            commit = c.get('commit') or {}
            return {
                # 截断到与 upstreams.json 相同的长度（见 SHORT_LEN 的注释）
                'latest': str(c.get('sha') or '')[:SHORT_LEN],
                'date': str((commit.get('committer') or {}).get('date') or ''),
                'subject': str(commit.get('message') or '').split('\n')[0][:120],
            }
    except Exception:  # noqa: BLE001
        pass
    return {'latest': '', 'date': '', 'subject': ''}


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
    """向 GitHub 查套件与两个上游的最新版本（不做缓存判断）。"""
    pins = pinned_upstreams()
    suite_repo = (os.environ.get('WB_SUITE_REPO') or '').strip()
    gw_repo = str((pins.get('wb2api') or {}).get('repo') or 'Sliverkiss/workbuddy2api')
    mg_repo = str((pins.get('manager') or {}).get('repo') or 'ithtelab/workbuddy-manager')

    result: dict = {
        'checked_at': int(time.time()),
        'suite': {'latest': '', 'error': '', 'repo': suite_repo},
        'wb2api': {'latest': '', 'date': '', 'subject': '', 'error': '', 'repo': gw_repo},
        'manager': {'latest': '', 'error': '', 'repo': mg_repo},
    }

    if not suite_repo:
        result['suite']['error'] = '未配置套件仓库（WB_SUITE_REPO）'
    else:
        try:
            result['suite']['latest'] = await _fetch_release_tag(suite_repo)
            if not result['suite']['latest']:
                result['suite']['error'] = '仓库里没有正式版 tag'
        except Exception as exc:  # noqa: BLE001
            result['suite']['error'] = str(exc)[:120]

    try:
        result['wb2api'].update(await _fetch_head_commit(gw_repo))
        if not result['wb2api']['latest']:
            result['wb2api']['error'] = '取不到远端提交'
    except Exception as exc:  # noqa: BLE001
        result['wb2api']['error'] = str(exc)[:120]

    try:
        result['manager']['latest'] = await _fetch_release_tag(mg_repo)
        if not result['manager']['latest']:
            result['manager']['error'] = '仓库里没有正式版 tag'
    except Exception as exc:  # noqa: BLE001
        result['manager']['error'] = str(exc)[:120]

    return result


async def check(force: bool = False) -> dict:
    """版本检查：套件 + 两个上游。结果缓存 6 小时（GitHub 限流较严）。"""
    cache = _read_cache()
    age = time.time() - float(cache.get('checked_at') or 0)
    if force or not cache or age > CHECK_TTL:
        fresh = await _fetch_all()
        # 保留上次成功的结果：临时网络故障不该让界面从"有新版本"掉成"未知"
        for key in ('suite', 'wb2api', 'manager'):
            if (fresh.get(key) or {}).get('error') and (cache.get(key) or {}).get('latest'):
                fresh[key] = {**cache[key], 'error': fresh[key]['error']}
        _write_cache(fresh)
        cache = fresh
    else:
        fresh = cache

    pins = pinned_upstreams()
    cur = current_version()

    s_latest = str((fresh.get('suite') or {}).get('latest') or '')
    s_dev = is_dev_version(cur)
    # 开发构建（无版本号）时，只要仓库存在正式版就算"可切换到该版本"；
    # 有版本号时严格比较，避免把降级当更新。
    s_has = bool(s_latest) and (s_dev or version_newer(s_latest, cur))

    gw = fresh.get('wb2api') or {}
    gw_pin = (pins.get('wb2api') or {}).get('short') or ''
    gw_latest = str(gw.get('latest') or '')
    # 用前缀比较（same_commit），不能用全等：两边缩写长度可能不同，
    # 全等会把同一个提交判成"有更新" —— 这个假阳性真实出现过。
    gw_has = bool(gw_pin and gw_latest) and not same_commit(gw_pin, gw_latest)

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
        'wb2api': {
            'current': gw_pin,
            'latest': gw_latest,
            'has_update': gw_has,
            'date': str(gw.get('date') or ''),
            'subject': str(gw.get('subject') or ''),
            'repo': str(gw.get('repo') or ''),
            'error': str(gw.get('error') or ''),
        },
        'manager': {
            'current': mg_cur,
            'latest': mg_latest,
            'has_update': mg_has,
            'repo': str(mg.get('repo') or ''),
            'error': str(mg.get('error') or ''),
        },
        'has_any': s_has or gw_has or mg_has,
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
