"""workbuddy2api 上游交互：账号文件、状态、模型、容器重启。"""
from __future__ import annotations

import asyncio
import base64
import ipaddress
import json
import re
import time
from pathlib import Path

from .. import config
from . import realm as _realm
from .realm import realm_of, supports_checkin


def _safe_file(filename: str) -> Path:
    """把请求里的文件名解析为 auths 目录下的真实路径，非法即抛错。

    穿越防线（`/`、反斜杠、`..`）是根本；此外只接受 `workbuddy-*.json`
    这一种形态，避免越权读到目录里的其他文件（例如隐藏文件或临时文件）。
    """
    if '/' in filename or '\\' in filename or '..' in filename:
        raise ValueError('非法的文件名')
    if '\x00' in filename:
        raise ValueError('非法的文件名')
    # 白名单形态：账号文件一律是 workbuddy-<uid>.json
    if not re.fullmatch(r'workbuddy-[0-9A-Za-z_-]{1,80}\.json', filename):
        raise ValueError('非法的文件名')
    target = config.AUTH_DIR / filename
    if target.suffix != '.json':
        raise ValueError('非法的文件名')
    return target


def read_account_file(filename: str) -> dict:
    return json.loads(_safe_file(filename).read_text(encoding='utf-8'))


def _jwt_times(access_token: str) -> tuple[int, int] | None:
    """从 accessToken（JWT）里读出 (iat, exp)。解不出返回 None。

    纯本地 base64 解码，不发网络请求；仅用于展示，绝不参与鉴权判断。
    """
    try:
        parts = (access_token or '').split('.')
        if len(parts) < 2:
            return None
        payload = parts[1]
        payload += '=' * (-len(payload) % 4)  # 补齐 base64url padding
        data = json.loads(base64.urlsafe_b64decode(payload))
        iat = int(data.get('iat') or 0)
        exp = int(data.get('exp') or 0)
        if iat > 0 and exp > iat:
            return iat, exp
    except Exception:  # noqa: BLE001
        return None
    return None


def token_ttl_seconds(access_token: str) -> int | None:
    """该令牌签发的总时长（exp - iat），单位秒。

    用途：界面上的「有效期进度条」需要一个「满格 = 多久」的基准。
    auth 文件里只有 expiresAt，没有签发起始时间，光看文件算不出比例；
    而 JWT 载荷里同时有 iat 与 exp，且只是本地解码、不发网络请求。

    为什么不另存一份 expiresIn：JWT 的 exp - iat 就是该令牌自身的真实寿命，
    且随令牌一起走——刷新换发新令牌时它自动更新，也不会被上游写回时丢掉。
    另存字段反而可能与令牌不一致或过期。

    纯展示用途：解不出来就返回 None，调用方回退到保守的默认窗口，
    绝不影响任何鉴权判断。
    """
    times = _jwt_times(access_token)
    return times[1] - times[0] if times else None


def token_issued_at(access_token: str) -> int | None:
    """令牌签发时间（JWT iat，Unix 秒）。解不出返回 None。

    为什么有用：界面显示的「有效期」是**剩余时间**，刷新会把它重新拉满，
    因此单看剩余天数分不清一个账号是「刚被保活续期」还是「从没刷新过、
    一直用着当初扫码签发的长令牌」。后者才是保活没覆盖到、到期会掉线的
    隐患账号。刷新会换发新令牌，故 iat 近似等于「最近一次刷新时间」。
    """
    times = _jwt_times(access_token)
    return times[0] if times else None


def list_auth_accounts() -> list[dict]:
    """读取 auths/ 目录下的本地账号（与 /status 的运行时状态互补）。"""
    out: list[dict] = []
    if not config.AUTH_DIR.is_dir():
        return out
    now = time.time()
    for path in sorted(config.AUTH_DIR.glob('workbuddy-*.json')):
        try:
            raw = json.loads(path.read_text(encoding='utf-8'))
        except Exception:
            continue
        acct = raw.get('account', {}) or {}
        auth = raw.get('auth', {}) or {}
        exp = int(auth.get('expiresAt', 0) or 0)
        token = str(auth.get('accessToken') or '')

        # 总时长：优先用 JWT 自身的 iat→exp（最权威）；JWT 解不出时退回用
        # 文件修改时间推算。上游刷新 token 后会原子写回该文件，因此 mtime
        # 近似等于「最近一次写入/刷新」时刻，于是 exp - mtime ≈ 本次有效期。
        # 这比原来那种「解不出就假定 60 天」的猜测更贴近真实：一个 7 天的
        # 令牌若按 60 天算，进度条只会显示 12%，看着像快过期，属于误报。
        ttl = token_ttl_seconds(token)
        issued = token_issued_at(token)
        try:
            mtime = int(path.stat().st_mtime)
        except OSError:
            mtime = 0
        if ttl is None and exp > mtime > 0:
            ttl = exp - mtime
        if issued is None and 0 < mtime < exp:
            issued = mtime

        out.append(
            {
                'file': path.name,
                'uid': str(acct.get('uid', '')),
                'nickname': acct.get('nickname') or '未命名',
                'enterprise_id': acct.get('enterpriseId', '') or '',
                'expires_at': exp,
                'is_expired': now >= exp,
                'remain_seconds': max(0, int(exp - now)),
                # 该令牌签发的总时长（供进度条按真实比例展示），解不出为 None
                'ttl_seconds': ttl,
                # 令牌签发时间 ≈ 最近一次刷新时间（刷新会换发新令牌），解不出为 None
                'issued_at': issued,
                # 账号所属版本（cn / global）。上游据此路由到不同上游，
                # 管理端据此做视图过滤与端点分派；存量文件无 realm 字段时
                # 按 domain 回退，domain 也为空则判 cn（行为与升级前一致）
                'realm': realm_of({'realm': raw.get('realm') or auth.get('realm'),
                                   'domain': auth.get('domain')}),
                'domain': str(auth.get('domain') or ''),
                # 该版本是否支持签到体系（国际版没有，调用方据此跳过而不是打 4xx）
                'checkin_supported': supports_checkin(
                    realm_of({'realm': raw.get('realm') or auth.get('realm'),
                              'domain': auth.get('domain')})),
                'source': 'file',
            }
        )
    return out


def merge_pool_status(accounts: list[dict], status: dict) -> list[dict]:
    """把 /status 的运行时状态合并进账号列表（含积分余额）。

    credits：账号当前可花费积分余额，由上游聚合所有套餐的
    CycleCapacityRemain 得出（见 upstream.UserResource）。
    """
    pool: dict[str, dict] = {}
    for item in (status or {}).get('accounts') or []:
        if isinstance(item, dict) and item.get('uid'):
            pool[str(item['uid'])] = item

    for a in accounts:
        p = pool.get(a['uid'])
        if not p:
            # 上游未返回该账号（可能刚添加尚未重载），保持字段为 None
            a.setdefault('credits', None)
            continue
        credits = p.get('credits')
        a['credits'] = int(credits) if isinstance(credits, (int, float)) else None
        a['cooling'] = bool(p.get('cooling'))
        # 冷却剩余秒数：上游状态机给的是权威值（可能是它解析出的「上游重置时刻」，
        # 也可能是无时间文案时的有界退避）。展示出来，用户就知道还要等多久，
        # 而不是只看到一个「冷却中」干等。
        _remain = p.get('cool_remaining_sec')
        a['cool_remaining_sec'] = int(_remain) if isinstance(_remain, (int, float)) and _remain > 0 else None
        # 被限流的模型清单（上游 issue #36 的限额台账）：多模型限流时，
        # 账号级 until 不等于各模型各自的恢复时刻，需分别展示。
        # 只透传列表形态（前端直接 .map()）；异常类型归空列表，避免整页崩掉。
        _rl = p.get('rate_limited_models')
        a['rate_limited_models'] = _rl if isinstance(_rl, list) else []
        a['disabled'] = bool(p.get('disabled'))
        # 禁用原因：上游对 11140（request illegal，需重新 OAuth 登录）会**硬禁用**
        # 账号（到期也不自愈），对 14017（试用未激活）只软冷却。展示原因才能
        # 让用户知道该去重新登录，而不是干等冷却。
        a['disabled_reason'] = str(p.get('disabled_reason') or '')
        a['success_count'] = p.get('success_count')
        a['in_flight'] = p.get('in_flight')
        a['breaker_fails'] = p.get('breaker_fails')
        a['last_success'] = p.get('last_success')
    return accounts


def delete_auth_account(filename: str) -> bool:
    target = _safe_file(filename)
    if target.exists():
        target.unlink()
        return True
    return False


ASYNC_HEADERS = {'Content-Type': 'application/json'}


def _err_text(exc: Exception) -> str:
    """异常文本可能为空（如 AssertionError），补上类型名便于排查。"""
    detail = str(exc).strip()
    return f'{type(exc).__name__}: {detail}' if detail else type(exc).__name__


def _auth_headers() -> dict:
    key = config.upstream_api_key()
    return {'Authorization': f'Bearer {key}'} if key else {}


async def get_status() -> dict:
    # 连接超时短一些：上游未运行时快速失败，避免拖慢管理端页面
    try:
        async with config.http_client(10, connect=3) as client:
            resp = await client.get(f'{config.WB2API_BASE}/status', headers=_auth_headers())
        if resp.status_code >= 400:
            return {'connected': False, 'error': f'上游返回 {resp.status_code}'}
        data = resp.json()
        data['connected'] = True
        return data
    except Exception as exc:  # noqa: BLE001
        return {'connected': False, 'error': _err_text(exc)}


async def get_models() -> tuple[bool, list | dict]:
    try:
        async with config.http_client(15, connect=3) as client:
            resp = await client.get(f'{config.WB2API_BASE}/v1/models', headers=_auth_headers())
        if resp.status_code >= 400:
            return False, {'error': f'上游返回 {resp.status_code}'}
        body = resp.json()
        return True, body.get('data', body)
    except Exception as exc:  # noqa: BLE001
        return False, {'error': _err_text(exc)}


def models_source(items: list) -> str:
    """判断这份模型列表来自上游「动态拉取」还是「内置静态表」。

    为什么需要区分：上游 `/v1/models` 优先用池里随机一个健康账号去动态拉取
    （成功则缓存 1 小时），**拉取失败就回退到编译进二进制的静态表**，且失败后
    还有 5 分钟负缓存。两者外观一样，但静态表是老版本写死的、数量少得多——
    界面若一律标「来自上游实时列表」，用户会以为账号/配置有问题，实际是上游
    在走回退。实测有部署只显示 6 个模型，且顺序与旧版静态表完全一致。

    判据：动态条目会带 `max_output_tokens`（上游 modelList 里动态分支才写这个
    键），静态表条目只有 id/object/created/owned_by/context_length。
    这是上游内部实现细节，故只作展示提示；判不出来返回 'unknown'，前端
    据此回退到中性文案，绝不因此报错。
    """
    if not isinstance(items, list) or not items:
        return 'unknown'
    dicts = [x for x in items if isinstance(x, dict)]
    if not dicts:
        return 'unknown'
    if any('max_output_tokens' in x for x in dicts):
        return 'dynamic'
    # 没有该键且条目结构齐全 → 基本可判定为静态回退表
    if all('id' in x for x in dicts):
        return 'static'
    return 'unknown'


async def restart_container() -> tuple[bool, str]:
    name = config.WB2API_CONTAINER
    try:
        proc = await asyncio.create_subprocess_exec(
            'docker', 'restart', name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, err = await proc.communicate()
        if proc.returncode == 0:
            return True, f'容器 {name} 已重启'
        return False, (err.decode(errors='ignore').strip() or f'docker 退出码 {proc.returncode}')
    except FileNotFoundError:
        return False, '未找到 docker 命令'
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def read_container_logs(limit: int = 200, timestamps: bool = True) -> list[str]:
    """读取上游容器日志（同步、失败返回空列表）。

    默认带 `--timestamps`：docker 会在每行前面加上精确到纳秒的 RFC3339 时间，
    自动任务日志据此获得准确时间并据此去重（上游自己的 log 前缀精度只到秒）。
    """
    import subprocess

    cmd = ['docker', 'logs', '--tail', str(max(1, min(5000, limit)))]
    if timestamps:
        cmd.append('--timestamps')
    cmd.append(config.WB2API_CONTAINER)
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=25)
        # docker logs 把应用日志写到 stderr
        raw = (proc.stdout or '') + (proc.stderr or '')
        return [ln for ln in raw.splitlines() if ln.strip()]
    except Exception:  # noqa: BLE001
        return []


def _mask(v: str) -> str:
    if not v:
        return ''
    return v[:6] + '*' * max(0, len(v) - 10) + v[-4:] if len(v) > 12 else '******'


def load_upstream_config() -> dict:
    """读取 workbuddy2api 的 config.json，敏感字段一律掩码。

    读不到时返回 available=False 并附带原因，供前端明确提示并禁止保存，
    避免把空配置写回真实文件。

    注意：不返回原始配置对象。原始配置含上游 API Key、Upstash token 与
    设备风控 token 的明文，前端并不需要它们，不应通过接口下发。
    """
    path = config.UPSTREAM_CONFIG
    cfg: dict | None = None
    error: str | None = None

    if not path.is_file():
        error = f'未找到上游配置文件 {path}'
    else:
        try:
            loaded = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(loaded, dict):
                cfg = loaded
            else:
                error = f'上游配置文件不是合法的 JSON 对象: {path}'
        except Exception as exc:  # noqa: BLE001
            error = f'上游配置文件解析失败: {exc}'

    if cfg is None:
        return {
            'available': False,
            'config_path': str(path),
            'auth_dir': str(config.AUTH_DIR),
            'error': error or '无法读取上游配置',
        }

    view = dict(cfg)
    if 'api_key' in view:
        view['api_key_masked'] = _mask(str(view.pop('api_key') or ''))
    # 账号列表实际读取的是管理端自己的 AUTH_DIR，以此为准；上游若声明了不同目录则一并暴露
    upstream_auth_dir = cfg.get('auth_dir')
    view['auth_dir'] = str(config.AUTH_DIR)
    if upstream_auth_dir and str(upstream_auth_dir) != str(config.AUTH_DIR):
        view['upstream_auth_dir'] = str(upstream_auth_dir)

    # Upstash：token 属敏感信息，只回传「是否已配置」，不回传内容
    up = cfg.get('upstash')
    up = up if isinstance(up, dict) else {}
    token = str(up.get('token') or '')
    view['upstash'] = {
        'url': str(up.get('url') or ''),
        'has_token': bool(token),
        'token_masked': _mask(token) if token else '',
    }

    # 出站设备风控 token（upstream.device_token）同样是凭据：
    # 它相当于把一台可信设备的身份借出去，泄露可被他人复用。
    # 与 Upstash token 一样只回传「是否已配置」+ 掩码。
    upst = cfg.get('upstream')
    upst = upst if isinstance(upst, dict) else {}
    dev = str(upst.get('device_token') or '')
    if 'upstream' in view and isinstance(view['upstream'], dict):
        view['upstream'] = dict(view['upstream'])
        view['upstream'].pop('device_token', None)
    view['upstream'] = {
        **(view.get('upstream') if isinstance(view.get('upstream'), dict) else {}),
        'has_device_token': bool(dev),
        'device_token_masked': _mask(dev) if dev else '',
    }

    view['available'] = True
    view['config_path'] = str(path)
    return view


# 上游 config.json 的可视化字段类型约束：
#   *_hours 是 []int（整点数组），cooldown.* 是时长字符串（30s/10m/2h/1d）
def _has_control_chars(v: str) -> bool:
    """是否含换行或控制字符（路径 / UA 这类单行文本不允许）。"""
    return any(ord(ch) < 32 for ch in v)


# 整点数组字段。上游 2026-09-14 起把 school（开学季）与 cat（夜猫）从宿主机
# crontab 迁入内置调度器，任务类型由 4 类变 6 类——这里必须同步，
# 否则设置页保存这两项会被当成未知键丢弃（白名单外的字段静默忽略）。
_HOURS_KEYS = ('checkin_hours', 'travel_hours', 'activity_hours', 'keepalive_hours',
               'school_hours', 'cat_hours')

# upstream 段里的单行文本字段（会做控制字符与长度校验）
_UPSTREAM_TEXT_KEYS = (
    'user_agent',          # 出站 UA 显式覆盖
    'client_version',      # WorkBuddy 客户端版本段
    'cli_version',         # CLI 版本段
    'client_name',         # 用量归属头 X-Product/X-IDE-Name/X-IDE-Type
    'device_token_file',   # 设备 token 文件路径
)
_UPSTREAM_TEXT_MAX = 512

# 整数/小数字段的取值范围：键 -> (最小, 最大, 单位)
# 上限不是洁癖——这些值直接决定上游的行为强度与成本（例如
# activity_report_count 决定每号每天发多少条对话）。前端的 max 只是
# 输入框属性，拦不住直接调接口，必须服务端兜底。
_INT_RANGES: dict[str, tuple[int, int, str]] = {
    'activity_report_count': (1, 50, '条'),
    'max_in_flight': (0, 64, '个'),
    'breaker_threshold': (1, 100, '次'),
    'idle_weight_max': (0, 1000, ''),
    'max_body_mb': (1, 256, 'MB'),
}

_FLOAT_RANGES: dict[str, tuple[float, float, str]] = {
    'idle_weight_per_hour': (0.0, 100.0, ''),
}


def _check_int(key: str, raw: object) -> int:
    lo, hi, unit = _INT_RANGES[key]
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ValueError(f'{key} 必须是整数')
    if not lo <= raw <= hi:
        raise ValueError(f'{key} 必须在 {lo}-{hi}{unit} 之间（收到 {raw}）')
    return raw


def _check_float(key: str, raw: object) -> float:
    lo, hi, unit = _FLOAT_RANGES[key]
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        raise ValueError(f'{key} 必须是数字')
    val = float(raw)
    if not lo <= val <= hi:
        raise ValueError(f'{key} 必须在 {lo}-{hi}{unit} 之间（收到 {raw}）')
    return val
_DURATION_RE = re.compile(r'^\d+\s*(s|m|h|d)$', re.IGNORECASE)


def _sanitize_section(section: str, incoming: dict) -> dict:
    """校验并归一化要写入的字段，挡住会把配置写坏的非法值。

    前端已经做了校验，这里再做一层兜底：错的数据宁可拒绝（抛错），
    也不要写进上游配置触发容器启动失败。
    """
    out = dict(incoming)
    for key, raw in incoming.items():
        if key in _HOURS_KEYS:
            if not isinstance(raw, list) or not all(
                isinstance(x, int) and not isinstance(x, bool) and 0 <= x <= 23 for x in raw
            ):
                raise ValueError(f'{key} 必须是 0-23 的整点数组，例如 [9, 21]')
            if not raw:
                raise ValueError(f'{key} 至少要有一个时刻')
            out[key] = sorted({int(x) for x in raw})
        elif isinstance(raw, str) and (
            key.endswith(('_rate', '_rate_max', '_cooldown', '_cooldown_max'))
            or key in ('ttl', 'gc_interval')
        ):
            if not _DURATION_RE.match(raw.strip()):
                raise ValueError(f'{key} 时长格式有误，应为 30s / 10m / 2h / 1d')
            out[key] = raw.strip()
        elif section == 'pool' and key == 'expiring_soon':
            # 快过期积分窗口（上游 2026-09-14 新增）：选号时优先消耗窗口内到期的
            # 积分。语义与普通时长不同——**空串或 "0" 表示禁用分桶**，不是非法值，
            # 所以不能套上面那条「必须匹配时长格式」的规则（否则用户没法关掉）。
            val = str(raw or '').strip()
            if val and val != '0' and not _DURATION_RE.match(val):
                raise ValueError('expiring_soon 时长格式有误，应为 168h / 7d；留空或 0 = 禁用')
            out[key] = val
        elif key in _INT_RANGES:
            # 统一区间校验（activity_report_count 等；见 _INT_RANGES 注释）
            out[key] = _check_int(key, raw)
        elif key in _FLOAT_RANGES:
            out[key] = _check_float(key, raw)
        elif section == 'prompt' and key == 'mode':
            mode = str(raw or '').strip().lower()
            if mode not in ('custom', 'passthrough'):
                raise ValueError('prompt.mode 只能是 custom 或 passthrough')
            out[key] = mode
        elif section == 'prompt' and key == 'file':
            # 路径非空但不可读会让上游启动直接失败（fail fast），
            # 因此这里做基础合法性检查，并明确提示风险
            path = str(raw or '').strip()
            if _has_control_chars(path):
                raise ValueError('prompt.file 不能包含换行或控制字符')
            out[key] = path
        elif section == 'upstream' and key in _UPSTREAM_TEXT_KEYS:
            # 单行文本：UA、客户端版本、用量归知名度、设备 token 文件路径
            val = str(raw or '').strip()
            if _has_control_chars(val):
                raise ValueError(f'upstream.{key} 不能包含换行或控制字符')
            if len(val) > _UPSTREAM_TEXT_MAX:
                raise ValueError(f'upstream.{key} 过长（上限 {_UPSTREAM_TEXT_MAX} 字符）')
            out[key] = val
        elif section == 'upstream' and key == 'passthrough_ip':
            if not isinstance(raw, bool):
                raise ValueError('upstream.passthrough_ip 必须是布尔值')
            out[key] = raw
        elif section == 'global' and key == 'enabled':
            # 逃生门开关：false = 锁死纯 CN（上游语义），必须是真布尔
            if not isinstance(raw, bool):
                raise ValueError('global.enabled 必须是布尔值')
            out[key] = raw
        elif section == 'global' and key in ('chat_base', 'billing_base'):
            # Base 地址：单行文本。留空 = 用内置默认（www.workbuddy.ai）
            val = str(raw or '').strip().rstrip('/')
            if _has_control_chars(val):
                raise ValueError(f'global.{key} 不能包含换行或控制字符')
            if len(val) > _UPSTREAM_TEXT_MAX:
                raise ValueError(f'global.{key} 过长（上限 {_UPSTREAM_TEXT_MAX} 字符）')
            if val and not val.startswith(('http://', 'https://')):
                # 上游是按 base + 路径拼接的，缺协议会拼出非法 URL
                raise ValueError(f'global.{key} 需以 http:// 或 https:// 开头')
            out[key] = val
        elif section == 'upstream' and key == 'device_token':
            # 敏感凭据，三种语义要分清：
            #   null   → 显式清除（配置里删掉该键）
            #   非空串 → 设为该值
            #   空串   → 保持原值不变（前端回显的是掩码，留空不能被当成清空）
            if raw is None:
                out['__delete__'] = True
                out.pop(key, None)
                continue
            tok = str(raw or '').strip()
            if _has_control_chars(tok):
                raise ValueError('upstream.device_token 不能包含换行或控制字符')
            # 上游读该文件时也有大小限制（>1KB 忽略），这里给个更保守的上限
            if len(tok) > _UPSTREAM_TEXT_MAX:
                raise ValueError(f'upstream.device_token 过长（上限 {_UPSTREAM_TEXT_MAX} 字符）')
            if tok:
                out[key] = tok
            else:
                out.pop(key, None)
    return out


def save_upstream_config(patch: dict) -> dict:
    """仅允许改写 schedule / pool / cooldown / features / upstash 等非敏感段。

    配置读不到时直接拒绝，绝不基于空 dict 生成新文件覆盖真实配置。
    """
    path = config.UPSTREAM_CONFIG
    if not path.is_file():
        raise FileNotFoundError(f'未找到上游配置文件 {path}，已取消保存')

    try:
        cfg = json.loads(path.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f'上游配置文件解析失败，已取消保存: {exc}') from exc
    if not isinstance(cfg, dict):
        raise ValueError('上游配置文件不是合法的 JSON 对象，已取消保存')

    for field in ('schedule', 'pool', 'cooldown', 'features',
                  'session_sticky', 'prompt', 'server', 'upstream', 'global'):
        if field in patch and isinstance(patch[field], dict):
            cfg.setdefault(field, {})
            clean = _sanitize_section(field, patch[field])
            # __delete__ 表示调用方要求显式删除某些敏感键（见 _sanitize_section）
            if clean.pop('__delete__', False):
                cfg[field].pop('device_token', None)
            cfg[field].update(clean)

    if 'upstash' in patch and isinstance(patch['upstash'], dict):
        incoming = patch['upstash']
        current = cfg.get('upstash')
        current = current if isinstance(current, dict) else {}

        if incoming.get('clear'):
            # 显式关闭：清空 url 与 token
            current = {'url': '', 'token': ''}
        else:
            if 'url' in incoming:
                current['url'] = str(incoming.get('url') or '').strip()
            # token 只在传入非空值时替换：前端回显的是掩码，
            # 留空即表示「保持不变」，避免误清空已配置的凭据
            if str(incoming.get('token') or '').strip():
                current['token'] = str(incoming['token']).strip()

        cfg['upstash'] = {
            'url': str(current.get('url') or ''),
            'token': str(current.get('token') or ''),
        }

    path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding='utf-8')
    # 保存后立即让 realm 的配置缓存失效：否则 global.enabled / 两个 base 的改动
    # 最长要 10 秒后才生效，用户会以为没保存成功
    _realm.invalidate()
    return load_upstream_config()


# ── Upstash 连通性检测 ───────────────────────────────────
def _upstash_rest_base(url: str) -> str | None:
    """把各种写法归一化为 Upstash REST 根地址。

    支持：https://xxx.upstash.io / xxx.upstash.io / rediss://default:tok@xxx.upstash.io:6379
    与 workbuddy2api 的 normalizeURL 保持一致的思路。

    **只做字符串归一化，不做安全判定**：调用方（test_upstash）必须再过一道
    `_reject_internal_host`，否则这里返回的任意主机会被服务端真的请求出去。
    """
    raw = (url or '').strip()
    if not raw:
        return None
    # 去掉 scheme
    if '://' in raw:
        scheme, rest = raw.split('://', 1)
        if scheme.lower() in ('rediss', 'redis'):
            # rediss://user:pass@host:port -> 取 host
            host = rest.rsplit('@', 1)[-1]
            host = host.split(':', 1)[0]
            return f'https://{host}' if host else None
        # https://host/... -> 取 host
        host = rest.split('/', 1)[0].split(':', 1)[0]
        return f'https://{host}' if host else None
    host = raw.split('/', 1)[0].split(':', 1)[0]
    return f'https://{host}' if host else None


# 明确禁止的主机名（云平台元数据服务：SSRF 的头号目标）
_BLOCKED_HOSTNAMES = (
    'metadata.google.internal',
    'metadata.tencentyun.com',
    'metadata',
    'instance-data',
)


def _reject_internal_host(host: str) -> str | None:
    """判断主机是否指向内网/本机/元数据服务；是则返回拒绝原因，否则 None。

    为什么必须拦：这是个**服务端代发起请求**的接口（SSRF）。它拿用户给的地址
    去 POST，再把响应片段回显给调用方。若不拦，管理员账号（或被提权到此的
    攻击者）就能用它探测内网、甚至读取云元数据端点（`169.254.169.254` /
    `metadata.tencentyun.com` —— 后者常能拿到实例临时凭证）。

    管理员权限不等于「可以随便发请求」：这类探测是典型的**提权后利用**步骤，
    纵深防御应当在这里就断掉。

    注意允许自定义 Redis 服务商（如自建 Upstash 兼容服务）：所以不是白名单
    域名，而是**排除内网与元数据**——公网主机名/IP 一律放行。
    """
    h = (host or '').strip().strip('[]').lower()
    if not h:
        return '地址为空'
    if h in _BLOCKED_HOSTNAMES or h.endswith('.internal') or h.endswith('.local'):
        return f'{host} 是不允许探测的内部地址'
    # 明文 IP：拦掉回环 / 私有 / 链路本地（含云元数据 169.254.169.254）/ 保留段
    try:
        addr = ipaddress.ip_address(h)
    except ValueError:
        # 不是 IP 字面量（域名）→ 放行。域名解析到内网的情况由部署方的网络策略兜底，
        # 这里不做 DNS 解析：解析后校验会引入 TOCTOU（解析与请求之间结果可能变），
        # 而且会让每次测试多一次 DNS 查询。
        return None
    if (addr.is_loopback or addr.is_private or addr.is_link_local
            or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
        return f'{host} 是不允许探测的内部地址'
    return None


async def test_upstash(url: str, token: str | None = None) -> tuple[bool, str]:
    """用 Upstash REST 接口探测连通性（PING）。token 留空时取配置文件中的值。

    安全：地址经 `_reject_internal_host` 过滤——这是服务端代发起请求的接口，
    不能让它打到内网或云元数据端点（SSRF）。
    """
    base = _upstash_rest_base(url)
    if not base:
        return False, '请先填写 Upstash 地址'
    host = base.split('://', 1)[-1].split('/', 1)[0]
    blocked = _reject_internal_host(host)
    if blocked:
        return False, blocked

    if not token:
        try:
            cfg = json.loads(config.UPSTREAM_CONFIG.read_text(encoding='utf-8'))
            token = str((cfg.get('upstash') or {}).get('token') or '')
        except Exception:  # noqa: BLE001
            token = ''
    if not token:
        return False, '缺少 Upstash Token'

    try:
        async with config.http_client(10, connect=5) as client:
            resp = await client.post(
                f'{base}/ping',
                headers={'Authorization': f'Bearer {token}'},
            )
        if resp.status_code == 401:
            return False, 'Token 无效（401）'
        if resp.status_code >= 400:
            return False, f'Upstash 返回 {resp.status_code}'
        body = resp.text.strip()
        if 'PONG' in body.upper():
            return True, '连接正常（PONG）'
        return True, f'已连通，响应：{body[:60]}'
    except Exception as exc:  # noqa: BLE001
        return False, f'无法连接：{_err_text(exc)}'
