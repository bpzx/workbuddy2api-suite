"""账号管理：列表、扫码添加、签到、测试、刷新、删除、重启上游。"""
from __future__ import annotations

import asyncio

from fastapi import APIRouter, Body, Depends, HTTPException, Query

from .. import config, db, security
from ..services import (
    credits as creditsvc, modelcatalog, reload, tasklog, tencent, wb2api,
)
from ..services.realm import realm_of, supports_checkin

router = APIRouter(prefix='/api', tags=['accounts'])


@router.get('/accounts')
async def list_accounts(user: dict = Depends(security.current_user)) -> dict:
    """账号列表：本地授权信息 + 上游运行时状态（含积分余额）。

    积分（credits）优先使用上游 /status 的值：它是上游调度时写入的快照，
    与账号可用性判定一致，开销也小。前端可用「刷新积分」触发实时查询。
    """
    accounts = wb2api.list_auth_accounts()
    status = await wb2api.get_status()
    wb2api.merge_pool_status(accounts, status)
    synced = sum(1 for a in accounts if a.get('credits') is not None)
    return {
        'total': len(accounts),
        'accounts': accounts,
        'pool_synced': synced,
        'pool_available': bool(status.get('connected')),
    }


@router.get('/status')
async def upstream_status(user: dict = Depends(security.current_user)) -> dict:
    return await wb2api.get_status()


@router.get('/models')
async def models(
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """上游可用模型列表。

    返回结构化对象而非裸数组，是为了带上 source：上游在动态拉取失败时会
    回退到**内置静态表**（老版本写死的，数量少得多），两者外观一样。前端
    据此如实标注来源，避免让人误以为是自己账号/配置有问题。

    realm 非空时只返回该版本的条目：上游清单里带 `cn:` / `global:` 前缀，
    不过滤会把两个版本的模型混在一起显示。
    """
    ok, data = await wb2api.get_models()
    if not ok:
        raise HTTPException(status_code=502, detail=str(data))
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict) and isinstance(data.get('data'), list):
        items = data['data']
    else:
        items = []
    source_items = items
    if realm:
        r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
        items = [m for m in items if modelcatalog._belongs(m, r)]
    return {
        'models': items,
        'source': wb2api.models_source(source_items),
        'count': len(items),
    }


@router.post('/auth/start')
async def auth_start(
    realm: str | None = Query(
        None,
        description='国内版 cn / 国际版 global；也可用 JSON body 传 {"realm": "..."}',
    ),
    body: dict | None = Body(None),
    user: dict = Depends(security.require_admin),
) -> dict:
    """发起扫码登录。realm 决定国内版 / 国际版端点（缺省国内版）。

    **参数来源要同时接受 body 与 query**：前端 post() 把参数放在 JSON body 里，
    而早先这里声明的是普通标量参数 —— FastAPI 对标量默认按 **query** 解析，
    body 里的 realm 被静默忽略、恒回落到默认值 'cn'。后果是：切到「国际版」
    点添加账号，拿到的仍是国内版二维码（`copilot.tencent.com`），且**不报错**。
    现同时接受两处，body 优先（与前端一致），query 保留兼容旧调用方。
    """
    raw = realm
    if isinstance(body, dict) and body.get('realm') is not None:
        raw = str(body.get('realm'))
    r = 'global' if str(raw or '').strip().lower() == 'global' else 'cn'
    try:
        return await tencent.start_login(r)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


@router.get('/auth/poll')
async def auth_poll(
    state: str,
    realm: str | None = None,
    region: str | None = None,
    user: dict = Depends(security.require_admin),
) -> dict:
    """轮询扫码结果；成功则落盘并触发上游重载。

    region：国际版账号的地区代码（如 HK）。国际版新号必须先完成地区注册，
    否则聊天会报 14017；地区由用户在弹窗里选，这里只负责提交，不替他决定。
    """
    if not state:
        return {'status': 'invalid'}

    r = None
    if realm is not None:
        r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    result = await tencent.poll_login(state, r)
    if result.get('status') != 'ready':
        return result

    realm_of_result = result.get('realm') or 'cn'

    # 探测 dict：billing 域身份头（X-User-Id / X-Domain 等）需要这些字段，
    # 与 _auth_dict 同构；device_token 此刻还没有（落盘时由外部写入）
    auth_probe = {
        'access_token': result['access_token'],
        'uid': result['uid'],
        'enterprise_id': result.get('enterprise_id', ''),
        'realm': realm_of_result,
        'domain': result.get('domain', ''),
    }

    # 国际版：先做地区注册（未注册会导致后续聊天报 14017）
    region_msg = ''
    if realm_of_result == 'global':
        if region:
            ok_r, region_msg = await tencent.submit_region(auth_probe, region)
        else:
            done, why = await tencent.registration_status(auth_probe)
            if not done:
                region_msg = '尚未完成地区注册，请在弹窗中选择地区后重试（否则聊天会报 14017）'
        # 注册完成后再领一次性 trial（失败不阻断，已领过按幂等）
        if region_msg == '' or region_msg.startswith('地区已提交'):
            ok_t, trial_msg = await tencent.claim_trial(auth_probe)
            if ok_t:
                region_msg = f'{region_msg}；{trial_msg}'.strip('；')

    # 自动签到（幂等，不阻断落盘）。国际版无签到体系，checkin 会直接跳过
    code, message = await tencent.checkin(auth_probe, realm_of_result)
    db.add_checkin_log(
        str(result.get('uid', '')), str(result.get('nickname', '')),
        'add', code in (0, 10001), code, message,
    )

    creditsvc.invalidate(str(result.get('uid', '')))
    filename, existed = tencent.write_auth_file(result)

    # 自动重载上游以加载新账号（后台合并执行，不阻塞本次响应）
    reload.request_restart()

    return {
        'status': 'success',
        'uid': result['uid'],
        'nickname': result['nickname'],
        'realm': realm_of_result,
        'updated': existed,
        'file': filename,
        'region_note': region_msg,
    }


def _auth_dict(raw: dict) -> dict:
    """把授权文件内容整理成探测 / 查询积分所需的字段（含版本，供端点分派）。

    device_token 是**每号**的设备风控 token（auth 文件顶层 device_token 键，
    与 workbuddy2api 的解析位置一致）；缺省留空，由 realm.device_token_for
    回退到上游 config 的全局值或文件。
    """
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    return {
        'access_token': auth.get('accessToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
    }


def _load(filename: str) -> dict:
    try:
        return wb2api.read_account_file(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail='账号文件不存在') from exc


@router.post('/accounts/{filename}/checkin')
async def account_checkin(filename: str, user: dict = Depends(security.require_admin)) -> dict:
    raw = _load(filename)
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    token = auth.get('accessToken', '')
    uid = str(acct.get('uid', ''))
    nickname = str(acct.get('nickname', ''))

    if not token:
        db.add_checkin_log(uid, nickname, 'manual', False, None, '该账号无有效 accessToken')
        return {'code': -1, 'message': '该账号无有效 accessToken'}

    # 传完整 auth dict：billing 域要带 X-User-Id 等身份头（对齐上游 BillingHeaders）
    code, message = await tencent.checkin({
        'access_token': token,
        'uid': uid,
        'enterprise_id': str(acct.get('enterpriseId') or ''),
        'domain': str(auth.get('domain') or ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
    }, realm_of(auth))
    # 0 = 签到成功；10001 = 今日已签到，同样视为成功；
    # -2 = 该版本无签到体系（国际版），不是失败，也不该记成失败
    ok = code in (0, 10001)
    db.add_checkin_log(uid, nickname, 'manual', ok, code, message)

    # 签到后顺带查实时积分：上游只在它自己的定时任务里刷新 credits，
    # 手动签到不会带动它更新，所以这里主动查一次返回给前端。
    credits: int | float | None = None
    if ok:
        # 签到会改变余额，先失效缓存再查实时值
        creditsvc.invalidate(uid)
        _, credits, _, _, _ = await creditsvc.get_credits(
            _auth_dict(raw), nickname=str(acct.get('nickname') or ''),
        )

    return {'code': code, 'message': message, 'credits': credits}


@router.get('/accounts/{filename}/credits')
async def account_credits(
    filename: str,
    force: bool = False,
    user: dict = Depends(security.require_admin),
) -> dict:
    """查询单个账号的实时积分余额（直接向腾讯查询，带 60s 缓存）。

    force=true 可绕过缓存强制查询。
    """
    raw = _load(filename)
    acct = raw.get('account') or {}
    ok, value, message, cached, age = await creditsvc.get_credits(
        _auth_dict(raw), force=force, nickname=str(acct.get('nickname') or ''),
    )
    return {'ok': ok, 'credits': value, 'message': message, 'cached': cached, 'cache_age': age}


@router.post('/accounts/refresh-credits')
async def refresh_all_credits(
    force: bool = True,
    user: dict = Depends(security.require_admin),
) -> dict:
    """并发查询所有账号的积分，返回 {uid: credits} 与每条是否来自缓存。

    上游 /status 的 credits 只在它定时任务时更新，可能滞后数小时；
    本接口直接向腾讯查询。force=true（默认）用于「刷新积分」按钮，
    强制绕过 60 秒缓存；force=false 用于页面加载，命中缓存时不重复请求腾讯。
    无论哪种，都回传 cached / cache_age，前端据此标注「实时 / 缓存」。
    """
    accounts = wb2api.list_auth_accounts()

    async def one(acc: dict) -> tuple[str, int | float | None, str, bool, int | None]:
        try:
            raw = wb2api.read_account_file(acc['file'])
        except Exception as exc:  # noqa: BLE001
            return acc['uid'], None, f'读取失败: {exc}', False, None
        ok, value, message, cached, age = await creditsvc.get_credits(
            _auth_dict(raw), force=force, nickname=str(acc.get('nickname') or ''),
        )
        return acc['uid'], value if ok else None, message, cached, age

    results = await asyncio.gather(*(one(a) for a in accounts)) if accounts else []

    credits_map: dict[str, int | float | None] = {}
    meta: dict[str, dict] = {}
    failed: list[str] = []
    for uid, credits, message, cached, age in results:
        credits_map[uid] = credits
        meta[uid] = {'cached': cached, 'cache_age': age, 'message': message}
        if credits is None:
            failed.append(f'{uid[:12]}: {message}')

    return {
        'total': len(accounts),
        'succeeded': len(accounts) - len(failed),
        'credits': credits_map,
        'meta': meta,
        'failed': failed,
    }


def _checkin_semaphore() -> asyncio.Semaphore:
    """限制签到并发数：太高容易触发腾讯风控，太低又会拖到前端超时。"""
    return asyncio.Semaphore(config.CHECKIN_CONCURRENCY)


@router.post('/accounts/checkin-all')
async def checkin_all(user: dict = Depends(security.require_admin)) -> dict:
    """对所有账号执行一次签到，并逐条记录结果。

    上游的自动签到只在失败时打日志、成功静默，且没有可触发的 HTTP 接口；
    这里用管理端自己的签到实现补齐「可手动触发 + 可追溯」。

    并发执行（上限见 WB_CHECKIN_CONCURRENCY）：逐个 await 时，
    几十个账号叠加腾讯 RPC 耗时会超过前端 60 秒超时——前端报失败、
    后端却还在跑，用户容易重复点击。并发后总耗时约等于最慢的单个账号。
    """
    accounts = wb2api.list_auth_accounts()
    sem = _checkin_semaphore()

    async def one(acc: dict) -> dict:
        filename = acc['file']
        uid = acc.get('uid', '')
        nickname = acc.get('nickname', '')
        try:
            raw = wb2api.read_account_file(filename)
            auth = raw.get('auth') or {}
            token = auth.get('accessToken', '')
        except Exception as exc:  # noqa: BLE001
            msg = f'读取失败: {exc}'
            db.add_checkin_log(uid, nickname, 'manual-batch', False, None, msg)
            return {'nickname': nickname, 'ok': False, 'message': msg}

        if not token:
            msg = '无有效 accessToken'
            db.add_checkin_log(uid, nickname, 'manual-batch', False, None, msg)
            return {'nickname': nickname, 'ok': False, 'message': msg}

        # 国际版没有签到体系：直接跳过（不记失败，也不打上游请求）
        if not supports_checkin(realm_of(auth)):
            msg = '国际版无签到体系，已跳过'
            db.add_checkin_log(uid, nickname, 'manual-batch', False, -2, msg)
            return {'nickname': nickname, 'ok': False, 'code': -2, 'message': msg}

        async with sem:
            # 传完整 auth dict：billing 域要带 X-User-Id 等身份头
            code, message = await tencent.checkin({
                'access_token': token,
                'uid': str((raw.get('account') or {}).get('uid') or uid),
                'enterprise_id': str((raw.get('account') or {}).get('enterpriseId') or ''),
                'domain': str(auth.get('domain') or ''),
                'realm': auth.get('realm'),
                'device_token': str(raw.get('device_token') or ''),
            }, realm_of(auth))
        ok = code in (0, 10001)
        db.add_checkin_log(uid, nickname, 'manual-batch', ok, code, message)
        return {'nickname': nickname, 'ok': ok, 'code': code, 'message': message}

    results = await asyncio.gather(*(one(a) for a in accounts)) if accounts else []
    succeeded = sum(1 for r in results if r['ok'])
    return {'total': len(results), 'succeeded': succeeded, 'results': list(results)}


@router.get('/checkin-logs')
def checkin_logs(
    limit: int = 200,
    uid: str | None = None,
    offset: int = 0,
    days: int | None = None,
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """签到记录（分页）：**本端触发的 + 上游自动签到的**统一视图。

    为什么要合并：签到记录原先只写本端触发的（手动 / 批量 / 添加账号），而上游
    定时签到的结果由日志采集器写进 task_logs。于是「自动签到」在签到记录里
    永远看不到——用户反馈的「自动签到不显示」就是这个。

    上游对签到成功是静默的（只打一行 `checkin done: total=.. ok=..` 汇总 + 失败明细），
    所以自动签到侧提供的是**每轮汇总/异常行**，不是逐账号成功明细；这一点在
    界面上如实标注，不假装有更细的数据。

    两张表各自条数都不大，按 ts 归并后在 Python 侧分页，避免为跨表分页写
    UNION + 双重 LIMIT 的复杂 SQL。

    候选量必须覆盖到「当前页的末尾」，不能固定取前 N 条：合并是按时间排序的，
    若只取各表最近的 500 条，落在 500 名之后的记录会永远翻不到，而 total 又是
    真实全量——界面会显示「共 810 条」却翻不出后面 300 条。因此按 offset+limit
    取候选（各表都取这么多，够覆盖最坏情况：全部记录都来自同一张表）。
    """
    start = max(0, int(offset))
    size = min(500, max(1, int(limit)))
    # 各表都取到 start+size，保证合并后第 start..start+size 条一定在候选里
    want = start + size

    local = db.list_checkin_logs(limit=want, uid=uid, offset=0, days=days)
    local_total = db.count_checkin_logs(uid=uid, days=days)
    local_items = [{**r, 'auto': False} for r in local]

    # 上游自动签到（采集器落库的 kind='checkin'）
    auto_rows = db.list_task_logs(limit=want, uid=uid, kind='checkin', offset=0, days=days)
    auto_total = db.count_task_logs(uid=uid, kind='checkin', days=days)
    auto_items = [
        {
            # 与 checkin_logs 的 id 空间不同，加偏移前缀避免前端 key 冲突
            'id': 10**9 + int(r['id']),
            'ts': r['ts'],
            'uid': r['uid'],
            'nickname': r.get('nickname') or '',
            'source': 'auto',
            'kind': 'checkin',
            # 汇总行不代表单个账号成功，success 仅用于界面着色，不参与判定
            'success': r.get('level') != 'error',
            'code': None,
            'message': r.get('message_cn') or r.get('message') or '',
            'auto': True,
        }
        for r in auto_rows
    ]

    merged = sorted(local_items + auto_items, key=lambda x: x['ts'], reverse=True)

    # 按版本过滤（realm 为空则不过滤，保持既有调用行为）
    realm_map = _realm_uid_filter(realm)
    if realm_map is not None:
        merged = [it for it in merged if _uid_matches_realm(str(it.get('uid') or ''), realm_map, realm)]

    # 自动侧的行也要解析昵称（上游只带 uid 前 8 位）；本端的已有昵称
    resolve_nick = _nickname_resolver()
    for it in merged:
        if not it.get('nickname'):
            it['nickname'] = resolve_nick(str(it.get('uid') or ''))

    start = max(0, int(offset))
    end = start + min(500, max(1, int(limit)))
    return {
        'items': merged[start:end],
        'total': local_total + auto_total,
        # 分别给出，便于界面说明「本端 N 条 / 自动 M 条」
        'local_total': local_total,
        'auto_total': auto_total,
    }


@router.post('/checkin-logs/clear')
def clear_checkin_logs(user: dict = Depends(security.require_admin)) -> dict:
    db.clear_checkin_logs()
    return {'ok': True}


def _nickname_resolver() -> object:
    """构造 uid → 昵称的解析函数。

    上游 2026-09-12 起把日志里的 uid 截成前 8 位，而账号表里是完整 uuid，
    无法直接相等匹配，因此按前缀解析；前缀命中多个账号（理论可能）时不猜，
    保留 uid 原文。签到记录与任务记录都用它，避免两处口径不一致。
    """
    nick_by_uid: dict[str, str] = {}
    nick_by_prefix: dict[str, str] = {}
    ambiguous: set[str] = set()
    try:
        for acc in wb2api.list_auth_accounts():
            auid = str(acc.get('uid') or '')
            nick = str(acc.get('nickname') or '')
            if not auid or not nick:
                continue
            nick_by_uid[auid] = nick
            prefix = auid[:8]
            if prefix in nick_by_prefix and nick_by_prefix[prefix] != nick:
                ambiguous.add(prefix)
            else:
                nick_by_prefix[prefix] = nick
    except Exception:  # noqa: BLE001
        pass

    def resolve(uid: str) -> str:
        if not uid:
            return ''
        if uid in nick_by_uid:
            return nick_by_uid[uid]
        prefix = uid[:8]
        return '' if prefix in ambiguous else nick_by_prefix.get(prefix, '')

    return resolve


def _realm_uid_filter(realm: str | None) -> dict[str, str] | None:
    """构造「哪些 uid 属于该版本」的映射；realm 为空返回 None（不过滤）。

    日志表里只有 uid，没有 realm 列（本方案选择不改数据库），所以按
    账号表把 uid 映射到版本再筛。上游日志里的 uid 是前 8 位，这里同时
    支持完整 uid 与前缀两种匹配。

    注意：账号已被删除时其历史日志无从判断版本，一律按「不属于任何版本」
    处理——宁可少显示，也不要把国际版账号的日志挂到国内版视图下。
    """
    if not realm:
        return None
    r = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    try:
        accounts = wb2api.list_auth_accounts()
    except Exception:  # noqa: BLE001
        return {}
    return {
        str(a.get('uid')): str(a.get('realm') or 'cn')
        for a in accounts
        if a.get('uid')
    }


def _uid_matches_realm(uid: str, realm_map: dict[str, str] | None, realm: str) -> bool:
    """该日志行的 uid 是否属于指定版本（realm_map 为 None 时恒 True）。"""
    if realm_map is None:
        return True
    u = str(uid or '')
    if not u:
        return False
    want = 'global' if str(realm).strip().lower() == 'global' else 'cn'
    full = realm_map.get(u)
    if full is not None:
        return full == want
    # 上游只给前 8 位：前缀唯一命中才算，命中多个视为不确定、不展示
    hits = {v for k, v in realm_map.items() if k[:8] == u[:8]}
    return len(hits) == 1 and hits.pop() == want


@router.get('/task-logs')
def task_logs(
    limit: int = 200,
    offset: int = 0,
    uid: str | None = None,
    kind: str | None = None,
    days: int | None = None,
    realm: str | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    """上游自动任务留痕（猫猫旅行 / 活跃上报 / 自动签到 / 保活）。

    上游把这些结果打在容器日志里，容器重建即丢失；本接口读取的是
    后台采集器解析后落库的记录，因此能长期保留并统计积分收益。

    分页返回：列表只取当前页，`total` 为**当前筛选下**的总数，
    概览 `stats` 也按同一时间范围统计，保证数字与列表一致。
    """
    logs = db.list_task_logs(limit=limit, uid=uid, kind=kind, offset=offset, days=days)

    # 按版本过滤（realm 为空则不过滤）。日志表无 realm 列，按账号表映射 uid
    realm_map = _realm_uid_filter(realm)
    if realm_map is not None:
        logs = [r for r in logs
                if _uid_matches_realm(str(r.get('uid') or ''), realm_map, realm)]

    resolve_nick = _nickname_resolver()
    for row in logs:
        # 结果文案中文化：数据库留英文原文（排查要看上游原话），
        # 接口额外给出 message_cn 供界面展示
        row['message_cn'] = tasklog.translate_message(row.get('message', ''))
        row['nickname'] = resolve_nick(str(row.get('uid') or ''))

    # 统计也要按同一口径：不过滤时用数据库聚合（快），过滤时按筛后的行算，
    # 否则会出现「徽标说 62 条、列表只有 3 条」的矛盾
    stats = db.task_log_stats(days=days)
    total = db.count_task_logs(uid=uid, kind=kind, days=days)
    if realm_map is not None:
        all_rows = db.list_task_logs(limit=2000, uid=uid, kind=kind, offset=0, days=days)
        kept = [r for r in all_rows
                if _uid_matches_realm(str(r.get('uid') or ''), realm_map, realm)]
        total = len(kept)
        by_kind: dict[str, dict] = {}
        for r in kept:
            k = str(r.get('kind') or '')
            slot = by_kind.setdefault(k, {'count': 0, 'credits': 0})
            slot['count'] += 1
            slot['credits'] += int(r.get('credits') or 0)
        stats = {
            'by_kind': by_kind,
            'total': len(kept),
            'total_credits': sum(v['credits'] for v in by_kind.values()),
        }

    return {
        'logs': logs,
        'total': total,
        'stats': stats,
        'kinds': tasklog.KIND_LABELS,
        'collector': tasklog.state(),
    }


@router.post('/task-logs/collect')
async def collect_task_logs(user: dict = Depends(security.require_admin)) -> dict:
    """立即采集一次（不等后台轮询），便于刚跑完任务就看结果。"""
    parsed, added = await tasklog._collect_once()
    return {'ok': True, 'parsed': parsed, 'added': added}


@router.post('/task-logs/clear')
def clear_task_logs(user: dict = Depends(security.require_admin)) -> dict:
    db.clear_task_logs()
    return {'ok': True}


@router.get('/upstream/logs')
def upstream_logs(limit: int = 200, user: dict = Depends(security.current_user)) -> dict:
    """上游容器日志中与自动任务相关的行（原始日志）。

    上游只在失败时打日志、成功大多静默（旅行与活跃上报除外，它们会记录
    积分与服务端判据）。结构化、可长期保留的记录见 /api/task-logs。
    """
    lines = wb2api.read_container_logs(limit=limit)
    keywords = ('checkin', 'keepalive', 'user-resource', 'travel', 'activity')
    interesting = [
        # 去掉 docker --timestamps 前缀（上游自己已带秒级时间，重复显示反而难读）
        tasklog.strip_docker_ts(ln)
        for ln in lines
        if any(k in ln.lower() for k in keywords)
    ]
    return {'available': lines != [], 'lines': interesting, 'total': len(lines)}


@router.post('/accounts/{filename}/test')
async def account_test(filename: str, user: dict = Depends(security.require_admin)) -> dict:
    raw = _load(filename)
    acct = raw.get('account') or {}
    auth = raw.get('auth') or {}
    # 探测需要 uid / enterpriseId / domain 以复刻上游请求头，
    # device_token 用于 X-Device-Token（上游 chat 域同样注入）
    ok, message = await tencent.probe_account({
        'access_token': auth.get('accessToken', ''),
        'uid': acct.get('uid', ''),
        'enterprise_id': acct.get('enterpriseId', ''),
        'domain': auth.get('domain', ''),
        'realm': auth.get('realm'),
        'device_token': str(raw.get('device_token') or ''),
    })
    return {'ok': ok, 'message': message}


@router.post('/accounts/{filename}/refresh')
async def account_refresh(filename: str, user: dict = Depends(security.require_admin)) -> dict:
    raw = _load(filename)
    if not (raw.get('auth') or {}).get('accessToken'):
        return {'ok': False, 'message': '该账号无有效 accessToken'}
    ok, message = await reload.restart_now()
    return {'ok': ok, 'message': '已触发上游重载以刷新 Token' if ok else message}


@router.delete('/accounts/{filename}')
async def account_delete(filename: str, user: dict = Depends(security.require_admin)) -> dict:
    try:
        removed = wb2api.delete_auth_account(filename)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not removed:
        raise HTTPException(status_code=404, detail='账号文件不存在')
    reload.request_restart()
    return {'success': True}


@router.post('/restart')
async def restart(user: dict = Depends(security.require_admin)) -> dict:
    ok, message = await reload.restart_now()
    return {'ok': ok, 'message': message}
