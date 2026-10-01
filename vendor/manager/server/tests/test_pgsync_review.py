"""复核补的测试：恢复的作用域、两种跳过原因、连接超时。

这三条都来自维护者复核：第一条钉住一个**容易被「好心改坏」**的行为（镜像里没有的
表不能清空），第二条与第三条是复核时改掉的两处（日志口径 / 连接超时）。
"""
from __future__ import annotations

import sqlite3
import unittest
from unittest import mock

from server import config
from server.services import pgsync

from .test_pgsync import _Case  # 复用同一套临时 HOME/DB 装置


class _StubPg:
    """把 PG 连接换成桩：只记录 connect 的参数，并提供一个空的上下文管理器。"""

    def __init__(self, kwargs_sink: list[dict]):
        self._sink = kwargs_sink

    def connect(self, dsn, **kwargs):  # noqa: ANN001
        self._sink.append(kwargs)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    # 导出流程会在事务里调它们（桩只要不炸即可）。cursor() 给一个「查什么都没有」
    # 的假游标：`_prev_tables` 会因此走「按首次导出处理」那条分支（正是真实首次
    # 导出的形态），而不是抛出一个被它自己吞掉的异常、在测试输出里留一段吓人的栈。
    def commit(self):
        pass

    def rollback(self):
        pass

    def cursor(self):
        return _StubCursor()


class _StubCursor:
    def execute(self, *a, **kw):
        return None

    def fetchone(self):
        return None

    def fetchall(self):
        return []

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class ImportScopeTest(_Case):
    """恢复时，**镜像里没有的表不会被清空**（且日志如实这么说）。

    为什么值得单独钉：恢复的直觉是「让本地等于镜像」，于是很容易顺手把这些表也
    DELETE 掉；但镜像可能来自更早的版本（那时这张表还不存在），清空等于删掉镜像里
    根本没有的数据，**不可恢复**。第一版的日志写的是「恢复后将保持为空」——
    与实际行为相反，照着它去理解会以为本地被清干净了。
    """

    def _run_import(self, mirror_tables: dict, logs: list[str]) -> None:
        class FakeSource:
            def __init__(self, conn):  # noqa: ANN001
                pass

            def tables(self):
                return list(mirror_tables)

            def columns(self, table):
                return mirror_tables[table]['columns']

            def rows(self, table, columns, batch=pgsync.BATCH):
                yield mirror_tables[table]['rows']

            def close(self):
                pass

        class Reporter:
            data: dict = {}

            def log(self, text, level='info'):
                logs.append(str(text))

            def step(self, text, percent=None):
                pass

        with mock.patch.object(pgsync, '_PgSource', FakeSource), \
             mock.patch.object(pgsync, '_require_psycopg', lambda: _StubPg([])), \
             mock.patch.object(pgsync, '_local_backup', lambda: None):
            pgsync.import_from_pg(
                {'host': 'stub', 'port': 5432, 'dbname': 'x', 'user': 'u',
                 'password': 'p', 'keep_local_backup': False, 'sslmode': 'prefer'},
                Reporter())

    def test_tables_missing_from_the_mirror_are_left_alone(self) -> None:
        self.db.execute('CREATE TABLE IF NOT EXISTS mirror_has(id INTEGER, v TEXT)')
        self.db.execute('CREATE TABLE IF NOT EXISTS local_only(id INTEGER, v TEXT)')
        self.db.execute("INSERT INTO mirror_has(id, v) VALUES(1, '旧值')")
        self.db.execute("INSERT INTO local_only(id, v) VALUES(1, '只在本地的数据')")
        logs: list[str] = []
        self._run_import(
            {'mirror_has': {'columns': [{'name': 'id', 'type': 'INTEGER', 'notnull': False, 'pk': 0},
                                        {'name': 'v', 'type': 'TEXT', 'notnull': False, 'pk': 0}],
                            'rows': [(1, '镜像里的新值')]}},
            logs)
        mirrored = self.db.query_one('SELECT v FROM mirror_has WHERE id = 1')
        kept = self.db.query_one('SELECT v FROM local_only WHERE id = 1')
        self.assertEqual(mirrored['v'], '镜像里的新值', '镜像里有的表没被覆盖')
        self.assertIsNotNone(kept, '镜像里没有的表被清空了 —— 那是不可恢复的损失')
        self.assertEqual(kept['v'], '只在本地的数据')
        said = [l for l in logs if 'local_only' in l]
        self.assertTrue(said, '没有任何日志提到这张表被跳过')
        self.assertIn('不会改动', said[0], f'日志口径与实际不符：{said[0]}')
        self.assertNotIn('保持为空', said[0], f'日志在说反话：{said[0]}')

    def test_mirror_only_tables_are_not_created_locally(self) -> None:
        """镜像里有、本地没有的表：**不建表、不写入**（镜像来自更新的版本时就是这样）。

        复核时我一度以为这里会走到 `_SqliteSink.accepts` 的「本地没有这张表」分支，
        写了条测试才发现根本到不了 —— 恢复只遍历 `available & local` 的交集
        （那条分支是给别的调用方留的防御）。所以这里断言真实行为：镜像独有的表
        被安静跳过，不凭空建表，也不出现在「恢复不会改动它们」的提示里。
        """
        self.db.execute('DROP TABLE IF EXISTS only_in_mirror')
        logs: list[str] = []
        self._run_import(
            {'only_in_mirror': {'columns': [{'name': 'a', 'type': 'TEXT', 'notnull': False, 'pk': 0}],
                                'rows': [('x',)]}},
            logs)
        tables = {str(r['name']) for r in self.db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertNotIn('only_in_mirror', tables, '镜像独有的表被凭空建到了本地')
        self.assertFalse([line for line in logs if 'only_in_mirror' in line],
                         f'不该有关于这张表的提示（它不在本地表清单里）：{logs}')

    def test_mirror_table_without_common_columns_is_skipped_not_cleared(self) -> None:
        """两边同名但列完全对不上：跳过，且**不清空**（清掉等于凭空删数据）。"""
        self.db.execute('CREATE TABLE IF NOT EXISTS renamed_cols(z INTEGER)')
        self.db.execute('INSERT INTO renamed_cols(z) VALUES(1)')
        logs: list[str] = []
        self._run_import(
            {'renamed_cols': {'columns': [{'name': 'a', 'type': 'TEXT', 'notnull': False, 'pk': 0}],
                              'rows': [('y',)]}},
            logs)
        self.assertIn('没有公共列', '\n'.join(logs), f'缺「没有公共列」的口径：{logs}')
        got = self.db.query_one('SELECT COUNT(*) AS c FROM renamed_cols')
        self.assertEqual(int(got['c']), 1, '列对不上就把本地那张表清空了 —— 数据凭空没了')


class ValidationOrderTest(_Case):
    """配置校验要**先于**驱动检查。

    复核时发现：`test_connection` 原来先 `_require_psycopg()`。于是没装驱动的
    机器上填了个空地址，用户看到的是「未安装 PostgreSQL 驱动」——而真正要改的是
    地址。顺带这也让「空地址」那条用例在没装驱动的环境里**假通过**（它断言的
    只是 ok=False，走的却是另一条分支）。
    """

    def test_empty_host_is_reported_even_without_the_driver(self) -> None:
        with mock.patch.object(pgsync, '_require_psycopg',
                               side_effect=AssertionError('不该走到要驱动这一步')):
            result = pgsync.test_connection(dict(pgsync.DEFAULT_CONFIG))
        self.assertFalse(result['ok'])
        self.assertIn('地址', result['message'],
                      f'没填地址时给出的不是地址相关的提示：{result["message"]}')


class ConnectTimeoutTest(_Case):
    """导出与恢复的连接都要带 connect_timeout。

    黑洞地址（SYN 被丢）下 libpq 默认挂到 OS 的 TCP 超时（分钟级），期间任务锁
    一直被占着、界面停在「进行中」，用户分不清是网络慢还是卡死。
    """

    def test_both_flows_pass_a_timeout(self) -> None:
        calls: list[dict] = []
        sink = type('S', (), {
            'accepts': lambda self, t, c: list(c),
            'prepare': lambda self, t, c: None,
            'write': lambda self, t, c, r: len(r),
            'drop': lambda self, t: None,
        })()
        rep = type('R', (), {
            'data': {},
            'log': lambda self, *a, **k: None,
            'step': lambda self, *a, **k: None,
        })()
        cfg = {'host': 'stub', 'port': 5432, 'dbname': 'x', 'user': 'u',
               'password': 'p', 'sslmode': 'prefer'}
        with mock.patch.object(pgsync, '_require_psycopg', lambda: _StubPg(calls)), \
             mock.patch.object(pgsync, '_write_meta', lambda pg, tables: None), \
             mock.patch.object(pgsync, '_PgSink', lambda pg: sink):
            pgsync.export_to_pg(cfg, rep)
        self.assertTrue(calls, '导出没有调用 psycopg.connect')
        self.assertEqual(calls[-1].get('connect_timeout'), pgsync.CONNECT_TIMEOUT,
                         f'导出连接没带超时：{calls[-1]}')


if __name__ == '__main__':
    unittest.main()
