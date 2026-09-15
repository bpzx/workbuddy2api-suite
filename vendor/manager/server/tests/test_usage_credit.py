"""调用扣费（上游 usage.credit）的采集、迁移与统计回归。

背景：上游 2026-09-13 起在末帧 usage 里带 credit（本次真实扣费），
管理端据此在请求日志与用量统计里展示「实付」。

两个容易出错的地方：
  1. 「上游没返回」与「扣了 0」必须区分——混为一谈会把未知当成免费；
  2. request_logs / usage_daily 都是已存在的表，SQLite 的
     CREATE TABLE IF NOT EXISTS 不会补列，必须显式迁移。
"""
from __future__ import annotations

import pathlib
import sqlite3
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config, db  # noqa: E402
from server.routers import gateway  # noqa: E402


class UsageCreditParsing(unittest.TestCase):
    """_usage_credit 只接受合法的非负数值。"""

    def test_valid_values(self) -> None:
        self.assertEqual(gateway._usage_credit({'credit': 1.5}), 1.5)
        self.assertEqual(gateway._usage_credit({'credit': 0}), 0.0)
        self.assertEqual(gateway._usage_credit({'credit': 0.0004}), 0.0004)
        self.assertEqual(gateway._usage_credit({'credit': 12}), 12.0)

    def test_missing_is_none_not_zero(self) -> None:
        """缺失必须是 None——否则会把「未知」当成「免费」。"""
        for u in ({}, {'credit': None}, None, 'x', []):
            self.assertIsNone(gateway._usage_credit(u), repr(u))

    def test_invalid_values_are_none(self) -> None:
        for u in ({'credit': 'abc'}, {'credit': True}, {'credit': -1}, {'credit': {}}):
            self.assertIsNone(gateway._usage_credit(u), repr(u))


class CreditSchema(unittest.TestCase):
    """两张表都要能自动补上 credit 列（老库迁移）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _reopen(self, path: pathlib.Path) -> None:
        db._conn = None
        config.DB_PATH = path
        db.connect()

    def test_fresh_db_has_credit_columns(self) -> None:
        self._reopen(pathlib.Path(self._tmp.name) / 'new.db')
        for table in ('request_logs', 'usage_daily'):
            cols = {r[1] for r in db.query(f'PRAGMA table_info({table})')}
            self.assertIn('credit', cols, table)

    def test_old_db_gets_migrated_and_keeps_data(self) -> None:
        """模拟升级前已有的库：没有 credit 列。"""
        legacy = pathlib.Path(self._tmp.name) / 'legacy.db'
        conn = sqlite3.connect(str(legacy))
        conn.executescript(
            '''
            CREATE TABLE request_logs (
              id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, key_id INTEGER,
              ip TEXT, model TEXT, mapped_model TEXT, status INTEGER DEFAULT 0,
              prompt_tokens INTEGER DEFAULT 0, completion_tokens INTEGER DEFAULT 0,
              latency_ms INTEGER DEFAULT 0, ua TEXT, error TEXT, stream INTEGER DEFAULT 0);
            CREATE TABLE usage_daily (
              day TEXT NOT NULL, key_id INTEGER NOT NULL, model TEXT NOT NULL,
              requests INTEGER NOT NULL DEFAULT 0,
              prompt_tokens INTEGER NOT NULL DEFAULT 0,
              completion_tokens INTEGER NOT NULL DEFAULT 0,
              PRIMARY KEY (day, key_id, model));
            INSERT INTO request_logs(ts, model, status) VALUES(111, 'glm-5.2', 200);
            '''
        )
        conn.commit()
        conn.close()

        self._reopen(legacy)
        for table in ('request_logs', 'usage_daily'):
            cols = {r[1] for r in db.query(f'PRAGMA table_info({table})')}
            self.assertIn('credit', cols, f'{table} 应被补上 credit 列')
        row = db.query_one('SELECT model, credit FROM request_logs')
        self.assertEqual(row['model'], 'glm-5.2', '迁移不应丢数据')
        self.assertIsNone(row['credit'], '老数据的新列应为 NULL')

    def test_migration_is_idempotent(self) -> None:
        path = pathlib.Path(self._tmp.name) / 'again.db'
        self._reopen(path)
        for _ in range(3):
            db._conn.close()
            self._reopen(path)
        cols = [r[1] for r in db.query('PRAGMA table_info(request_logs)')]
        self.assertEqual(cols.count('credit'), 1, '重复迁移不应产生重复列')


class CreditRecording(unittest.TestCase):
    """扣费入库与累计。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.DB_PATH
        config.DB_PATH = pathlib.Path(self._tmp.name) / 'rec.db'
        db._conn = None
        db.connect()
        db.execute(
            'INSERT INTO api_keys(name, key_hash, prefix, enabled, expires_at, max_ips, '
            'ip_allowlist, models, quota, used_tokens, created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
            ('k', 'h', 'wbk_abc', 1, None, 0, '[]', '[]', 0, 0, int(time.time())),
        )
        self.key = {'id': 1}

    def tearDown(self) -> None:
        try:
            if db._conn is not None:
                db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = None
        config.DB_PATH = self._orig
        self._tmp.cleanup()

    def _record(self, credit):
        gateway._record(self.key, '1.2.3.4', 'glm-5.2', 'glm-5.2', 200,
                        100, 50, 10, 'ua', None, False, credit=credit)

    def test_credit_stored_and_null_distinguished(self) -> None:
        self._record(1.25)
        self._record(None)
        rows = db.query('SELECT credit FROM request_logs ORDER BY id')
        self.assertEqual(float(rows[0]['credit']), 1.25)
        self.assertIsNone(rows[1]['credit'], '未返回扣费应存 NULL 而非 0')

    def test_credited_zero_is_a_real_zero(self) -> None:
        """上游明确返回 0（免费号）要存 0，不能与缺失混淆。"""
        self._record(0)
        self.assertEqual(float(db.query_one('SELECT credit FROM request_logs')['credit']), 0.0)

    def test_daily_accumulates_credit(self) -> None:
        self._record(1.25)
        self._record(0.75)
        row = db.query_one('SELECT requests, credit FROM usage_daily')
        self.assertEqual(int(row['requests']), 2)
        self.assertAlmostEqual(float(row['credit']), 2.0, places=6)

    def test_no_credit_does_not_break_usage(self) -> None:
        """上游未返回扣费时，请求数/Tokens 仍要正常累计。"""
        self._record(None)
        row = db.query_one('SELECT requests, prompt_tokens, completion_tokens, credit FROM usage_daily')
        self.assertEqual(int(row['requests']), 1)
        self.assertEqual(int(row['prompt_tokens']), 100)
        self.assertEqual(float(row['credit']), 0.0)

    def test_rebuild_preserves_credit(self) -> None:
        """重建统计不能把扣费清零。"""
        self._record(3.5)
        self._record(1.5)
        db.rebuild_usage_from_logs()
        row = db.query_one('SELECT credit FROM usage_daily')
        self.assertAlmostEqual(float(row['credit']), 5.0, places=6)


if __name__ == '__main__':
    unittest.main()
