"""套件自更新端点（/api/system/suite-*）。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

为什么单独开一个路由文件，而不是改上游的 routers/system.py
----------------------------------------------------------------
本项目的边界是「vendor/ 下的上游代码一字不改」，上游的 system.py 与
updater.py 一概不动（CI 有反向断言守着）。我们自己的端点放在本文件里，
构建期由 docker/patches/apply.py 落位，并在 main.py 里只加两行注册。

与上游 /api/system/* 的关系
---------------------------
两者**职责不同、互不重叠**：
  * 上游 /api/system/* 管的是"更新上游网关 / 更新管理端代码"（裸机流程）；
  * 本文件 /api/system/suite-* 管的是"更新本套件自己的镜像"（容器流程）。

本发行版的面板已不再提供上游更新按钮 —— 上游更新必须走宿主机
`./scripts/sync-upstreams.sh` + 重建镜像，面板只报告"是否有更新"。
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request

from .. import security
from ..iputil import client_ip
from ..services import suite

router = APIRouter(prefix='/api/system', tags=['suite'])


@router.get('/suite-status')
async def suite_status(user: dict = Depends(security.current_user)):
    """套件更新状态（含 updater 侧车是否可用）。

    任何登录用户可读：普通用户也该看到"有新版本"，只是不能点更新。
    """
    return await suite.read_status()


@router.get('/suite-check')
async def suite_check(force: bool = False, user: dict = Depends(security.current_user)):
    """版本检查：本套件 + 两个上游。

    force 绕过 6 小时缓存。与上游 check_update 的处理一致：非管理员静默
    降级为使用缓存，而不是报 403 —— 手动刷新只是便利功能，不值得为它
    返回错误。
    """
    if force and user.get('role') != 'admin':
        force = False
    return await suite.check(force=force)


@router.post('/suite-update')
async def suite_update(request: Request, user: dict = Depends(security.require_admin)):
    """触发套件一键更新（拉新镜像并重建容器）。

    只有管理员能触发：这会重建包括管理端在内的全部容器，属于高危操作。
    上游的 system.py 没有做审计，我们这里补上 —— 更新动作必须可追溯
    （谁在什么时候点了更新）。
    """
    ok, message = await suite.trigger_update()
    security.audit(
        user, 'suite_update', '',
        f'{message}（来源 {client_ip(request)}）',
    )
    if not ok:
        # 409 与上游 start_update 的失败语义保持一致：请求本身合法，
        # 但当前状态不允许执行（侧车不可达 / 已有更新在跑）。
        raise HTTPException(status_code=409, detail=message)
    return {'ok': True, 'message': message}
