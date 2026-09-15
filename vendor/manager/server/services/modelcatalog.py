"""模型目录：给「模型中心」页提供带细节的模型清单。

数据来源分两级，UI 会如实标注当前用的是哪一级：

  1. **腾讯模型接口**（首选）：用本地账号直连腾讯，能拿到显示名（`name`）、
     真实上下文（maxInputTokens）、最大输出、以及**推理档位**
     （`reasoning.supportedEfforts`）。这些字段上游的 `/v1/models` 会丢掉。
  2. **上游 /v1/models**（回退）：只给 id / 上下文 / 最大输出。腾讯接口调不通
     （账号全过期、网络问题）时用这一级，保证页面不空白。

为什么需要缓存：每次进页面都打腾讯接口既慢又容易被风控。这里缓存 5 分钟，
并在失败时做短暂的负缓存，避免连续失败时反复重试。

安全：不返回任何凭据；只暴露模型元数据。
"""
from __future__ import annotations

import json
import time

from .. import config
from . import tencent, wb2api

# 成功缓存 5 分钟：模型清单变化很慢，没必要每次进页面都打腾讯
_TTL_OK = 300
# 失败负缓存 60 秒：避免连续失败时反复重试（尤其账号全过期时）
_TTL_FAIL = 60

# 缓存**按版本分键**：国内版与国际版的模型清单完全不同，若共用一份，
# 后拉取的那个会把前一个覆盖掉（两个版本的模型无法共存）。
_cache: dict[str, dict] = {}


def _slot(realm: str) -> dict:
    return _cache.setdefault(realm, {'at': 0.0, 'ttl': 0, 'payload': None})


# 系列归属：腾讯接口不返回，这里按模型 id 的前缀做**命名约定推导**。
# 仅为便于分组浏览，UI 上会标明是推导值；认不出的一律归入「其他」，
# 不做猜测性归类。
_SERIES_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (('glm',), '智谱 GLM'),
    (('deepseek',), 'DeepSeek'),
    (('kimi', 'moonshot'), 'Kimi'),
    (('minimax',), 'MiniMax'),
    (('hy', 'hunyuan'), '腾讯混元'),
    (('auto',), '自动选择'),
)


def series_of(model_id: str) -> str:
    """按 id 前缀推导系列名。认不出返回「其他」。"""
    mid = (model_id or '').lower()
    for prefixes, label in _SERIES_RULES:
        if mid.startswith(prefixes):
            return label
    return '其他'


def _pick_account() -> dict | None:
    """挑一个最可能可用的本地账号（优先剩余有效期长的、未过期的）。

    不做网络探测：那会把简单列表请求变成慢操作。选到不可用的账号时，
    调用方会再尝试下一个，最多试 `_MAX_TRIES` 个。
    """
    try:
        accounts = wb2api.list_auth_accounts()
    except Exception:  # noqa: BLE001
        return None
    alive = [a for a in accounts if not a.get('is_expired') and a.get('remain_seconds', 0) > 0]
    alive.sort(key=lambda a: a.get('remain_seconds', 0), reverse=True)
    return alive[0] if alive else None


def _load_token(filename: str) -> str:
    """读取指定 auth 文件的 accessToken。只在服务端内部使用，不外传。"""
    try:
        raw = wb2api.read_account_file(filename)
    except Exception:  # noqa: BLE001
        return ''
    return str((raw.get('auth') or {}).get('accessToken') or '')


def _decorate(items: list[dict]) -> list[dict]:
    return [
        {
            'id': m.get('id', ''),
            'name': m.get('name') or '',
            'context_length': int(m.get('context_length') or 0),
            'max_output_tokens': int(m.get('max_output_tokens') or 0),
            'efforts': list(m.get('efforts') or []),
            'series': series_of(str(m.get('id') or '')),
        }
        for m in items
        if m.get('id')
    ]


async def catalog(realm: str = 'cn', force: bool = False) -> dict:
    """返回**指定版本**的模型清单与统计。结果带 source / fetched_at 等元信息。

    realm 区分国内版与国际版：两边的模型几乎不重叠（国际版以 gpt-* 等为主），
    必须分开取、分开缓存，否则会互相覆盖。
    """
    slot = _slot(realm)
    now = time.time()
    if not force and slot['payload'] is not None and now - slot['at'] < slot['ttl']:
        out = dict(slot['payload'])
        out['cached'] = True
        out['cache_age'] = int(now - slot['at'])
        return out

    result = await _build(realm, force=force)
    slot.update({
        'at': now,
        'ttl': _TTL_OK if result.get('source') == 'tencent' else _TTL_FAIL,
        'payload': result,
    })
    return dict(result, cached=False, cache_age=0)


async def _build(realm: str, force: bool = False) -> dict:
    """依次尝试：腾讯接口（多个账号）→ 上游 /v1/models。

    候选账号**只取该版本的**——否则可能拿国际版账号去问国内版端点
    （或反过来），既取不到模型也平白多一次失败。
    """
    errors: list[str] = []
    # 最多试 3 个账号：单个账号可能恰好凭证失效，但没必要把所有账号都试一遍
    try:
        candidates = [
            a for a in wb2api.list_auth_accounts()
            if a.get('realm') == realm
            and not a.get('is_expired') and a.get('remain_seconds', 0) > 0
        ]
        candidates.sort(key=lambda a: a.get('remain_seconds', 0), reverse=True)
    except Exception as exc:  # noqa: BLE001
        candidates = []
        errors.append(f'读取本地账号失败: {exc}')
    if not candidates:
        errors.append(f'没有可用的{"国际版" if realm == "global" else "国内版"}账号')

    for acct in candidates[:3]:
        token = _load_token(str(acct.get('file') or ''))
        if not token:
            continue
        ok, data = await tencent.fetch_models({'access_token': token, 'realm': realm,
                                               'domain': acct.get('domain', '')})
        if ok and isinstance(data, list) and data:
            return {
                'models': _decorate(data),
                'source': 'tencent',
                'source_label': '腾讯模型接口（含显示名与推理档位）',
                'via': acct.get('nickname') or acct.get('uid') or '',
                'errors': errors,
            }
        errors.append(f'{acct.get("nickname") or acct.get("uid")}: {data}')

    # 回退：上游 /v1/models（字段少，但至少保证页面有内容）。
    # 注意上游的清单里带 `cn:` / `global:` 前缀，必须按版本挑出属于本版本的那些，
    # 否则两个版本的模型会混在一页里。
    ok, data = await wb2api.get_models()
    if ok:
        items = data if isinstance(data, list) else (
            data.get('data') if isinstance(data, dict) else None
        )
        if isinstance(items, list) and items:
            picked = [_strip_realm_prefix(m) for m in items if _belongs(m, realm)]
            picked = [m for m in picked if m.get('id')]
            if picked:
                return {
                    'models': _decorate(picked),
                    'source': 'upstream',
                    'source_label': '上游 /v1/models（字段有限：无显示名与推理档位）',
                    'via': 'workbuddy2api',
                    'errors': errors,
                }
            errors.append(f'上游清单里没有{"国际版" if realm == "global" else "国内版"}条目')
    else:
        errors.append(f'上游 /v1/models 失败: {data}')

    return {
        'models': [],
        'source': 'none',
        'source_label': '暂无可用的模型数据来源',
        'via': '',
        'errors': errors,
    }


def _strip_realm_prefix(m: dict) -> dict:
    """去掉上游模型名的 `cn:` / `global:` 前缀，返回新 dict。

    上游 `/v1/models` 输出的名字带版本前缀（网关侧约定），界面展示时不需要，
    而且同一个裸名可能在两个版本各有一份；保留前缀会导致显示成 `cn:glm-5.2`。
    """
    mid = str(m.get('id') or '')
    for pref in ('cn:', 'global:'):
        if mid.lower().startswith(pref):
            mid = mid[len(pref):]
            break
    return {**m, 'id': mid}


def _belongs(m: dict, realm: str) -> bool:
    """上游清单里的这一条是否属于指定版本。

    带前缀的按前缀判定；不带前缀的（老版本上游）默认算国内版，
    与国际版的「裸名默认 CN」口径一致。
    """
    mid = str(m.get('id') or '').lower()
    if mid.startswith('global:'):
        return realm == 'global'
    if mid.startswith('cn:'):
        return realm == 'cn'
    return realm == 'cn'


def invalidate(realm: str | None = None) -> None:
    """清空缓存（手动刷新、账号变动后调用）。不传 realm 则全清。"""
    if realm is None:
        _cache.clear()
        return
    _cache.pop(realm, None)


def summarize(models: list[dict]) -> dict:
    """统计卡数据。全部由清单真实计算，不含推测项。

    容忍未经过 `_decorate` 的原始条目（缺 series 时现场推导），
    这样调用方不必先确保装饰过，统计口径也不会因入口不同而漂移。
    """
    ids = [m.get('id', '') for m in models]
    reasoning = [m for m in models if m.get('efforts')]
    # 「大上下文」按 128K 起算（常见分档线），仅作浏览辅助
    large = [m for m in models if (m.get('context_length') or 0) >= 131072]
    max_ctx = max((m.get('context_length') or 0 for m in models), default=0)
    series = sorted({m.get('series') or series_of(str(m.get('id') or '')) for m in models})
    return {
        'total': len(models),
        'reasoning': len(reasoning),
        'large_context': len(large),
        'max_context': max_ctx,
        'series': series,
        'unique_ids': len(set(ids)),
    }
