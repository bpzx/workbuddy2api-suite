"""管理端鉴权：PBKDF2 密码、HMAC 签名 Cookie 会话、登录防爆破。"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time

from fastapi import Depends, HTTPException, Request

from . import config

_fail: dict[str, list] = {}
MAX_FAILS = 5
LOCK_SECONDS = 600


# ── 密码哈希 ─────────────────────────────────────────────
def make_hash(pwd: str, iterations: int = 260000) -> str:
    salt = secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac('sha256', pwd.encode(), bytes.fromhex(salt), iterations)
    return f'pbkdf2_sha256${iterations}${salt}${digest.hex()}'


def verify_pwd(pwd: str, stored: str) -> bool:
    try:
        algo, iters, salt, expect = stored.split('$')
        if algo != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac('sha256', pwd.encode(), bytes.fromhex(salt), int(iters))
        return hmac.compare_digest(digest.hex(), expect)
    except Exception:
        return False


# ── 用户存储 ─────────────────────────────────────────────
def load_users() -> dict:
    """读取 users.json。

    **绝不因解析失败而自动重建**：原实现在文件损坏（半写、磁盘满、进程被杀、
    手工改坏）时直接 `bootstrap_users()`，那会**生成新 secret + 新随机密码的
    admin 并原地覆盖写回**——结果是全部账号（含其他管理员）被静默清空、
    所有人的会话失效，而新密码只打印在 stderr 里。一次瞬时故障就足以让
    管理面「重启」，且现场被覆盖、无法复盘。实测复现过。

    现在：文件不存在 → 首次启动，正常 bootstrap；
          文件存在但读不了/解析不了 → 抛错，让请求失败并保留现场。
    """
    config.ensure_dirs()
    if not config.USERS_FILE.exists():
        return bootstrap_users()
    try:
        data = json.loads(config.USERS_FILE.read_text(encoding='utf-8'))
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f'用户文件损坏，拒绝自动重建以保护现场：{config.USERS_FILE}（{exc}）。'
            '请先备份该文件，再用备份恢复；若确需重置，手动删除该文件后重启。'
        ) from exc
    if not isinstance(data, dict) or not isinstance(data.get('users'), list):
        raise RuntimeError(f'用户文件结构异常：{config.USERS_FILE}')
    if not data.get('secret'):
        raise RuntimeError(f'用户文件缺少 secret：{config.USERS_FILE}')
    return data


def _restrict_permissions(path) -> None:
    """把敏感文件权限收紧到仅属主可读写（0600）。

    users.json 里存着**签发会话 Cookie 的 secret**——拿到它就能伪造任意角色
    （含 admin）的登录态。因此它属于最高敏感级，不应让同主机的其他用户读到。
    Windows 上 os.chmod 只影响只读位，无实际意义，故静默忽略失败。
    """
    import os
    import stat
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass


def save_users(data: dict) -> None:
    """原子写入 users.json。

    为什么必须原子：原实现直接 `O_TRUNC` 覆盖，**写窗口内被杀进程/断电/磁盘满
    就会留下半截文件**，进而触发（修复前的）自动重建 → 全部账号被清空。
    现在改为「写临时文件 → fsync → os.replace」：替换是原子的，读者要么看到
    旧内容、要么看到新内容，永远不会看到半截。

    临时文件同样以 0600 创建，避免"先写后改权限"之间出现可被他人读取的窗口。

    **失败时绝不退化成非原子直写**：那样等于把刚堵上的损坏路径又打开一次。
    替换失败就报错，让调用方看到（旧文件此时仍完好，可安全重试）。
    """
    config.ensure_dirs()
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    target = config.USERS_FILE
    tmp = target.with_name(f'.{target.name}.tmp')

    # 支持不支持 O_CREAT 指定 mode 的平台：退化为默认权限创建后再 chmod
    try:
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    except OSError:
        fd = None

    try:
        if fd is not None:
            with os.fdopen(fd, 'w', encoding='utf-8') as fh:
                fh.write(payload)
                fh.flush()
                os.fsync(fh.fileno())  # 落盘后再替换，避免断电留下空/半截文件
        else:
            # 先写临时文件（非目标文件），仍然保证目标只在原子替换时改变
            tmp.write_text(payload, encoding='utf-8')
        _restrict_permissions(tmp)
        os.replace(tmp, target)  # 原子替换：要么旧、要么新，不会半截
        _restrict_permissions(target)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        # 旧文件未被触碰，保持完好；把失败暴露出去让上层处理
        raise


def bootstrap_users() -> dict:
    """首次启动：创建 admin 用户，密码取 WB_ADMIN_PASSWORD 或随机生成并打印一次。"""
    import os
    import sys

    pwd = os.environ.get('WB_ADMIN_PASSWORD', '').strip() or secrets.token_urlsafe(9)
    data = {
        'secret': config.new_secret(),
        'users': [{'username': 'admin', 'role': 'admin', 'pwd_hash': make_hash(pwd)}],
        'api_keys': [],
    }
    save_users(data)
    if not os.environ.get('WB_ADMIN_PASSWORD', '').strip():
        print('=' * 60, file=sys.stderr)
        print('[WorkBuddy Manager] 已生成初始管理员账号', file=sys.stderr)
        print(f'  用户名: admin', file=sys.stderr)
        print(f'  密码  : {pwd}', file=sys.stderr)
        print('  请登录后立即在「设置 → 管理用户」中修改密码。', file=sys.stderr)
        print('=' * 60, file=sys.stderr)
    return data


# ── 会话签名 ─────────────────────────────────────────────
def _sign(payload: str, secret: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    return base64.urlsafe_b64encode(f'{payload}|{sig}'.encode()).decode()


def _unsign(token: str, secret: str) -> dict | None:
    try:
        raw = base64.urlsafe_b64decode(token.encode()).decode()
        payload, sig = raw.rsplit('|', 1)
        expect = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expect, sig):
            return None
        obj = json.loads(payload)
        if obj.get('exp', 0) < time.time():
            return None
        return obj
    except Exception:
        return None


def issue_token(username: str, role: str) -> str:
    """签发会话 cookie。

    载荷里带 `sv`（session version）：该用户当前的会话版本号。改密码 / 改角色 /
    「吊销会话」都会递增它，于是**已签发的 cookie 立即失效**——否则被盗会话
    即使改了密码也还能继续用到 7 天过期。role 仍写进载荷仅作展示参考，
    鉴权一律以用户表为准（见 current_user）。
    """
    cfg = load_users()
    sv = session_version(cfg, username)
    payload = json.dumps(
        {'username': username, 'role': role, 'sv': sv,
         'exp': int(time.time()) + config.SESSION_DAYS * 86400}
    )
    return _sign(payload, cfg['secret'])


def cookie_secure(request: Request) -> bool:
    setting = config.SECURE_COOKIE.lower()
    if setting in ('true', '1'):
        return True
    if setting in ('false', '0'):
        return False
    proto = request.headers.get('x-forwarded-proto', request.url.scheme)
    return proto == 'https'


# ── 登录防爆破 ───────────────────────────────────────────
# 按 IP 与按用户名双维度计数：
#   - 按 IP：防单机爆破（配合修正后的真实 IP 解析才有效）
#   - 按用户名：防「换 IP 打同一账号」的分布式爆破
# 注意：两个字典都设有容量上限，避免被大量不同 IP/用户名撑爆内存。
_fail: dict[str, list] = {}
_user_fail: dict[str, list] = {}
MAX_FAILS = 5
LOCK_SECONDS = 600
MAX_TRACKED = 5000


def _prune(store: dict[str, list]) -> None:
    """超限时淘汰**已经过了锁定窗口**的条目，绝不整体清空。

    为什么不能清空：这些字典承载登录失败计数。原实现超限即 `store.clear()`，
    于是攻击者只要用大量不存在的用户名（或伪造来源 IP）把字典灌满，就能
    **顺手把 admin 的锁定计数一起抹掉**，之后可以无限猜密码。
    「宁可放宽也不被撑爆」这个取舍对内存成立，但对**安全计数器**不成立。

    淘汰规则按安全性排序：
      1. 只淘汰 `first` 已超过 LOCK_SECONDS 的条目（它们本来就不再锁定谁）
      2. 仍在锁定窗口内的条目**一条都不动**——哪怕因此略微超出上限
    这样上限不再是硬保证，但超出的部分有界（窗口内最多被灌这么多条），
    而「锁定不可被抹掉」这个安全属性是硬的。上限的意义是防内存爆炸，
    LOCK_SECONDS 只有 10 分钟，窗口内的条目数受请求速率与时间共同约束。
    """
    if len(store) <= MAX_TRACKED:
        return
    now = time.time()
    expired = [k for k, v in store.items() if (now - v[1]) >= LOCK_SECONDS]
    if not expired:
        # 全都在锁定窗口内：不清空、不淘汰。宁可暂时超出上限，也不能放宽锁定。
        return
    # 优先淘汰最旧的过期条目，留一点余量避免每次请求都触发
    for k in sorted(expired, key=lambda k: store[k][1])[: len(expired)]:
        store.pop(k, None)


def login_blocked(ip: str, username: str | None = None) -> bool:
    now = time.time()
    cnt, first = _fail.get(ip, [0, 0.0])
    if cnt >= MAX_FAILS and (now - first) < LOCK_SECONDS:
        return True
    if username:
        key = username.lower()
        ucnt, ufirst = _user_fail.get(key, [0, 0.0])
        if ucnt >= MAX_FAILS and (now - ufirst) < LOCK_SECONDS:
            return True
    return False


def record_fail(ip: str, username: str | None = None) -> None:
    now = time.time()
    cnt, first = _fail.get(ip, [0, now])
    if (now - first) >= LOCK_SECONDS:
        cnt, first = 0, now
    _fail[ip] = [cnt + 1, first]

    if username:
        key = username.lower()
        ucnt, ufirst = _user_fail.get(key, [0, now])
        if (now - ufirst) >= LOCK_SECONDS:
            ucnt, ufirst = 0, now
        _user_fail[key] = [ucnt + 1, ufirst]

    _prune(_fail)
    _prune(_user_fail)


def clear_fail(ip: str, username: str | None = None) -> None:
    _fail.pop(ip, None)
    if username:
        _user_fail.pop(username.lower(), None)


# ── 会话吊销 ─────────────────────────────────────────────
# 每个用户带一个会话版本号 sv（存在 users.json 的该用户条目上，缺省 0）。
# 递增它 = 立即让该用户已签发的所有 cookie 失效。用于：
#   - 改密码后（被盗会话不能继续用）
#   - 改角色后（旧权限快照作废）
#   - 管理员手动「吊销会话」
# 为什么不用全局 epoch：那会连带把其他用户的登录踢掉，属于过度杀伤。
def session_version(cfg: dict, username: str) -> int:
    for u in cfg.get('users', []):
        if u.get('username') == username:
            try:
                return int(u.get('sv') or 0)
            except (TypeError, ValueError):
                return 0
    return 0


def revoke_sessions(username: str) -> int:
    """递增该用户的会话版本，返回新值。其全部既有 cookie 立即失效。"""
    cfg = load_users()
    new_sv = 0
    for u in cfg.get('users', []):
        if u.get('username') == username:
            try:
                new_sv = int(u.get('sv') or 0) + 1
            except (TypeError, ValueError):
                new_sv = 1
            u['sv'] = new_sv
            break
    save_users(cfg)
    return new_sv


def audit(actor: dict | None, action: str, target: str = '', detail: str = '') -> None:
    """记录一条管理端审计日志。

    为什么必须做：本次事故中，攻击者改掉管理员密码、建了自己的账号，而**服务端
    没有留下任何痕迹**——只能靠反代 access log 去猜。管理端是公网入口，
    敏感操作（登录、改密码、增删用户、改安全配置）必须可追溯。

    审计属于旁路，任何异常都不能影响请求本身（与请求日志同样的处理原则）。
    """
    try:
        from . import db
        db.add_audit_log(
            (actor or {}).get('username') or 'anonymous',
            str(action),
            str(target or ''),
            str(detail or '')[:500],
        )
    except Exception:  # noqa: BLE001
        pass


# ── FastAPI 依赖 ─────────────────────────────────────────
def current_user(request: Request) -> dict:
    """解析请求身份。**这是管理端唯一的身份入口**。

    仅接受签名 cookie，并且必须**回查用户表**：

      - role 以表里的为准，不用 cookie 里的快照（否则降权后旧 cookie 仍是管理员）
      - 用户已被删除 → 拒绝（否则删号后其 cookie 在有效期内仍然通行）
      - 会话版本不匹配 → 拒绝（改密码 / 吊销后旧 cookie 立即失效）

    安全事件记录（2026-09-14）：这里**曾经**接受 `X-API-Key` 头，只要它出现在
    `users.json` 的 `api_keys` 数组里就直接授予 admin。那个数组没有任何代码
    去写、没有管理界面，唯一作用就是这条提权后门；而它在早前的路径穿越里
    与 secret 一起泄露，直接导致生产站管理员被改密码。**已彻底移除**：
    管理端身份只认签名 cookie。下游调用请用网关的 `/v1/*`（那套密钥走
    SQLite api_keys 表、只授权模型调用，与后台权限无关）。
    """
    cfg = load_users()
    token = request.cookies.get(config.COOKIE_NAME)
    if not token:
        raise HTTPException(status_code=401, detail='未登录')
    obj = _unsign(token, cfg['secret'])
    if not obj:
        raise HTTPException(status_code=401, detail='未登录')

    username = str(obj.get('username') or '')
    if not username:
        raise HTTPException(status_code=401, detail='未登录')
    row = next((u for u in cfg.get('users', []) if u.get('username') == username), None)
    if not row:
        # 用户已被删除：其 token 不应继续有效
        raise HTTPException(status_code=401, detail='未登录')
    if int(obj.get('sv') or 0) != session_version(cfg, username):
        # 改密码 / 改角色 / 手动吊销之后，旧 token 作废
        raise HTTPException(status_code=401, detail='登录状态已失效，请重新登录')
    return {'username': username, 'role': row.get('role', 'viewer')}


def require_admin(user: dict = Depends(current_user)) -> dict:
    if user.get('role') != 'admin':
        raise HTTPException(status_code=403, detail='需要管理员权限')
    return user
