"""请求日志查询。"""
from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException

from .. import db, keysvc, security

router = APIRouter(prefix='/api/logs', tags=['logs'])


@router.get('')
def list_logs(
    page: int = 1,
    size: int = 20,
    key_id: str | None = None,
    model: str | None = None,
    status: str | None = None,
    ip: str | None = None,
    days: int | None = None,
    user: dict = Depends(security.current_user),
) -> dict:
    where: list[str] = []
    args: list[object] = []

    if days:
        # 钳到 [1, 3650]：超大值会在 SQLite 绑定时溢出（曾 500），
        # 负值则会把过滤条件变成「未来之后」，语义上无意义
        d = min(3650, max(1, int(days)))
        where.append('l.ts >= ?')
        args.append(int(time.time()) - d * 86400)
    if key_id and key_id not in ('all', ''):
        # 非数字会 int() 抛错 → 500。这里显式拦成 400，避免用非法输入探测
        try:
            kid = int(key_id)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail='key_id 必须是数字或 all') from None
        where.append('l.key_id = ?')
        args.append(kid)
    if model:
        where.append('(l.model LIKE ? OR l.mapped_model LIKE ?)')
        args.extend([f'%{model}%', f'%{model}%'])
    if ip:
        where.append('l.ip LIKE ?')
        args.append(f'%{ip}%')
    if status == 'ok':
        where.append('l.status >= 200 AND l.status < 300')
    elif status == 'error':
        where.append('(l.status < 200 OR l.status >= 300)')

    clause = ('WHERE ' + ' AND '.join(where)) if where else ''
    total = db.query_one(f'SELECT COUNT(*) AS c FROM request_logs l {clause}', args)['c']

    page = max(1, page)
    size = min(200, max(1, size))
    rows = db.query(
        f'SELECT l.*, k.name AS key_name FROM request_logs l '
        f'LEFT JOIN api_keys k ON k.id = l.key_id {clause} '
        f'ORDER BY l.id DESC LIMIT ? OFFSET ?',
        (*args, size, (page - 1) * size),
    )
    items = [
        {
            'id': r['id'],
            'ts': r['ts'],
            'key_id': r['key_id'],
            'key_name': r['key_name'],
            'ip': r['ip'],
            'model': r['model'],
            'mapped_model': r['mapped_model'],
            'status': r['status'],
            'prompt_tokens': r['prompt_tokens'],
            'completion_tokens': r['completion_tokens'],
            'latency_ms': r['latency_ms'],
            # 首字延迟：NULL 表示未采集（非流式请求或旧记录）
            'first_token_ms': r['first_token_ms'],
            'ua': r['ua'],
            'error': r['error'],
            'stream': bool(r['stream']),
            'credit': r['credit'],
        }
        for r in rows
    ]
    return {'total': total, 'items': items}


@router.post('/clear')
def clear_logs(user: dict = Depends(security.require_admin)) -> dict:
    db.execute('DELETE FROM request_logs')
    return {'ok': True}
