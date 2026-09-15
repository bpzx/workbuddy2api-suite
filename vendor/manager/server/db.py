"""SQLite 存储层：密钥、日志、用量、IP 规则与全局设置。"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from . import config

_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def day_of(ts: int | float | None = None) -> str:
    """把时间戳换算成「哪一天」，全库统一用本地时区。

    必须统一口径：写入用量（bump_usage）与回填用量（backfill_usage_from_logs）
    过去一个用本地日期、一个用 UTC 日期，在 UTC+8 机器上凌晨 00:00-08:00 的调用
    会被算进两个不同的 day，导致回填把同一次调用重复计数。
    展示层（stats.py 的 today/_since）也用本地日期，故此处一律取本地。
    """
    t = time.time() if ts is None else float(ts)
    return time.strftime('%Y-%m-%d', time.localtime(t))


def day_sql(column: str = 'ts') -> str:
    """在 SQL 里按本地时区取日期的表达式（与 day_of 口径一致）。

    注意 SQLite 的 date(ts,'unixepoch') 是 UTC，不能直接用它——那正是
    之前造成口径不一致的原因。这里用 'unixepoch','localtime' 两个修饰符。
    """
    return f"strftime('%Y-%m-%d', {column}, 'unixepoch', 'localtime')"

SCHEMA = """
CREATE TABLE IF NOT EXISTS api_keys (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  name          TEXT    NOT NULL,
  key_hash      TEXT    NOT NULL,
  prefix        TEXT    NOT NULL,
  enabled       INTEGER NOT NULL DEFAULT 1,
  expires_at    INTEGER,
  max_ips       INTEGER NOT NULL DEFAULT 0,
  ip_allowlist  TEXT    NOT NULL DEFAULT '[]',
  models        TEXT    NOT NULL DEFAULT '[]',
  quota         INTEGER NOT NULL DEFAULT 0,
  used_tokens   INTEGER NOT NULL DEFAULT 0,
  created_at    INTEGER NOT NULL,
  last_used_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_keys_prefix ON api_keys(prefix);

CREATE TABLE IF NOT EXISTS api_key_ips (
  key_id     INTEGER NOT NULL,
  ip         TEXT    NOT NULL,
  first_seen INTEGER NOT NULL,
  PRIMARY KEY (key_id, ip)
);

CREATE TABLE IF NOT EXISTS request_logs (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  ts                INTEGER NOT NULL,
  key_id            INTEGER,
  ip                TEXT,
  model             TEXT,
  mapped_model      TEXT,
  status            INTEGER DEFAULT 0,
  prompt_tokens     INTEGER DEFAULT 0,
  completion_tokens INTEGER DEFAULT 0,
  latency_ms        INTEGER DEFAULT 0,
  -- 首字延迟（time-to-first-token，毫秒）：仅流式请求有意义。
  -- 与 latency_ms 不同——后者含模型生成全部内容的耗时，回答越长越大，
  -- 无法反映上游响应速度；首字延迟才是「上游多久开始回话」。
  -- NULL = 未采集到（非流式请求，或该版本之前的历史记录）。
  first_token_ms    INTEGER,
  ua                TEXT,
  error             TEXT,
  stream            INTEGER DEFAULT 0,
  -- 本次调用的真实扣费（来自上游 usage.credit）；NULL = 上游未返回，不等于 0
  credit            REAL
);
CREATE INDEX IF NOT EXISTS idx_logs_ts ON request_logs(ts);

CREATE TABLE IF NOT EXISTS usage_daily (
  day               TEXT    NOT NULL,
  key_id            INTEGER NOT NULL,
  model             TEXT    NOT NULL,
  requests          INTEGER NOT NULL DEFAULT 0,
  prompt_tokens     INTEGER NOT NULL DEFAULT 0,
  completion_tokens INTEGER NOT NULL DEFAULT 0,
  -- 当日实际扣费合计（来自上游 usage.credit；上游未返回时不计入）
  credit            REAL    NOT NULL DEFAULT 0,
  PRIMARY KEY (day, key_id, model)
);

-- 管理端审计日志：登录、改密码、增删用户、改安全配置等敏感操作留痕。
-- 为什么单独一张表：这些操作不产生请求日志（那是网关的），出了事无从追溯。
CREATE TABLE IF NOT EXISTS audit_logs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  actor    TEXT NOT NULL DEFAULT '',   -- 操作者用户名（anonymous = 未认证）
  action   TEXT NOT NULL DEFAULT '',   -- login / update_user / delete_user ...
  target   TEXT NOT NULL DEFAULT '',   -- 被操作对象（如被改的用户名）
  detail   TEXT NOT NULL DEFAULT '',
  ip       TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_audit_ts ON audit_logs(ts);

CREATE TABLE IF NOT EXISTS ip_rules (
  id         INTEGER PRIMARY KEY AUTOINCREMENT,
  kind       TEXT    NOT NULL,
  cidr       TEXT    NOT NULL,
  note       TEXT    NOT NULL DEFAULT '',
  created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS ip_access_logs (
  id      INTEGER PRIMARY KEY AUTOINCREMENT,
  ts      INTEGER NOT NULL,
  ip      TEXT,
  path    TEXT,
  blocked INTEGER NOT NULL DEFAULT 0,
  ua      TEXT
);
CREATE INDEX IF NOT EXISTS idx_ip_logs_ts ON ip_access_logs(ts);

CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT
);

-- 签到 / 保活结果记录。上游只在失败时打日志、成功静默，
-- 因此本表用于留下我们自己触发的签到结果，便于事后追溯。
CREATE TABLE IF NOT EXISTS checkin_logs (
  id       INTEGER PRIMARY KEY AUTOINCREMENT,
  ts       INTEGER NOT NULL,
  uid      TEXT,
  nickname TEXT,
  source   TEXT NOT NULL DEFAULT 'manual',
  kind     TEXT NOT NULL DEFAULT 'checkin',
  success  INTEGER NOT NULL DEFAULT 0,
  code     INTEGER,
  message  TEXT
);
CREATE INDEX IF NOT EXISTS idx_checkin_ts ON checkin_logs(ts);

-- 上游自动任务日志（旅行 / 活跃上报 / 签到 / 保活）的结构化留痕。
-- 上游把这些结果打在容器日志里，容器重建（更新上游）后日志就没了，
-- 所以采集器解析后落到本表长期保留。dedup_key 由「容器日志时间戳 + 行内容」
-- 生成，重复采集同一条日志时用 INSERT OR IGNORE 天然去重。
CREATE TABLE IF NOT EXISTS task_logs (
  id        INTEGER PRIMARY KEY AUTOINCREMENT,
  ts        INTEGER NOT NULL,
  uid       TEXT,
  kind      TEXT NOT NULL,
  level     TEXT NOT NULL DEFAULT 'ok',
  credits   INTEGER NOT NULL DEFAULT 0,
  message   TEXT,
  dedup_key TEXT NOT NULL UNIQUE
);
CREATE INDEX IF NOT EXISTS idx_task_logs_ts ON task_logs(ts);
"""


def _restrict_db_permissions() -> None:
    """把数据库文件（含 WAL/SHM 伴生文件）收紧到仅属主可读写。

    为什么：库里存着 **API 密钥的哈希与前缀、全部请求日志（含来源 IP 与 UA）、
    审计日志**。默认创建的 SQLite 文件是 0644——同主机的其他用户，或任何能读到
    该目录的进程，都能直接读走：密钥前缀可用于针对性爆破，日志则暴露调用方与
    内部拓扑。

    WAL 模式下还有 `-wal` / `-shm` 两个伴生文件，同样含尚未落盘的数据，
    必须一并收紧（只 chmod 主库文件是不够的）。

    Windows 上 chmod 语义有限，失败静默忽略——不影响功能。
    """
    import os
    import stat as _stat
    for suffix in ('', '-wal', '-shm'):
        p = Path(str(config.DB_PATH) + suffix)
        try:
            if p.exists():
                os.chmod(p, _stat.S_IRUSR | _stat.S_IWUSR)
        except OSError:
            pass


def connect() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        config.ensure_dirs()
        _conn = sqlite3.connect(str(config.DB_PATH), check_same_thread=False)
        _conn.row_factory = sqlite3.Row
        _conn.execute('PRAGMA journal_mode=WAL')
        _conn.execute('PRAGMA synchronous=NORMAL')
        _conn.executescript(SCHEMA)
        _migrate(_conn)
        _conn.commit()
        # 建表之后再收紧权限：库文件此刻才确定存在，WAL 伴生文件也在初始化后出现
        _restrict_db_permissions()
    return _conn


# 增量迁移：SQLite 的 CREATE TABLE IF NOT EXISTS 不会给已存在的表补列，
# 因此新增字段必须显式 ALTER。每条用 PRAGMA 检测后再加，可重复执行。
_MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # (表名, 列名, 列定义)
    ('request_logs', 'credit', 'REAL'),
    ('usage_daily', 'credit', 'REAL NOT NULL DEFAULT 0'),
    # 首字延迟：可空（历史记录与非流式请求为 NULL）
    ('request_logs', 'first_token_ms', 'INTEGER'),
)


def _migrate(conn: sqlite3.Connection) -> None:
    for table, column, decl in _MIGRATIONS:
        try:
            cols = {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}
        except sqlite3.Error:
            continue
        if not cols or column in cols:
            continue
        try:
            conn.execute(f'ALTER TABLE {table} ADD COLUMN {column} {decl}')
        except sqlite3.Error:
            # 并发启动时可能已被另一进程加过，忽略即可
            pass


def query(sql: str, args: Iterable[Any] = ()) -> list[sqlite3.Row]:
    with _lock:
        return list(connect().execute(sql, tuple(args)).fetchall())


def query_one(sql: str, args: Iterable[Any] = ()) -> sqlite3.Row | None:
    with _lock:
        return connect().execute(sql, tuple(args)).fetchone()


def execute(sql: str, args: Iterable[Any] = ()) -> int:
    with _lock:
        conn = connect()
        cur = conn.execute(sql, tuple(args))
        conn.commit()
        return int(cur.lastrowid or 0)


def executemany(sql: str, seq: Iterable[Iterable[Any]]) -> None:
    with _lock:
        conn = connect()
        conn.executemany(sql, [tuple(x) for x in seq])
        conn.commit()


# ── settings ─────────────────────────────────────────────
def get_setting(key: str, default: Any = None) -> Any:
    row = query_one('SELECT value FROM settings WHERE key = ?', (key,))
    if not row:
        return default
    try:
        return json.loads(row['value'])
    except Exception:
        return default


def set_setting(key: str, value: Any) -> None:
    execute(
        'INSERT INTO settings(key, value) VALUES(?, ?) '
        'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
        (key, json.dumps(value, ensure_ascii=False)),
    )


# ── 用量累计 ─────────────────────────────────────────────
def bump_usage(
    key_id: int,
    model: str,
    prompt_tokens: int,
    completion_tokens: int,
    credit: float | None = None,
) -> None:
    """累计当日用量。credit 为本次真实扣费，缺省不计入（不按 0 记）。"""
    day = day_of()
    execute(
        'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit) '
        'VALUES(?, ?, ?, 1, ?, ?, ?) '
        'ON CONFLICT(day, key_id, model) DO UPDATE SET '
        '  requests = requests + 1, '
        '  prompt_tokens = prompt_tokens + excluded.prompt_tokens, '
        '  completion_tokens = completion_tokens + excluded.completion_tokens, '
        '  credit = credit + excluded.credit',
        (day, key_id, model, prompt_tokens, completion_tokens, float(credit or 0)),
    )


# ── 签到 / 保活记录 ──────────────────────────────────────
def add_checkin_log(
    uid: str,
    nickname: str,
    source: str,
    success: bool,
    code: int | None = None,
    message: str = '',
    kind: str = 'checkin',
) -> None:
    execute(
        'INSERT INTO checkin_logs(ts, uid, nickname, source, kind, success, code, message) '
        'VALUES(?, ?, ?, ?, ?, ?, ?, ?)',
        (int(time.time()), uid or '', nickname or '', source, kind, 1 if success else 0, code, message),
    )


# days 参数的统一上限。**必须有上限**：超大整数在 SQLite 绑定时会溢出抛错
# （实测 /api/logs?days=999999999999999 返回 500）。db 层是唯一的收敛点，
# 在这里钳一次就覆盖了所有调用方（logs/stats/accounts 各处）。
_DAYS_MAX = 3650


def clamp_days(days: int | None) -> int | None:
    """把 days 钳到 [1, _DAYS_MAX]；None/非法值返回 None（= 不按时间过滤）。"""
    if days is None:
        return None
    try:
        d = int(days)
    except (TypeError, ValueError):
        return None
    return min(_DAYS_MAX, max(1, d))


def _checkin_where(uid: str | None = None, days: int | None = None) -> tuple[str, list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if uid:
        where.append('uid = ?')
        args.append(uid)
    d = clamp_days(days)
    if d:
        where.append('ts >= ?')
        args.append(int(time.time()) - d * 86400)
    return ((' WHERE ' + ' AND '.join(where)) if where else '', args)


def list_checkin_logs(
    limit: int = 200,
    uid: str | None = None,
    *,
    offset: int = 0,
    days: int | None = None,
) -> list[dict]:
    clause, args = _checkin_where(uid, days)
    rows = query(
        f'SELECT * FROM checkin_logs{clause} ORDER BY id DESC LIMIT ? OFFSET ?',
        (*args, min(2000, max(1, limit)), max(0, int(offset))),
    )
    return [
        {
            'id': r['id'],
            'ts': r['ts'],
            'uid': r['uid'],
            'nickname': r['nickname'],
            'source': r['source'],
            'kind': r['kind'],
            'success': bool(r['success']),
            'code': r['code'],
            'message': r['message'],
        }
        for r in rows
    ]


def count_checkin_logs(uid: str | None = None, days: int | None = None) -> int:
    """当前筛选下的总条数（分页用；不传筛选即全量）。"""
    clause, args = _checkin_where(uid, days)
    row = query_one(f'SELECT COUNT(*) AS n FROM checkin_logs{clause}', args)
    return int(row['n']) if row else 0


def clear_checkin_logs() -> None:
    execute('DELETE FROM checkin_logs')


# ── 上游自动任务日志 ─────────────────────────────────────
def add_task_logs(entries: list[dict]) -> int:
    """批量写入自动任务日志，返回**实际新增**条数（重复的按 dedup_key 忽略）。

    用 `INSERT OR IGNORE` + `total_changes` 差值统计，避免反复采集同一批
    日志时把「已存在」也算成新增。
    """
    if not entries:
        return 0
    rows = [
        (
            int(e.get('ts') or 0),
            str(e.get('uid') or ''),
            str(e.get('kind') or ''),
            str(e.get('level') or 'ok'),
            int(e.get('credits') or 0),
            str(e.get('message') or '')[:500],
            str(e.get('dedup_key') or ''),
        )
        for e in entries
    ]
    with _lock:
        conn = connect()
        before = conn.total_changes
        conn.executemany(
            'INSERT OR IGNORE INTO task_logs(ts, uid, kind, level, credits, message, dedup_key) '
            'VALUES(?, ?, ?, ?, ?, ?, ?)',
            rows,
        )
        conn.commit()
        return conn.total_changes - before


def _clean(text: object, limit: int = 500) -> str:
    """把外部文本清成单行：去控制字符并截断。

    为什么必须做：登录取的用户名、网关记的 UA/路径都来自外部输入，若含换行
    就能在日志/审计里**伪造出额外的行**，污染排查与事后追溯。日志是给人看的，
    单行是硬要求。
    """
    t = str(text if text is not None else '')
    # 先按字符过滤控制字符（含 \r \n），再兜底替换残留的转义序列
    t = ''.join(ch for ch in t if ch >= ' ')
    t = t.replace(chr(13), ' ').replace(chr(10), ' ')
    return t[:limit]


def add_audit_log(actor: str, action: str, target: str = '',
                  detail: str = '', ip: str = '') -> None:
    """写一条管理端审计日志。由 security.audit 调用（那里已兜底异常）。"""
    execute(
        'INSERT INTO audit_logs(ts, actor, action, target, detail, ip) VALUES(?, ?, ?, ?, ?, ?)',
        (int(time.time()), _clean(actor, 64), _clean(action, 32),
         _clean(target, 128), _clean(detail, 500), _clean(ip, 64)),
    )


def list_audit_logs(limit: int = 200, offset: int = 0) -> list[dict]:
    rows = query(
        'SELECT * FROM audit_logs ORDER BY id DESC LIMIT ? OFFSET ?',
        (min(1000, max(1, limit)), max(0, int(offset))),
    )
    return [dict(r) for r in rows]


def count_audit_logs() -> int:
    row = query_one('SELECT COUNT(*) AS n FROM audit_logs')
    return int(row['n']) if row else 0


def clear_audit_logs() -> None:
    execute('DELETE FROM audit_logs')


def _task_log_where(
    uid: str | None = None,
    kind: str | None = None,
    days: int | None = None,
) -> tuple[str, list[Any]]:
    where: list[str] = []
    args: list[Any] = []
    if uid:
        where.append('uid = ?')
        args.append(uid)
    if kind:
        where.append('kind = ?')
        args.append(kind)
    d = clamp_days(days)
    if d:
        where.append('ts >= ?')
        args.append(int(time.time()) - d * 86400)
    return ((' WHERE ' + ' AND '.join(where)) if where else '', args)


def list_task_logs(
    limit: int = 200,
    uid: str | None = None,
    kind: str | None = None,
    *,
    offset: int = 0,
    days: int | None = None,
) -> list[dict]:
    clause, args = _task_log_where(uid, kind, days)
    rows = query(
        f'SELECT * FROM task_logs{clause} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?',
        (*args, min(2000, max(1, limit)), max(0, int(offset))),
    )
    return [
        {
            'id': r['id'],
            'ts': r['ts'],
            'uid': r['uid'],
            'kind': r['kind'],
            'level': r['level'],
            'credits': r['credits'],
            'message': r['message'],
        }
        for r in rows
    ]


def count_task_logs(
    uid: str | None = None,
    kind: str | None = None,
    days: int | None = None,
) -> int:
    """当前筛选下的总条数（分页用）。

    分页必须用筛选后的总数：此前界面徽章取的是全局统计，而列表只取前 500 条，
    会出现「徽章说 2200 条、实际只能看到 500 条」且更早记录翻不到的情况。
    """
    clause, args = _task_log_where(uid, kind, days)
    row = query_one(f'SELECT COUNT(*) AS n FROM task_logs{clause}', args)
    return int(row['n']) if row else 0


def task_log_stats(days: int | None = None) -> dict:
    """按类型汇总条数与累计积分，用于页面上方的概览。

    days 让概览与列表的时间范围保持一致，否则筛选后数字会对不上。
    """
    clause, args = _task_log_where(days=days)
    rows = query(
        'SELECT kind, COUNT(*) AS n, COALESCE(SUM(credits), 0) AS credits '
        f'FROM task_logs{clause} GROUP BY kind',
        args,
    )
    by_kind = {r['kind']: {'count': int(r['n']), 'credits': int(r['credits'])} for r in rows}
    total = query_one(
        f'SELECT COUNT(*) AS n, COALESCE(SUM(credits),0) AS credits FROM task_logs{clause}',
        args,
    )
    return {
        'by_kind': by_kind,
        'total': int(total['n']) if total else 0,
        'total_credits': int(total['credits']) if total else 0,
    }


def clear_task_logs() -> None:
    execute('DELETE FROM task_logs')


# ── 用量回填 ─────────────────────────────────────────────
def backfill_usage_from_logs() -> dict:
    """把 request_logs 里尚未计入 usage_daily 的用量补进统计。

    用途：修复历史缺陷（曾因统计函数缺失，导致部分调用的用量没有累计）。
    以「已记录的调用」推算应有用量，再把差额写入 usage_daily，
    因此可重复执行而不会重复计数。
    """
    # 应有用量（按天 × 密钥 × 模型）
    # 必须与 bump_usage 用同一时区口径（本地），否则凌晨的调用会被算成两天
    expected = query(
        f"SELECT {day_sql('ts')} AS day, key_id, COALESCE(model,'') AS model, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "
        "FROM request_logs WHERE key_id IS NOT NULL GROUP BY day, key_id, model"
    )
    current = {
        (r['day'], r['key_id'], r['model']): r
        for r in query(
            'SELECT day, key_id, model, requests, prompt_tokens, completion_tokens, credit FROM usage_daily'
        )
    }

    fixed = 0
    added_requests = added_tokens = 0
    for row in expected:
        key = (row['day'], row['key_id'], row['model'])
        cur = current.get(key)
        cur_req = int(cur['requests']) if cur else 0
        cur_pt = int(cur['prompt_tokens']) if cur else 0
        cur_ct = int(cur['completion_tokens']) if cur else 0

        d_req = int(row['requests']) - cur_req
        d_pt = int(row['pt']) - cur_pt
        d_ct = int(row['ct']) - cur_ct
        if d_req <= 0 and d_pt <= 0 and d_ct <= 0:
            continue
        execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit) '
            'VALUES(?, ?, ?, ?, ?, ?, ?) '
            'ON CONFLICT(day, key_id, model) DO UPDATE SET '
            '  requests = MAX(requests, excluded.requests), '
            '  prompt_tokens = MAX(prompt_tokens, excluded.prompt_tokens), '
            '  completion_tokens = MAX(completion_tokens, excluded.completion_tokens), '
            '  credit = MAX(credit, excluded.credit)',
            (row['day'], row['key_id'], row['model'], int(row['requests']),
             int(row['pt']), int(row['ct']), float(row['cr'] or 0)),
        )
        fixed += 1
        added_requests += max(0, d_req)
        added_tokens += max(0, d_pt) + max(0, d_ct)

    return {
        'repaired': fixed,
        'requests': added_requests,
        'tokens': added_tokens,
    }


def rebuild_usage_from_logs() -> dict:
    """以请求日志为准**重建**用量统计（会替换 usage_daily 的内容）。

    用途：修复历史时区口径不一致造成的污染——凌晨的调用曾被同时算进
    本地日与 UTC 日两行，导致总量偏高。回填（MAX 语义）只能补缺口、
    无法删除多出来的行，因此需要一次重建。

    注意：本操作以 request_logs 为唯一依据。若请求日志曾被清空，
    那部分历史汇总会随之丢失（接口上已明确标注）。
    """
    expected = query(
        f"SELECT {day_sql('ts')} AS day, key_id, COALESCE(model,'') AS model, "
        "COUNT(*) AS requests, COALESCE(SUM(prompt_tokens),0) AS pt, "
        "COALESCE(SUM(completion_tokens),0) AS ct, COALESCE(SUM(credit),0) AS cr "
        "FROM request_logs WHERE key_id IS NOT NULL GROUP BY day, key_id, model"
    )
    before = query_one('SELECT COUNT(*) AS c, COALESCE(SUM(requests),0) AS r, '
                       'COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_daily')
    execute('DELETE FROM usage_daily')
    for row in expected:
        execute(
            'INSERT INTO usage_daily(day, key_id, model, requests, prompt_tokens, completion_tokens, credit) '
            'VALUES(?, ?, ?, ?, ?, ?, ?)',
            (row['day'], row['key_id'], row['model'], int(row['requests']),
             int(row['pt']), int(row['ct']), float(row['cr'] or 0)),
        )
    after = query_one('SELECT COUNT(*) AS c, COALESCE(SUM(requests),0) AS r, '
                      'COALESCE(SUM(prompt_tokens+completion_tokens),0) AS t FROM usage_daily')
    return {
        'rows_before': int(before['c']) if before else 0,
        'rows_after': int(after['c']) if after else 0,
        # 差值可正可负：负数说明此前确实被重复计数了
        'requests_delta': int(after['r'] or 0) - int(before['r'] or 0),
        'tokens_delta': int(after['t'] or 0) - int(before['t'] or 0),
    }
