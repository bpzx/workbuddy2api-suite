"""API 密钥的生成、校验与限额判定（网关与管理端共用）。"""
from __future__ import annotations

import hashlib
import json
import secrets
import time

from . import db
from .iputil import ip_matches

TOKEN_PREFIX = 'wbk_'


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _parse(row) -> dict:
    return {
        'id': row['id'],
        'name': row['name'],
        'prefix': row['prefix'],
        'enabled': bool(row['enabled']),
        'expires_at': row['expires_at'],
        'max_ips': row['max_ips'],
        'ip_allowlist': json.loads(row['ip_allowlist'] or '[]'),
        'models': json.loads(row['models'] or '[]'),
        'quota': row['quota'],
        'used_tokens': row['used_tokens'],
        'created_at': row['created_at'],
        'last_used_at': row['last_used_at'],
    }


def list_keys() -> list[dict]:
    rows = db.query('SELECT * FROM api_keys ORDER BY id DESC')
    return [_parse(r) for r in rows]


def create_key(
    name: str,
    expires_at: int | None = None,
    max_ips: int = 0,
    ip_allowlist: list[str] | None = None,
    models: list[str] | None = None,
    quota: int = 0,
) -> dict:
    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    key_id = db.execute(
        'INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, ip_allowlist, models, quota, used_tokens, created_at) '
        'VALUES(?, ?, ?, 1, ?, ?, ?, ?, ?, 0, ?)',
        (
            name,
            _hash(token),
            token[:12],
            expires_at,
            max_ips,
            json.dumps(ip_allowlist or []),
            json.dumps(models or []),
            quota,
            int(time.time()),
        ),
    )
    row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    out = _parse(row)
    out['key'] = token  # 仅此一次返回明文
    return out


def update_key(key_id: int, patch: dict) -> dict | None:
    row = db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,))
    if not row:
        return None
    fields: dict[str, object] = {}
    if 'name' in patch and patch['name']:
        fields['name'] = str(patch['name'])
    if 'enabled' in patch:
        fields['enabled'] = 1 if patch['enabled'] else 0
    if 'expires_at' in patch:
        fields['expires_at'] = patch['expires_at']
    if 'max_ips' in patch:
        fields['max_ips'] = int(patch['max_ips'] or 0)
    if 'ip_allowlist' in patch:
        fields['ip_allowlist'] = json.dumps(patch['ip_allowlist'] or [])
    if 'models' in patch:
        fields['models'] = json.dumps(patch['models'] or [])
    if 'quota' in patch:
        fields['quota'] = int(patch['quota'] or 0)
    if fields:
        assignments = ', '.join(f'{k} = ?' for k in fields)
        db.execute(f'UPDATE api_keys SET {assignments} WHERE id = ?', (*fields.values(), key_id))
    return _parse(db.query_one('SELECT * FROM api_keys WHERE id = ?', (key_id,)))


def delete_key(key_id: int) -> bool:
    if not db.query_one('SELECT id FROM api_keys WHERE id = ?', (key_id,)):
        return False
    db.execute('DELETE FROM api_keys WHERE id = ?', (key_id,))
    db.execute('DELETE FROM api_key_ips WHERE key_id = ?', (key_id,))
    return True


def reset_usage(key_id: int) -> None:
    db.execute('UPDATE api_keys SET used_tokens = 0 WHERE id = ?', (key_id,))


def resolve(token: str) -> dict | None:
    """按前缀定位后比对哈希，避免全表扫描。"""
    if not token or not token.startswith(TOKEN_PREFIX):
        return None
    rows = db.query('SELECT * FROM api_keys WHERE prefix = ?', (token[:12],))
    digest = _hash(token)
    for row in rows:
        if secrets.compare_digest(row['key_hash'], digest):
            return _parse(row)
    return None


def validate(key: dict, ip: str, model: str | None) -> str | None:
    """返回 None 表示放行，否则返回拒绝原因。"""
    if not key['enabled']:
        return '密钥已停用'
    if key['expires_at'] and key['expires_at'] < time.time():
        return '密钥已过期'
    if key['quota'] and key['used_tokens'] >= key['quota']:
        return '密钥配额已用尽'

    allow = key['ip_allowlist']
    if allow and not any(ip_matches(ip, c) for c in allow):
        return f'来源 IP {ip} 不在密钥白名单内'

    if key['max_ips']:
        known = db.query('SELECT ip FROM api_key_ips WHERE key_id = ?', (key['id'],))
        ips = {r['ip'] for r in known}
        if ip not in ips and len(ips) >= key['max_ips']:
            return f'密钥已绑定 {len(ips)} 个 IP，超出上限 {key["max_ips"]}'

    # 模型白名单：**不能因为 model 缺失就跳过检查**。
    # 原写法 `if key['models'] and model and model not in ...` 在 body 不带 model
    # （或传空串/非字符串）时整段跳过，于是限定单模型的密钥可用「不带 model」的
    # 请求走上游默认模型——白名单形同虚设。请求侧已在网关把缺失/非法的 model
    # 拦成 400；这里再兜一层，任何非字符串或空值一律拒绝。
    if key['models']:
        if not isinstance(model, str) or not model.strip():
            return '请求未指定 model，而该密钥启用了模型白名单'
        if model not in key['models']:
            return f'模型 {model} 不在密钥白名单内'
    return None


def touch(key: dict, ip: str, tokens: int = 0) -> None:
    db.execute(
        'INSERT OR IGNORE INTO api_key_ips(key_id, ip, first_seen) VALUES(?, ?, ?)',
        (key['id'], ip, int(time.time())),
    )
    db.execute('UPDATE api_keys SET last_used_at = ? WHERE id = ?', (int(time.time()), key['id']))
    if tokens:
        db.execute('UPDATE api_keys SET used_tokens = used_tokens + ? WHERE id = ?', (tokens, key['id']))
