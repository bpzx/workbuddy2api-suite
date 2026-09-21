"""API 密钥管理接口。"""
from __future__ import annotations

import ipaddress

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

from .. import db, keysvc, security
from ..services import modelcatalog
from ..iputil import client_ip

router = APIRouter(prefix='/api/keys', tags=['keys'])

# 校验失败时给用户的说明。放在写入**之前**拦，而不是存下去再说 ——
# 存坏数据的后果是「该密钥永久不可用」：`ip_matches` 对非法 CIDR 一律返回
# False（fail-closed，方向是对的），于是白名单里只要有一个写错的 CIDR，
# 这把密钥对**所有**来源 IP 都拒绝，而报错只说「不在白名单内」，
# 用户完全看不出是自己把 CIDR 写错了。宁可在这里拒掉并说清怎么写。
_CIDR_HINT = ('IP 白名单里有无法识别的条目：{bad}。\n'
              '  请写成单个 IP（1.2.3.4）或 CIDR（10.0.0.0/8）的形态。\n'
              '  留着它会让这把密钥**拒绝所有来源**（因为匹配不上任何 IP）。')


def _check_ip_allowlist(items: list[str] | None) -> None:
    """逐项校验 IP 白名单，非法即 400（附上该怎么写）。"""
    for raw in items or []:
        s = str(raw).strip()
        if not s:
            continue      # 空项由 keysvc 过滤掉，不算错
        try:
            ipaddress.ip_network(s, strict=False)
        except ValueError:
            raise HTTPException(status_code=400,
                                detail=_CIDR_HINT.format(bad=s)) from None


def _check_name(name: str | None) -> None:
    """名称不能只有空白。

    否则列表里会出现一行「没有名字」的密钥，管理员认不出它是干什么的、
    也不知道是自己误操作建的（实测可以建出一把 name='   ' 的密钥）。
    """
    if name is not None and not str(name).strip():
        raise HTTPException(status_code=400, detail='密钥名称不能只有空格')


class KeyIn(BaseModel):
    name: str = Field(min_length=1, max_length=64)
    expires_at: int | None = None
    max_ips: int = 0
    ip_allowlist: list[str] = Field(default_factory=list)
    models: list[str] = Field(default_factory=list)
    quota: int = 0
    # 积分额度（issue #27）：0 = 不限。与 token 额度各自独立，任一超限即拒绝。
    # 用 float：上游 credit 是小数（如 0.05 表示按倍率扣费）。
    quota_credit: float = 0
    # 版本归属：'' = 不限制（存量密钥的形态）。非 cn/global 的值由
    # keysvc._norm_realm 归一化成 ''——不报错，免得旧前端（不带该字段）被拒。
    realm: str = Field(default='', max_length=16)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    expires_at: int | None = None
    max_ips: int | None = None
    ip_allowlist: list[str] | None = None
    models: list[str] | None = None
    quota: int | None = None
    quota_credit: float | None = None
    realm: str | None = None


class WhitelistCheckIn(BaseModel):
    models: list[str] = Field(default_factory=list)
    realm: str = Field(default='', max_length=16)


@router.post('/check-models')
def check_models(body: WhitelistCheckIn,
                 user: dict = Depends(security.current_user)) -> dict:
    """检查模型白名单里哪些名字**匹配不到已知模型**（issue #46 的可选做法 2）。

    为什么要有个接口而不是前端自己比对：判据必须与调用侧**同一份**
    （`keysvc._bare_model` 的去 `cn:` 前缀、保留 `global:` 口径）。前端再实现
    一遍就是第二份事实来源，迟早漂移——而"两处口径不一致"正是 issue #46 的成因。

    **拿不到清单时不猜**（两种粒度）：

      · 两份清单都拿不到 → `checked: false`，前端如实显示「暂时无法校验」；
      · 只有一份拿得到 → **只判那一版的条目**，另一版的条目原样放过。因为两个版本
        的清单是分开取的，拿国内版清单去判 `global:xxx` 必然判成"找不到"——那是
        假警报，用户会去改一个本来正确的名字（比不提示更糟）。

    校验刻意**只读缓存、不发网络**：它挂在输入框失焦上，不该让一次上游慢响应把
    交互拖住；而且校验失败是可接受的（下次再看），打上游失败反而更糟。
    """
    names = [str(x).strip() for x in body.models if str(x).strip()]
    if not names:
        return {'checked': True, 'unknown': []}

    # 两个版本的清单**分别取、分别判**（见 docstring 里的假警报说明）
    known_by_realm: dict[str, set[str] | None] = {
        r: modelcatalog.cached_ids(r) for r in ('cn', 'global')
    }
    if all(v is None for v in known_by_realm.values()):
        return {'checked': False, 'unknown': [],
                'reason': '暂时读不到模型清单（去「模型」页刷新一次再回来）'}

    aliases = list((db.get_setting('model_map', {}) or {}).keys())
    unknown = keysvc.unknown_whitelist_entries(names, known_by_realm, aliases)
    return {'checked': True, 'unknown': unknown}


@router.get('')
def list_keys(user: dict = Depends(security.current_user)) -> list[dict]:
    return keysvc.list_keys()


@router.post('')
def create_key(body: KeyIn, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    _check_name(body.name)
    _check_ip_allowlist(body.ip_allowlist)
    created = keysvc.create_key(
        name=body.name,
        expires_at=body.expires_at,
        max_ips=body.max_ips,
        ip_allowlist=body.ip_allowlist,
        models=body.models,
        quota=body.quota,
        realm=body.realm,
        quota_credit=body.quota_credit,
    )
    # 密钥是拿额度用的凭证，发放必须留痕（含来源 IP）
    security.audit(user, 'create_key', str(created.get('name') or ''),
                   f"id={created.get('id')}；来源 {client_ip(request)}")
    return created


@router.patch('/{key_id}')
def update_key(key_id: int, body: KeyPatch, user: dict = Depends(security.require_admin)) -> dict:
    # 只校验本次真的提交了的字段（PATCH 是部分更新，`exclude_unset` 语义）
    patch = body.model_dump(exclude_unset=True)
    if 'name' in patch:
        _check_name(patch['name'])
    if 'ip_allowlist' in patch:
        _check_ip_allowlist(patch['ip_allowlist'])
    updated = keysvc.update_key(key_id, patch)
    if not updated:
        raise HTTPException(status_code=404, detail='密钥不存在')
    return updated


@router.post('/{key_id}/reset-usage')
def reset_usage(key_id: int, user: dict = Depends(security.require_admin)) -> dict:
    # 不存在的 id 应报 404，而不是静默成功：否则前端会提示「已重置」，
    # 而实际什么都没发生（密钥可能已被别人删掉，页面上却看着还在）。
    if not keysvc.reset_usage(key_id):
        raise HTTPException(status_code=404, detail='密钥不存在')
    return {'ok': True}


@router.delete('/{key_id}')
def delete_key(key_id: int, request: Request,
               user: dict = Depends(security.require_admin)) -> dict:
    if not keysvc.delete_key(key_id):
        raise HTTPException(status_code=404, detail='密钥不存在')
    security.audit(user, 'delete_key', str(key_id), f'来源 {client_ip(request)}')
    return {'ok': True}
