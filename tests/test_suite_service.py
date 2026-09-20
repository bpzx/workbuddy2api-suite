"""本发行版自己的后端逻辑测试（套件版本语义、tag 挑选、检查结果组装）。

跑法（与 CI 一致）——必须让 PYTHONPATH 指向**打过补丁**的 manager 树，
因为被测的 `server/services/suite.py` 正是由 apply.py 落位进去的：

    cp -r vendor/manager /tmp/patched
    python docker/patches/apply.py /tmp/patched
    PYTHONPATH=/tmp/patched/manager python -m unittest discover -s tests -t tests -v

刻意**不用** try/except 包住 import：拿不到模块就应该直接报错。
若改成 skip，PYTHONPATH 配错时测试会"全绿"，正是上游测试里反复防的那种空转。
"""
from __future__ import annotations

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# 顶层是 tests/ 时，仓库根就是上一级
_REPO = Path(__file__).resolve().parents[1]

from server.services import suite  # noqa: E402


class SemverTest(unittest.TestCase):
    def test_parses_common_forms(self) -> None:
        self.assertEqual(suite.parse_semver('v1.2.3'), (1, 2, 3))
        self.assertEqual(suite.parse_semver('1.2.3'), (1, 2, 3))
        self.assertEqual(suite.parse_semver('v1.2'), (1, 2))
        self.assertEqual(suite.parse_semver('  v10.0.1 '), (10, 0, 1))

    def test_rejects_non_versions(self) -> None:
        """sha-* / dev / unknown / 预发布后缀都不是"正式版"，必须拒绝。

        CI 在 push main 时产出 sha-xxxxxxx 镜像标签，若不拒绝，它们会被当成
        版本号参与比较，于是每次提交都变成"有新版本"，纯噪音。
        """
        for bad in ('sha-a1b2c3d', 'dev', 'unknown', '', 'latest', 'v1.2.3-rc1'):
            with self.subTest(bad=bad):
                self.assertIsNone(suite.parse_semver(bad))

    def test_is_dev_version(self) -> None:
        self.assertTrue(suite.is_dev_version('sha-a1b2c3d'))
        self.assertTrue(suite.is_dev_version('unknown'))
        self.assertFalse(suite.is_dev_version('v1.2.3'))

    def test_newer_requires_strictly_greater(self) -> None:
        self.assertTrue(suite.version_newer('v1.2.4', 'v1.2.3'))
        self.assertTrue(suite.version_newer('v1.3', 'v1.2.9'))
        # 相等与降级都**不能**算更新：否则回滚场景会冒出「v1.2.4 → v1.2.3」
        self.assertFalse(suite.version_newer('v1.2.3', 'v1.2.3'))
        self.assertFalse(suite.version_newer('v1.2.3', 'v1.2.4'))
        # 缺位补 0 的语义
        self.assertFalse(suite.version_newer('v1.2', 'v1.2.0'))
        self.assertTrue(suite.version_newer('v1.2.1', 'v1.2'))

    def test_newer_is_false_when_unparsable(self) -> None:
        """任一端解析不了就返回 False —— 宁可不提示，也不误报。"""
        self.assertFalse(suite.version_newer('sha-abc', 'v1.2.3'))
        self.assertFalse(suite.version_newer('v1.2.3', 'unknown'))
        self.assertFalse(suite.version_newer('', ''))


class HighestReleaseTagTest(unittest.TestCase):
    def test_picks_max_not_first(self) -> None:
        """GitHub 的 /tags 按提交时间倒序，不是按版本号，所以必须逐个比。"""
        tags = ['v1.0.9', 'v1.10.0', 'v1.2.0', 'v1.9.9']
        self.assertEqual(suite.highest_release_tag(tags), 'v1.10.0')

    def test_ignores_non_release_tags(self) -> None:
        tags = ['nightly', 'sha-1234567', 'v1.0.0', 'release-2', '2026-09-20']
        self.assertEqual(suite.highest_release_tag(tags), 'v1.0.0')

    def test_empty_when_no_release_tag(self) -> None:
        self.assertEqual(suite.highest_release_tag(['nightly', 'sha-abc']), '')

    def test_tolerates_junk_entries(self) -> None:
        self.assertEqual(suite.highest_release_tag(['', None, 'v2', 'v1.9']), 'v2')  # type: ignore[list-item]


class CurrentVersionTest(unittest.TestCase):
    def test_env_wins(self) -> None:
        with mock.patch.dict(os.environ, {'WB_SUITE_VERSION': 'v1.2.3'}):
            self.assertEqual(suite.current_version(), 'v1.2.3')

    def test_falls_back_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'version'
            f.write_text('v9.9.9\n', encoding='utf-8')
            with mock.patch.dict(os.environ, {'WB_SUITE_VERSION': ''}), \
                 mock.patch.object(suite, 'SUITE_VERSION_FILE', f):
                self.assertEqual(suite.current_version(), 'v9.9.9')

    def test_unknown_when_nothing_available(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with mock.patch.dict(os.environ, {'WB_SUITE_VERSION': ''}), \
                 mock.patch.object(suite, 'SUITE_VERSION_FILE', Path(td) / 'missing'):
                self.assertEqual(suite.current_version(), 'unknown')


class PinnedUpstreamsTest(unittest.TestCase):
    """容器里没有上游 git 仓库，upstreams.json 是唯一正确的事实来源。"""

    def _with_file(self, payload) -> dict:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'upstreams.json'
            f.write_text(json.dumps(payload), encoding='utf-8')
            with mock.patch.object(suite, 'UPSTREAMS_FILE', f):
                return suite.pinned_upstreams()

    def test_reads_pins(self) -> None:
        pins = self._with_file({'upstreams': {
            'wb2api': {'commit': 'b08f518c9bb2', 'commit_short': 'b08f518',
                       'repo': 'Sliverkiss/workbuddy2api', 'subject': 'x'},
            'manager': {'commit': '8bc9b0d95975', 'commit_short': '8bc9b0d',
                        'repo': 'ithtelab/workbuddy-manager'},
        }})
        self.assertEqual(pins['wb2api']['short'], 'b08f518')
        self.assertEqual(pins['manager']['repo'], 'ithtelab/workbuddy-manager')

    def test_derives_short_from_commit(self) -> None:
        pins = self._with_file({'upstreams': {'wb2api': {'commit': 'abcdef1234567890'}}})
        self.assertEqual(pins['wb2api']['short'], 'abcdef1')

    def test_degrades_on_missing_or_broken_file(self) -> None:
        """版本提示是辅助信息，文件缺失/损坏不该抛异常把接口带崩。"""
        with mock.patch.object(suite, 'UPSTREAMS_FILE', Path('/nonexistent/up.json')):
            self.assertEqual(suite.pinned_upstreams(), {})


class CheckTest(unittest.TestCase):
    """check() 的组装逻辑：三块各自的 has_update 语义。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.cache = Path(self._td.name) / 'suite-check.json'
        self.patches = [
            mock.patch.object(suite, 'CHECK_CACHE_FILE', self.cache),
            mock.patch.object(suite, 'UPSTREAMS_FILE', Path(self._td.name) / 'up.json'),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])
        self.addCleanup(self._td.cleanup)

    def _pin(self, wb2api_short: str, manager: str = 'ithtelab/workbuddy-manager') -> None:
        (Path(self._td.name) / 'up.json').write_text(json.dumps({'upstreams': {
            'wb2api': {'commit': 'x' * 40, 'commit_short': wb2api_short,
                       'repo': 'Sliverkiss/workbuddy2api'},
            'manager': {'commit': 'y' * 40, 'commit_short': 'y' * 7, 'repo': manager},
        }}), encoding='utf-8')

    def _fetch(self, **over):
        base = {
            'checked_at': 1_700_000_000,
            'suite': {'latest': 'v1.2.4', 'error': '', 'repo': 'o/r'},
            'wb2api': {'latest': 'e4f5g6h', 'date': '', 'subject': 's',
                       'error': '', 'repo': 'Sliverkiss/workbuddy2api'},
            'manager': {'latest': 'v1.0.58', 'error': '', 'repo': 'ithtelab/workbuddy-manager'},
        }
        for k, v in over.items():
            base[k] = {**base[k], **v}
        return base

    def _run(self, current: str, fetch: dict, mg_current: str = 'v1.0.57'):
        with mock.patch.dict(os.environ, {'WB_SUITE_VERSION': current}), \
             mock.patch.object(suite, '_fetch_all', new=mock.AsyncMock(return_value=fetch)), \
             mock.patch.object(suite.updater, 'current_version', return_value=mg_current):
            return _run_async(suite.check(force=True))

    def test_release_current_uses_semver_compare(self) -> None:
        self._pin('b08f518')
        out = self._run('v1.2.3', self._fetch())
        self.assertTrue(out['suite']['has_update'])
        self.assertFalse(out['suite']['is_dev'])

    def test_release_current_up_to_date(self) -> None:
        self._pin('b08f518')
        out = self._run('v1.2.4', self._fetch())
        self.assertFalse(out['suite']['has_update'])

    def test_dev_build_can_switch_to_release(self) -> None:
        """main 产出的 sha-* 没有版本号：如实标为开发构建，但存在正式版时算可切换。"""
        self._pin('b08f518')
        out = self._run('sha-a1b2c3d', self._fetch())
        self.assertTrue(out['suite']['is_dev'])
        self.assertTrue(out['suite']['has_update'])

    def test_upstream_gateway_compares_commits(self) -> None:
        self._pin('b08f518')
        same = self._run('v1.2.3', self._fetch(wb2api={'latest': 'b08f518'}))
        self.assertFalse(same['wb2api']['has_update'])
        diff = self._run('v1.2.3', self._fetch(wb2api={'latest': 'e4f5g6h'}))
        self.assertTrue(diff['wb2api']['has_update'])

    def test_upstream_gateway_no_pin_is_not_an_update(self) -> None:
        """读不到固定 commit 时不能报"有更新"（避免假阳性）。"""
        out = self._run('v1.2.3', self._fetch())
        self.assertEqual(out['wb2api']['current'], '')
        self.assertFalse(out['wb2api']['has_update'])

    def test_upstream_manager_uses_its_own_version(self) -> None:
        self._pin('b08f518')
        out = self._run('v1.2.3', self._fetch(), mg_current='v1.0.58')
        self.assertFalse(out['manager']['has_update'])
        out2 = self._run('v1.2.3', self._fetch(), mg_current='v1.0.57')
        self.assertTrue(out2['manager']['has_update'])

    def test_has_any_aggregates(self) -> None:
        self._pin('b08f518')
        out = self._run('v1.2.4', self._fetch(wb2api={'latest': 'b08f518'},
                                             manager={'latest': 'v1.0.57'}),
                        mg_current='v1.0.57')
        self.assertFalse(out['has_any'])

    def test_uses_cache_unless_forced(self) -> None:
        """未 force 且缓存新鲜时不再请求 GitHub（未授权 API 限流很紧）。"""
        self._pin('b08f518')
        # checked_at 必须是"刚刚"：否则缓存过期，走的是重新拉取的分支
        self.cache.write_text(
            json.dumps({**self._fetch(), 'checked_at': int(time.time())}),
            encoding='utf-8',
        )
        fetch = mock.AsyncMock(return_value=self._fetch())
        with mock.patch.dict(os.environ, {'WB_SUITE_VERSION': 'v1.2.3'}), \
             mock.patch.object(suite, '_fetch_all', new=fetch), \
             mock.patch.object(suite.updater, 'current_version', return_value='v1.0.57'):
            out = _run_async(suite.check())
        fetch.assert_not_awaited()
        self.assertTrue(out['cached'])

    def test_cached_failure_keeps_last_good_latest(self) -> None:
        """临时网络故障不该让界面从"有新版本"掉成"未知"。"""
        self._pin('b08f518')
        self.cache.write_text(json.dumps({
            'checked_at': 1, **self._fetch(),
        }), encoding='utf-8')
        fresh = self._fetch(suite={'latest': '', 'error': '网络不通'})
        out = self._run('v1.2.3', fresh)
        self.assertEqual(out['suite']['latest'], 'v1.2.4')
        self.assertEqual(out['suite']['error'], '网络不通')


def _run_async(coro):
    """跑一个协程，不依赖 pytest-asyncio。"""
    import asyncio

    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


if __name__ == '__main__':
    unittest.main()
