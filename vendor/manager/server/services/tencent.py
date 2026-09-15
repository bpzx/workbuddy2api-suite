"""腾讯 CodeBuddy 登录 / 签到协议客户端（对齐 workbuddy2api cmd/login）。

国内版与国际版共用**同一套路径**，只是 base 与 Origin/Referer/UA 随版本变；
少数接口的候选路径顺序两边相反（见 realm.billing_paths 的注释）。
本模块内所有请求都经 realm 层取端点，不再直接引用写死的域名。
"""
from __future__ import annotations

import json
import time
from typing import Any

import httpx

from .. import config
from .realm import (
    CN,
    GLOBAL,
    Realm,
    attribution_headers,
    billing_base,
    billing_headers,
    billing_paths,
    chat_base,
    chat_paths,
    device_token_for,
    headers as realm_headers,
    realm_of,
    resolve_realm,
    supports_checkin,
)

# 扫码 state 缓存：state -> (登记时间, 发起时的版本)。
# 记 realm 是为了在回调时校验一致——若用户先开国内版的码、又切到国际版再轮询，
# 不校验就会把国际版的 token 写进国内版的会话流程（上游 validateRealmMatch 同此意图）。
_state_cache: dict[str, tuple[float, Realm]] = {}
STATE_TTL = 300


def _envelope(resp: httpx.Response) -> tuple[int, Any]:
    """腾讯接口统一信封 {code, msg, data}；HTTP 4xx 也可能是业务信封。"""
    try:
        env = resp.json()
    except Exception:
        return resp.status_code, None
    if isinstance(env, dict) and 'code' in env:
        return int(env.get('code', -1)), env.get('data')
    return resp.status_code, env


def _hdr(realm: Realm, token: str | None = None) -> dict:
    """该版本的通用请求头（Origin/Referer/UA 随版本变）。"""
    return realm_headers(realm, token)


def _billing_hdr(realm: Realm, auth: dict | str | None = None) -> dict:
    """billing 域请求头（带身份头，对齐上游 BillingHeaders）。

    billing 域（签到 / 积分 / trial / 注册）在上游一直携带 X-User-Id 等身份头，
    我们此前只发通用头——Go 侧测试明确断言 trial 必须带 X-User-Id，签到与查
    积分同理。这里统一走 realm.billing_headers。

    auth 允许传 token 字符串（兼容既有调用），此时只有 Authorization，
    身份头缺失——新调用点应尽量传完整 dict。
    """
    if isinstance(auth, str):
        return realm_headers(realm, auth)
    return billing_headers(realm, auth)


async def start_login(realm: Realm = CN) -> dict:
    """发起扫码登录。realm 决定用哪套端点与 Origin（默认国内版）。"""
    async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
        resp = await client.post(
            f'{chat_base(realm)}/v2/plugin/auth/state',
            params={'platform': 'CLI'},
            json={},
            headers=_hdr(realm),
        )
    code, data = _envelope(resp)
    if code != 0 or not data:
        raise RuntimeError(f'获取授权链接失败 code={code}')
    state = data.get('state') or ''
    if state:
        _state_cache[state] = (time.time(), realm)
    return {'state': state, 'authUrl': data.get('authUrl') or '', 'realm': realm}


def is_pending(state: str) -> bool:
    return state in _state_cache


def state_realm(state: str) -> Realm | None:
    """该 state 登记时用的版本；未知返回 None。"""
    entry = _state_cache.get(state)
    return entry[1] if entry else None


def drop_state(state: str) -> None:
    _state_cache.pop(state, None)


async def poll_login(state: str, realm: Realm | None = None) -> dict:
    """轮询扫码结果。waiting / expired / ready(含 token 与账号信息)。

    realm 用于校验一致性：轮询方声明的版本必须与发起时一致，
    否则返回 realm_mismatch 而不是把另一个版本的凭证混进来。
    不传 realm 时沿用登记时的版本（兼容既有调用）。
    """
    entry = _state_cache.get(state)
    if entry is None:
        return {'status': 'invalid'}
    created, reg_realm = entry
    if time.time() - created > STATE_TTL:
        drop_state(state)
        return {'status': 'expired'}
    if realm is not None and realm != reg_realm:
        # 不 drop：用户可能切错了版本，切回去还能继续用这张码
        return {'status': 'realm_mismatch', 'expected': reg_realm, 'got': realm}
    realm = reg_realm

    async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
        resp = await client.get(
            f'{chat_base(realm)}/v2/plugin/auth/token',
            params={'state': state},
            headers=_hdr(realm),
        )
        code, data = _envelope(resp)
        if code != 0 or not data or not data.get('accessToken'):
            return {'status': 'waiting'}

        access_token = data['accessToken']
        refresh_token = data.get('refreshToken', '')
        expires_in = int(data.get('expiresIn', 3600) or 3600)
        domain = data.get('domain', '')

        acct_resp = await client.get(
            f'{chat_base(realm)}/v2/plugin/login/account',
            params={'state': state},
            headers=_hdr(realm, access_token),
        )
    _, acct = _envelope(acct_resp)
    acct = acct or {}
    uid = acct.get('uid')
    if not uid:
        return {'status': 'waiting'}

    drop_state(state)
    return {
        'status': 'ready',
        'uid': str(uid),
        'nickname': acct.get('nickname', '') or '',
        'enterprise_id': acct.get('enterpriseId', '') or '',
        'access_token': access_token,
        'refresh_token': refresh_token,
        'expires_at': int(time.time()) + expires_in,
        'domain': domain,
        'realm': realm,
    }


def write_auth_file(account: dict) -> tuple[str, bool]:
    """严格按 workbuddy2api 的嵌套结构落盘，返回 (文件名, 是否覆盖)。

    realm 写在 `auth` 对象内（与 domain 同级）——上游就是从这里读的。
    落盘用的是 resolve_realm（**不含逃生门**）：否则一旦 global.enabled=false，
    新登的国际版账号会被永久写成 cn（上游 BackfillRealm 注释专门警告过这点）。

    device_token 是顶层键（与上游 auth 解析位置一致）。**重新登录不能把它冲掉**：
    它是设备风控凭据，用户手动写入后若因换 token 重登而丢失，会静默降级风控
    形态——所以这里读旧文件保留，而不是当作字段缺失。
    """
    uid = account['uid']
    config.AUTH_DIR.mkdir(parents=True, exist_ok=True)
    target = config.AUTH_DIR / f'workbuddy-{uid}.json'
    existed = target.exists()
    domain = account.get('domain', '')
    resolved = resolve_realm(account.get('realm'), domain)

    # 保留旧的 device_token（若有）。读失败不影响主流程。
    old_device_token = ''
    if existed:
        try:
            old = json.loads(target.read_text(encoding='utf-8'))
            if isinstance(old, dict):
                old_device_token = str(old.get('device_token') or '')
        except Exception:  # noqa: BLE001
            old_device_token = ''

    payload = {
        'account': {
            'uid': uid,
            'enterpriseId': account.get('enterprise_id', ''),
            'nickname': account.get('nickname', ''),
        },
        'auth': {
            'accessToken': account['access_token'],
            'refreshToken': account.get('refresh_token', ''),
            'expiresAt': account['expires_at'],
            'domain': domain,
            'realm': resolved,
        },
    }
    if old_device_token:
        payload['device_token'] = old_device_token
    target.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding='utf-8')
    return target.name, existed


async def checkin(access_token: str | dict, realm: Realm = CN) -> tuple[int, str]:
    """每日签到。10001 = 今日已签到，属正常幂等。

    **国际版没有签到体系**：上游调度器对 global 账号直接过滤、不发起任何请求
    （理由是避免风控）。这里同样直接返回、不打接口——否则会白吃一个 4xx，
    还可能被当成异常行为。

    access_token 可传字符串（只有 Authorization）或完整 auth dict
    （额外带上 X-User-Id 等身份头——上游 billing 域一直这么做，见 BillingHeaders）。
    """
    if not supports_checkin(realm):
        return -2, '国际版无签到体系，已跳过'
    try:
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            resp = await client.post(
                f'{billing_base(realm)}{billing_paths(realm, "daily-checkin")[0]}',
                json={},
                headers=_billing_hdr(realm, access_token),
            )
        code, _ = _envelope(resp)
        if code == 0:
            return 0, '签到成功'
        if code == 10001:
            return 10001, '今日已签到'
        return code, f'签到返回 code={code}'
    except Exception as exc:  # noqa: BLE001
        return -1, f'签到异常: {exc}'


def _pick_accounts(data: object) -> list | None:
    """从 billing 响应里取套餐数组（兼容多层信封）。"""
    return _extract_resource_accounts(data)


def _credits_of(accounts: list) -> float:
    total = 0.0
    for item in accounts:
        if not isinstance(item, dict):
            continue
        total += _package_remain(item)
    return max(0.0, total)


async def fetch_credits(auth: dict) -> tuple[bool, int | float | None, str]:
    """查询账号**实时**积分余额。

    为什么必须由管理端自己查：workbuddy2api 只在它的定时任务
    （签到 / 保活时刻）刷新 credits，之后 /status 里一直是旧值；
    手动签到也不会触发它刷新。因此要拿到当前余额只能直接调腾讯接口。

    口径与上游保持一致：优先取套餐的 CycleCapacityRemain，
    无 Cycle 字段时退回 CapacityRemain（见 upstream.UserResource）。
    billing 路径按版本分派（国际版无 /v2 前缀优先，与国内版相反）。
    返回 (ok, credits, message)。
    """
    access_token = str(auth.get('access_token') or '')
    if not access_token:
        return False, None, '该账号无有效 accessToken'
    realm = realm_of(auth)

    now = time.time()
    body = {
        'PageNumber': 1,
        'PageSize': 100,
        'ProductCode': 'p_tcaca',
        'Status': [0, 3],
        'PackageEndTimeRangeBegin': time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(now)),
        'PackageEndTimeRangeEnd': time.strftime(
            '%Y-%m-%d %H:%M:%S', time.localtime(now + 365 * 101 * 24 * 3600)
        ),
    }
    try:
        # 路径按版本分派；国际版以无 /v2 前缀为首选、404 时回落（与国内版相反）
        # 头走 billing 域（带 X-User-Id 等身份头，对齐上游 BillingHeaders）
        hdr = _billing_hdr(realm, auth)
        resp = None
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            for path in billing_paths(realm, 'user-resource'):
                resp = await client.post(
                    f'{billing_base(realm)}{path}', json=body, headers=hdr
                )
                if resp.status_code != 404:
                    break
        assert resp is not None
        code, data = _envelope(resp)
        if code != 0 or not data:
            return False, None, f'查询失败 code={code}'

        accounts = _extract_resource_accounts(data)
        if accounts is None:
            return False, None, '响应结构无法识别'

        total = 0.0
        for item in accounts:
            if not isinstance(item, dict):
                continue
            total += _package_remain(item)
        # 上游会把负值钳为 0；保留小数以贴近官方展示
        total = max(0.0, total)
        return True, (int(total) if total.is_integer() else round(total, 2)), '查询成功'
    except Exception as exc:  # noqa: BLE001
        return False, None, f'查询异常: {exc}'


async def fetch_models(auth: dict) -> tuple[bool, list | str]:
    """拉取该账号可用的 CLI 模型（含显示名、上下文、最大输出、推理档位）。

    为什么要管理端自己拉、而不是用上游的 /v1/models：上游把腾讯返回的
    `name`（显示名）和 `reasoning.supportedEfforts`（推理档位）**丢掉了**，
    只暴露 id / context_length / max_output_tokens。要做「模型中心」这类
    带显示名与能力的展示，只能照上游约定直连腾讯接口（同一路径、同一信封）。

    口径与上游 FetchModels 保持一致：
      - 只取 agents 里名为 `cli` 的模型 id 列表（那才是对 CLI 暴露的）
      - `disabled` 的条目不收录
    路径按版本分派：国际版 `/v2/enterprises/personal/models` 优先、
    `/console/...` 回落；国内版直接 `/console/...`。
    返回 (ok, models 或错误信息)。不含任何凭据。
    """
    access_token = str(auth.get('access_token') or '')
    if not access_token:
        return False, '该账号无有效 accessToken'
    realm = realm_of(auth)
    paths = (
        ['/v2/enterprises/personal/models', '/console/enterprises/personal/models']
        if realm == GLOBAL
        else ['/console/enterprises/personal/models']
    )
    try:
        data = None
        last_code = -1
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            for path in paths:
                resp = await client.get(
                    f'{chat_base(realm)}{path}', headers=_hdr(realm, access_token)
                )
                code, body = _envelope(resp)
                last_code = code
                if code == 0 and isinstance(body, dict):
                    data = body
                    break
        if not isinstance(data, dict):
            return False, f'模型接口返回 code={last_code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'模型接口异常: {exc}'

    raw_models = data.get('models') if isinstance(data.get('models'), list) else []
    agents = data.get('agents') if isinstance(data.get('agents'), list) else []

    cli_ids: list[str] = []
    for ag in agents:
        if isinstance(ag, dict) and ag.get('name') == 'cli':
            ids = ag.get('models')
            if isinstance(ids, list):
                cli_ids = [str(x) for x in ids if x]
            break

    info: dict[str, dict] = {}
    for m in raw_models:
        if not isinstance(m, dict) or not m.get('id'):
            continue
        reasoning = m.get('reasoning') if isinstance(m.get('reasoning'), dict) else {}
        efforts = reasoning.get('supportedEfforts')
        info[str(m['id'])] = {
            'id': str(m['id']),
            'name': str(m.get('name') or '').strip(),
            'context_length': _as_int(m.get('maxInputTokens')),
            'max_output_tokens': _as_int(m.get('maxOutputTokens')),
            'disabled': bool(m.get('disabled')),
            'efforts': [str(x) for x in efforts if x] if isinstance(efforts, list) else [],
        }

    # cli 列表为空时退回全部未禁用模型：上游此时直接报错，但管理端只是展示，
    # 给个可用列表比整页空白更有用（来源会在 UI 上如实标注）。
    ids = cli_ids or list(info.keys())
    out = [info[i] for i in ids if i in info and not info[i]['disabled']]
    if not out:
        return False, '模型接口未返回任何可用模型'
    return True, out


def _as_int(v: object) -> int:
    try:
        return int(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return 0


def _extract_resource_accounts(data: object) -> list | None:
    """不同层级的信封包装，尽量把套餐数组取出来。"""
    cur = data
    for _ in range(4):
        if isinstance(cur, dict):
            if 'Accounts' in cur and isinstance(cur['Accounts'], list):
                return cur['Accounts']
            # 逐层下钻：Response / Data / data 等
            for key in ('Response', 'Data', 'data', 'response'):
                if isinstance(cur.get(key), (dict, list)):
                    cur = cur[key]
                    break
            else:
                return None
        elif isinstance(cur, list):
            return cur
        else:
            return None
    return None


def _package_remain(item: dict) -> float:
    """单个套餐的剩余额度，口径与上游 UserResource 一致。"""
    def num(key: str) -> float:
        v = item.get(key)
        return float(v) if isinstance(v, (int, float)) else 0.0

    cycle_size = num('CycleCapacitySize')
    cycle_remain = num('CycleCapacityRemain')
    cycle_used = num('CycleCapacityUsed')
    if cycle_size > 0 or cycle_remain > 0 or cycle_used > 0:
        return cycle_remain
    return num('CapacityRemain')


async def probe_account(auth: dict, model: str = 'glm-5.2') -> tuple[bool, str]:
    """以最小**流式**对话请求探测账号可用性。

    必须用流式：上游强制要求 stream=true，非流式会返回
    code=11101「Non-stream chat request is currently not supported」
    （见 workbuddy2api payload.go 中强制 obj["stream"]=true 的处理）。
    因此这里发起流式请求，读到首个数据块即判定可用，随即断开。

    请求头复刻上游 ChatHeaders：缺失字段用 X-No-* 约定。
    国内版带 X-Enterprise-Id / X-Domain（有值则带、否则 X-No-*）；
    国际版按上游 injectGlobalChatHeaders 固定 `X-No-Enterprise-Id: 1` +
    `X-Domain: www.workbuddy.ai`，且 chat 路径以 /console 优先、404 回落 /v2。
    """
    import time as _time

    access_token = str(auth.get('access_token') or '')
    if not access_token:
        return False, '该账号无有效 accessToken'

    realm = realm_of(auth)
    uid = str(auth.get('uid') or '')
    enterprise_id = str(auth.get('enterprise_id') or '')
    domain = str(auth.get('domain') or '')
    # 账号自带的 domain 若已是完整 URL，说明部署方指定了 base，优先采用
    base = domain if domain.startswith('http') else chat_base(realm)

    headers = _hdr(realm, access_token)
    # chat 是流式路径：Accept 覆盖为流式形态（对齐上游 D6 —— 非流式默认收紧为
    # application/json，只有 chat 才声明 text/event-stream）
    headers['Accept'] = 'application/json, text/event-stream'
    headers['X-User-Id' if uid else 'X-No-User-Id'] = uid or '1'
    if realm == GLOBAL:
        # 镜像上游：国际版声明「个人账号无企业 ID」并断言国际域
        headers['X-No-Enterprise-Id'] = '1'
        headers['X-Domain'] = 'www.workbuddy.ai'
    else:
        headers['X-Enterprise-Id' if enterprise_id else 'X-No-Enterprise-Id'] = enterprise_id or '1'
        headers['X-Domain' if domain else 'X-No-Department-Info'] = domain or '1'
    headers['X-Product'] = 'SaaS'
    # 用量归属头：默认伪造官方桌面端指纹（X-Agent-Purpose + X-IDE-* 四头），
    # 与上游 2026-09-14 起的 injectAttribution 默认值一致——此前只有
    # X-Product=SaaS，在官网用量归因里是显眼的「网关特征」。
    headers.update(attribution_headers())
    # 设备风控头：上游 chat 域同样注入（ChatHeaders → injectDeviceToken）。
    # 三级回退 auth 每号 > config 全局 > 文件；取不到就不发（与上游一致）。
    _dt = device_token_for(auth)
    if _dt:
        headers['X-Device-Token'] = _dt

    payload = {
        'model': model,
        # **首条必须是 system**：上游要求 messages[0].role == 'system'，否则返回
        # 11-128「first message is not system prompt」。
        #
        # 为什么以前不报错：过去 prompt.mode 缺省是 custom，上游会用自有提示词
        # 在头部插一条 system；改用缺省 passthrough（2026-09-14 起）后不再插入，
        # 客户端原样透传 —— 我们这条只带 user 的探测就被拒了。
        # 探测请求是我们自己造的（不是真实客户端），所以这里显式补上。
        'messages': [
            {'role': 'system', 'content': 'You are a helpful assistant.'},
            {'role': 'user', 'content': 'ping'},
        ],
        'max_tokens': 1,
        'stream': True,
    }

    started = _time.time()
    try:
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            for path in chat_paths(realm):
                async with client.stream(
                    'POST', f'{base}{path}', json=payload, headers=headers,
                ) as resp:
                    if resp.status_code in (404, 405) and path != chat_paths(realm)[-1]:
                        continue  # 换下一条候选路径（上游同此回落逻辑）
                    if resp.status_code >= 400:
                        raw = (await resp.aread()).decode('utf-8', errors='replace')
                        code, msg = _parse_error_body(raw, resp.status_code)
                        return False, _explain_code(code, msg)
                    # 读到首个非空数据块即可确认账号可用，无需等流结束
                    async for chunk in resp.aiter_bytes():
                        if chunk:
                            elapsed = int((_time.time() - started) * 1000)
                            return True, f'连通正常（{model}，{elapsed}ms）'
                    return False, '上游未返回任何数据'
        return False, '所有候选路径均不可用'
    except Exception as exc:  # noqa: BLE001
        return False, f'请求异常: {exc}'


def _parse_error_body(raw: str, status: int) -> tuple[int | str, str]:
    """错误响应可能是 {code,msg} 信封，也可能是纯文本。"""
    try:
        env = json.loads(raw)
        if isinstance(env, dict):
            return env.get('code', status), str(env.get('msg') or env.get('message') or '')
    except Exception:  # noqa: BLE001
        pass
    return status, raw.strip()[:200]


# 已知业务码 -> 可读说明（来源：workbuddy2api 源码与实测）
#
# 注意键**必须带引号**：`11-128` 不加引号会被 Python 当成算术表达式
# （11 - 128 = -117），于是这个提示永远匹配不上，而且真正收到 "11-128" 时
# int() 还会抛异常被静默吞掉。上游这个错误码是不带引号的形态，必须按字符串存。
_CODE_HINTS: dict[int | str, str] = {
    0: '成功',
    10001: '今日已签到',
    11101: '上游不接受非流式请求（协议问题，非账号问题）',
    '11-128': '首条消息必须是 system（网关提示词未注入时客户端需自带）',
    12153: '会话已失效，需重新登录',
}


def _explain_code(code: int | str, msg: str = '') -> str:
    """把上游错误码翻译成人能看懂的一句话。

    码可能是数字（int/json number）也可能是带横线的字符串（"11-128"），
    所以查找要**先按原值、再按 int**，并对 int() 失败做好兜底。
    """
    key: int | str = code
    if key not in _CODE_HINTS:
        try:
            key = int(code)
        except (TypeError, ValueError):
            key = code  # 保持原样（如 "11-128"），按字符串查
    hint = _CODE_HINTS.get(key)
    parts = [f'上游返回 code={code}']
    if msg:
        parts.append(msg)
    if hint:
        parts.append(f'（{hint}）')
    return ' '.join(parts)


# ── 国际版：地区注册与 trial ──────────────────────────────
# 国际版新号必须先完成地区注册，否则聊天会报 14017「trial not activated」。
# 端口与流程对齐上游 scripts/global_region.py（从其反编译结果整理）：
#   GET  /auth/realms/copilot/overseas/user/register?userId=<uid>  查是否已注册
#   POST /console/login/account                                     提交地区
#   POST /billing/ide/trial                                         领一次性 trial
# 注意：**地区由使用者决定**，这里只提供提交能力，不替用户选国家。
INTERNATIONAL_REGIONS: tuple[tuple[str, str], ...] = (
    # (IOS2 代码, 地区中文名) —— 国际版官网给出的可选短名单
    ('HK', '中国香港'),
    ('MO', '中国澳门'),
    ('SG', '新加坡'),
    ('TH', '泰国'),
    ('PH', '菲律宾'),
    ('MY', '马来西亚'),
    ('ID', '印度尼西亚'),
)


async def registration_status(auth: dict) -> tuple[bool, str]:
    """查该国际版账号是否已完成地区注册。

    返回 (ok, 说明)。ok=True 表示**已完成**（无需再注册）。
    国内版无此步骤，直接返回已完成。
    """
    realm = realm_of(auth)
    if realm != GLOBAL:
        return True, '国内版无需地区注册'
    uid = str(auth.get('uid') or '')
    token = str(auth.get('access_token') or '')
    if not uid or not token:
        return False, '缺少 uid 或 accessToken，无法查询注册状态'
    url = f'{billing_base(realm)}/auth/realms/copilot/overseas/user/register'
    try:
        # 上游参照实现（scripts/global_region.py activate_region）明确带 X-User-Id，
        # 这里走 billing 域头（含身份头），保持一致
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            resp = await client.get(url, params={'userId': uid}, headers=_billing_hdr(realm, auth))
        code, data = _envelope(resp)
        if code == 200 or code == 0:
            return True, '地区注册已完成'
        if code == 500:
            return False, '尚未完成地区注册'
        # 「region required」也是未注册的一种表述
        text = str(data or '')
        if 'region required' in text.lower():
            return False, '尚未完成地区注册'
        return False, f'注册状态未知 code={code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'查询注册状态异常: {exc}'


async def submit_region(auth: dict, region_code: str) -> tuple[bool, str]:
    """提交国际版账号的地区。

    region_code 取 INTERNATIONAL_REGIONS 里的代码（如 'HK'）。
    提交成功后建议再调 registration_status 复核（上游脚本也是这么做的）。

    请求体形状**照上游参照实现 scripts/global_region.py**（其注释标注「实测」）：

        {"attributes": {"countryCode": [Code],        ← 数字地区码（如 810000）
                        "countryFullName": [EnName],  ← 英文全名（如 China Hong Kong）
                        "countryName": [IOS2]}}       ← 短码（如 HK）

    三个字段**取值各不相同**，此前我们错把三者都填成 IOS2，且少了 attributes
    外层——上游是按这三个字段落库的，填错会把地区归属写歪。
    这里按 IOS2 先查回真实条目，取不到就退回只用 IOS2（不至于提交非法值）。
    """
    realm = realm_of(auth)
    if realm != GLOBAL:
        return False, '国内版无需地区注册'
    code_upper = (region_code or '').strip().upper()
    valid = {c for c, _ in INTERNATIONAL_REGIONS}
    if code_upper not in valid:
        return False, f'不支持的地区代码 {region_code!r}（可选：{"、".join(sorted(valid))}）'
    token = str(auth.get('access_token') or '')
    if not token:
        return False, '缺少 accessToken'

    ios2, en_name, numeric = await _region_fields(code_upper)

    attrs = {
        'countryCode': [numeric],
        'countryFullName': [en_name],
        'countryName': [ios2],
    }
    body = {'attributes': attrs}
    try:
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            resp = await client.post(
                f'{billing_base(realm)}/console/login/account',
                json=body,
                headers=_billing_hdr(realm, auth),
            )
        code, data = _envelope(resp)
        if code == 0 or code == 200:
            return True, f'地区已提交（{code_upper}）'
        return False, f'提交地区失败 code={code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'提交地区异常: {exc}'


async def _region_fields(ios2: str) -> tuple[str, str, str]:
    """按 IOS2 短码查该地区的 (IOS2, EnName, Code)。

    地区列表来自上游接口 `/billing/area/get-country-code`（响应 data 是内嵌
    JSON 字符串）。查不到时退回 (IOS2, IOS2, IOS2)——提交非法值会被上游拒绝，
    好过替用户瞎猜一个。
    """
    fallback = (ios2, ios2, ios2)
    try:
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            resp = await client.post(
                f'{billing_base(GLOBAL)}/billing/area/get-country-code',
                json={'filterForbidden': 1},
                headers=_hdr(GLOBAL),
            )
        code, data = _envelope(resp)
        if code != 0 or data is None:
            return fallback
        # 响应 data 可能是内嵌 JSON 字符串（上游脚本明确处理了这一层）
        inner = json.loads(data) if isinstance(data, str) else data
        if not isinstance(inner, dict):
            return fallback
        lst = ((inner.get('data') or {}).get('list')
               if isinstance(inner.get('data'), dict) else None) or []
        for item in lst:
            if isinstance(item, dict) and str(item.get('IOS2') or '').upper() == ios2:
                return (
                    str(item.get('IOS2') or ios2),
                    str(item.get('EnName') or ios2),
                    str(item.get('Code') or ios2),
                )
    except Exception:  # noqa: BLE001
        pass
    return fallback


async def claim_trial(auth: dict) -> tuple[bool, str]:
    """领取国际版的一次性 trial 加油包。

    14051 = 已领过，按幂等处理（算成功）。
    """
    realm = realm_of(auth)
    if realm != GLOBAL:
        return False, '国内版无 trial 加油包'
    token = str(auth.get('access_token') or '')
    if not token:
        return False, '缺少 accessToken'
    try:
        async with config.http_client(config.TENCENT_TIMEOUT, connect=5) as client:
            resp = await client.post(
                f'{billing_base(realm)}/billing/ide/trial',
                json={},
                headers=_billing_hdr(realm, auth),
            )
        code, data = _envelope(resp)
        if code == 0:
            return True, 'trial 已领取'
        if code == 14051 or '14051' in str(data or ''):
            return True, 'trial 此前已领取（幂等）'
        return False, f'领取 trial 失败 code={code}'
    except Exception as exc:  # noqa: BLE001
        return False, f'领取 trial 异常: {exc}'
