"""管理端鉴权与会话。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from .. import config, security
from ..iputil import client_ip

router = APIRouter(prefix='/api', tags=['auth'])


@router.get('/healthz')
def healthz() -> dict:
    return {'ok': True, 'service': 'workbuddy-manager'}


@router.post('/login')
async def login(request: Request) -> JSONResponse:
    ip = client_ip(request)

    # 先解析用户名，以便同时做「按 IP」与「按用户名」的锁定判断
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        raise HTTPException(status_code=400, detail='请求格式错误') from None
    # body 必须是 JSON 对象：`null` / `[]` / `"str"` / `123` 都是**合法 JSON**，
    # 上面的 try 不会拦住它们，随后 body.get(...) 会抛 AttributeError → 500。
    # 500 不泄露内容，但属于未处理异常：每次触发都在服务端留下错误日志
    # （可被用来刷日志），且暴露了输入校验不完整。这里显式拒绝。
    if not isinstance(body, dict):
        raise HTTPException(status_code=400, detail='请求格式错误') from None
    username = str(body.get('username', '')).strip()[:64]
    password = str(body.get('password', ''))

    if security.login_blocked(ip, username):
        raise HTTPException(status_code=429, detail='失败次数过多，请 10 分钟后再试')

    cfg = security.load_users()
    user = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not user or not security.verify_pwd(password, user.get('pwd_hash', '')):
        # 同时记 IP 与用户名：前者防单机爆破，后者防换 IP 打同一账号
        security.record_fail(ip, username)
        # 登录失败也要留痕：本次事故中攻击者拿到了会话，但服务端没有任何记录
        security.audit({'username': username or '(空)'}, 'login_failed', username,
                       f'来源 {ip}')
        raise HTTPException(status_code=401, detail='用户名或密码错误')

    security.clear_fail(ip, username)
    token = security.issue_token(username, user.get('role', 'viewer'))
    security.audit(user, 'login', username, f'来源 {ip}')
    resp = JSONResponse({'ok': True, 'username': username, 'role': user.get('role', 'viewer')})
    resp.set_cookie(
        config.COOKIE_NAME,
        token,
        max_age=config.SESSION_DAYS * 86400,
        httponly=True,
        samesite='lax',
        secure=security.cookie_secure(request),
        path='/',
    )
    return resp


@router.post('/logout')
def logout() -> JSONResponse:
    resp = JSONResponse({'ok': True})
    resp.delete_cookie(config.COOKIE_NAME, path='/')
    return resp


@router.get('/me')
def me(user: dict = Depends(security.current_user)) -> dict:
    return {'username': user.get('username'), 'role': user.get('role', 'viewer')}
