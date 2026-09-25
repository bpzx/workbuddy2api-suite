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


class GhReasonTest(unittest.TestCase):
    """把 GitHub 异常翻成可读原因。

    这里以前把所有异常吞掉、统一报「仓库里没有正式版 tag」—— 真实原因可能是
    「仓库私有/不存在」或「触发了未认证限流」，与"确实没有 tag"完全是两回事。
    这个假警报真实出现过：面板同时显示了一个具体 tag 和"没有 tag"。
    """

    @staticmethod
    def _status_error(status: int) -> Exception:
        import httpx

        req = httpx.Request('GET', 'https://api.github.com/repos/o/r/tags')
        resp = httpx.Response(status, request=req)
        return httpx.HTTPStatusError('boom', request=req, response=resp)

    def test_404_says_inaccessible(self) -> None:
        self.assertIn('不可访问', suite._gh_reason(self._status_error(404)))

    def test_403_mentions_rate_limit(self) -> None:
        self.assertIn('限流', suite._gh_reason(self._status_error(403)))

    def test_other_status_keeps_code(self) -> None:
        self.assertIn('500', suite._gh_reason(self._status_error(500)))

    def test_network_error_falls_back_to_type_name(self) -> None:
        self.assertIn('ConnectError', suite._gh_reason(RuntimeError('ConnectError: down')))


class FetchReleaseTagTest(unittest.TestCase):
    """`_fetch_release_tag` 返回 (tag, 失败原因)，界面直接显示这个原因。"""

    def test_success_picks_highest_and_has_no_reason(self) -> None:
        async def fake(path: str):
            self.assertIn('/tags', path)
            return [{'name': 'v1.0.9'}, {'name': 'v1.0.10'}, {'name': 'nightly'}]

        with mock.patch.object(suite, '_gh_get', new=mock.AsyncMock(side_effect=fake)):
            tag, reason = _run_async(suite._fetch_release_tag('o/r'))
        self.assertEqual(tag, 'v1.0.10')
        self.assertEqual(reason, '')

    def test_failure_returns_reason_instead_of_empty(self) -> None:
        with mock.patch.object(
            suite, '_gh_get',
            new=mock.AsyncMock(side_effect=RuntimeError('ConnectError: down')),
        ):
            tag, reason = _run_async(suite._fetch_release_tag('o/r'))
        self.assertEqual(tag, '')
        self.assertIn('ConnectError', reason)

    def test_accessible_but_no_release_tag_is_a_distinct_message(self) -> None:
        """仓库能访问、只是没有正式版 tag —— 这才是"没有 tag"，要与查询失败分开。"""
        async def fake(path: str):
            return [{'name': 'nightly'}] if '/tags' in path else {}

        with mock.patch.object(suite, '_gh_get', new=mock.AsyncMock(side_effect=fake)):
            tag, reason = _run_async(suite._fetch_release_tag('o/r'))
        self.assertEqual(tag, '')
        self.assertIn('没有正式版 tag', reason)

    def test_empty_slug_is_reported(self) -> None:
        tag, reason = _run_async(suite._fetch_release_tag(''))
        self.assertEqual(tag, '')
        self.assertIn('未配置', reason)


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
    """读 upstreams.json —— 现在只取 `repo`（用于向 GitHub 查"最新正式版"）。"""

    def _with_file(self, payload) -> dict:
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'upstreams.json'
            f.write_text(json.dumps(payload), encoding='utf-8')
            with mock.patch.object(suite, 'UPSTREAMS_FILE', f):
                return suite.pinned_upstreams()

    def test_exposes_repo_only(self) -> None:
        pins = self._with_file({'upstreams': {
            'wb2api': {'commit': 'x' * 40, 'commit_short': 'x' * 7,
                       'repo': 'Sliverkiss/workbuddy2api'},
            'manager': {'commit': 'y' * 40, 'commit_short': 'y' * 7,
                        'repo': 'ithtelab/workbuddy-manager'},
        }})
        self.assertEqual(pins['manager']['repo'], 'ithtelab/workbuddy-manager')
        # commit / 缩写 / 提交说明那几个字段已随面板那一行的移除而删除：
        # 没有消费方的字段留着就是死数据（完整锁定记录仍以 upstreams.json 为准）
        self.assertEqual(set(pins['manager']), {'repo'})

    def test_degrades_on_missing_or_broken_file(self) -> None:
        """版本提示是辅助信息，文件缺失/损坏不该抛异常把接口带崩。"""
        with mock.patch.object(suite, 'UPSTREAMS_FILE', Path('/nonexistent/up.json')):
            self.assertEqual(suite.pinned_upstreams(), {})


class FetchAllRepoTest(unittest.TestCase):
    """`_fetch_all` 只查套件与 manager 的仓库。"""

    def test_does_not_query_the_deleted_wb2api_repo(self) -> None:
        """wb2api 仓库已删除：再查它只会 404，并被"保留上次成功结果"逻辑**永久**
        留成"有新版本可更新"的假象。这里钉住"不再查它"。"""
        calls: list[str] = []

        async def fake(slug: str):
            calls.append(slug)
            return 'v1.0.0', ''

        up = {'upstreams': {
            'wb2api': {'repo': 'Sliverkiss/workbuddy2api'},
            'manager': {'repo': 'ithtelab/workbuddy-manager'},
        }}
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / 'up.json'
            f.write_text(json.dumps(up), encoding='utf-8')
            with mock.patch.object(suite, 'UPSTREAMS_FILE', f), \
                 mock.patch.dict(os.environ, {'WB_SUITE_REPO': 'o/suite'}), \
                 mock.patch.object(suite, '_fetch_release_tag',
                                   new=mock.AsyncMock(side_effect=fake)):
                out = _run_async(suite._fetch_all())
        self.assertEqual(sorted(calls), ['ithtelab/workbuddy-manager', 'o/suite'])
        self.assertNotIn('Sliverkiss/workbuddy2api', calls)
        self.assertNotIn('wb2api', out)
        # 仓库地址取自 upstreams.json（容器内没有上游 git 仓库，这是唯一来源）
        self.assertEqual(out['manager']['repo'], 'ithtelab/workbuddy-manager')


class CheckTest(unittest.TestCase):
    """check() 的组装逻辑：套件与上游管理端两块。"""

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

    def _fetch(self, **over) -> dict:
        base = {
            'checked_at': 1_700_000_000,
            'suite': {'latest': 'v1.2.4', 'error': '', 'repo': 'o/r'},
            'manager': {'latest': 'v1.0.58', 'error': '',
                        'repo': 'ithtelab/workbuddy-manager'},
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
        out = self._run('v1.2.3', self._fetch())
        self.assertTrue(out['suite']['has_update'])
        self.assertFalse(out['suite']['is_dev'])

    def test_release_current_up_to_date(self) -> None:
        out = self._run('v1.2.4', self._fetch())
        self.assertFalse(out['suite']['has_update'])

    def test_dev_build_can_switch_to_release(self) -> None:
        """main 产出的 sha-* 没有版本号：如实标为开发构建，但存在正式版时算可切换。"""
        out = self._run('sha-a1b2c3d', self._fetch())
        self.assertTrue(out['suite']['is_dev'])
        self.assertTrue(out['suite']['has_update'])

    def test_upstream_manager_uses_its_own_version(self) -> None:
        out = self._run('v1.2.3', self._fetch(), mg_current='v1.0.58')
        self.assertFalse(out['manager']['has_update'])
        out2 = self._run('v1.2.3', self._fetch(), mg_current='v1.0.57')
        self.assertTrue(out2['manager']['has_update'])

    def test_response_no_longer_carries_wb2api(self) -> None:
        """wb2api 那一行已从面板移除，接口也不该再返回它。

        它现在由本项目自行维护（原仓库已删除）；继续返回"上游网关"的版本对比，
        只会让人以为还有个上游在跑。对应的查询逻辑也已删掉（见 FetchAllRepoTest）。
        """
        out = self._run('v1.2.3', self._fetch())
        self.assertNotIn('wb2api', out)

    def test_has_any_aggregates(self) -> None:
        out = self._run('v1.2.4', self._fetch(manager={'latest': 'v1.0.57'}),
                        mg_current='v1.0.57')
        self.assertFalse(out['has_any'])

    def test_uses_cache_unless_forced(self) -> None:
        """未 force 且缓存新鲜时不再请求 GitHub（未授权 API 限流很紧）。"""
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

    def test_cached_failure_keeps_last_good_latest_and_says_so(self) -> None:
        """查询失败时保留上次成功的结果，但必须**说明**这是上次的。

        否则界面会同时显示一个版本号和一条报错、却不解释两者关系 ——
        「仓库里没有正式版 tag」配上具体 tag 就是这么来的（真实出现过的假警报）。
        """
        self.cache.write_text(json.dumps({'checked_at': 1, **self._fetch()}),
                              encoding='utf-8')
        fresh = self._fetch(suite={'latest': '', 'error': 'GitHub 拒绝（可能已限流）'})
        out = self._run('v1.2.3', fresh)
        self.assertEqual(out['suite']['latest'], 'v1.2.4')
        self.assertIn('GitHub 拒绝', out['suite']['error'])
        self.assertIn('上次成功检测', out['suite']['error'])


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
