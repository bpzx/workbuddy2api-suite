"""更新流程的健壮性回归（上游构建失败相关）。

真实事故：上游某提交删掉了一批脚本、却漏改 Dockerfile（仍 COPY 已删除的
`scripts/probe_active.py`），导致 `docker compose up --build` 失败。管理端
当时只把它当普通命令失败上报，用户看到的是难懂的 checksum 报错，
既不知道原因、也没有退路。

这里覆盖三件事：
  1. Dockerfile 预检能提前查出「引用了不存在的文件」，且不误报；
  2. 固定上游版本（回退手段）的读写与格式校验；
  3. 构建失败诊断能把 docker 报错归纳成人话。
"""
from __future__ import annotations

import pathlib
import sys
import tempfile
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import updater  # noqa: E402

# 复用 worker 里的纯函数（deploy/update.py 是独立脚本，不在包内）
DEPLOY_DIR = pathlib.Path(__file__).resolve().parents[2] / 'deploy'
sys.path.insert(0, str(DEPLOY_DIR))
import update as worker  # noqa: E402


class DockerfilePrecheck(unittest.TestCase):
    """构建前预检：查出 Dockerfile 引用了仓库里不存在的文件。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self._orig = worker.UPSTREAM_DIR
        worker.UPSTREAM_DIR = self.dir

    def tearDown(self) -> None:
        worker.UPSTREAM_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, dockerfile: str, files: list[str]) -> None:
        (self.dir / 'Dockerfile').write_text(dockerfile, encoding='utf-8')
        for f in files:
            path = self.dir / f
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text('', encoding='utf-8')

    def test_detects_missing_copy(self) -> None:
        """复刻真实事故：COPY scripts/probe_active.py 但文件已删除。"""
        self._write(
            'FROM alpine\n'
            'COPY --from=build /out/wb2api /app/wb2api\n'
            'COPY login.sh /app/\n'
            'COPY scripts/probe_active.py /app/scripts/probe_active.py\n',
            ['login.sh'],
        )
        self.assertEqual(worker._missing_copy_sources(), ['scripts/probe_active.py'])

    def test_no_false_positive_when_present(self) -> None:
        self._write('COPY a.py /app/a.py\nCOPY b.py /app/b.py\n', ['a.py', 'b.py'])
        self.assertEqual(worker._missing_copy_sources(), [])

    def test_ignores_stage_copy_wildcard_and_url(self) -> None:
        """阶段拷贝 / 通配符 / URL 不是本地文件，不能误报。"""
        self._write(
            'COPY --from=build /out/x /app/x\n'
            'COPY scripts/*.py /app/scripts/\n'
            'COPY https://example.com/f /app/f\n'
            'RUN echo COPY not.a.copy /tmp/\n',
            [],
        )
        self.assertEqual(worker._missing_copy_sources(), [])

    def test_missing_dockerfile_is_not_an_error(self) -> None:
        self.assertEqual(worker._missing_copy_sources(), [])


class BuildFailureDiagnosis(unittest.TestCase):
    """把 docker 的报错归纳成一句人话。"""

    def test_detects_dockerfile_missing_file(self) -> None:
        out = ('failed to calculate checksum of ref x::y: '
               '"/scripts/probe_active.py": not found')
        hint = worker._diagnose_build_failure(out)
        self.assertIn('不存在的文件', hint)
        self.assertIn('上游', hint)

    def test_detects_disk_and_network(self) -> None:
        self.assertIn('磁盘', worker._diagnose_build_failure('no space left on device'))
        self.assertIn('网络', worker._diagnose_build_failure('dial tcp 1.2.3.4:443: i/o timeout'))
        self.assertIn('权限', worker._diagnose_build_failure('permission denied'))

    def test_unknown_returns_empty(self) -> None:
        self.assertEqual(worker._diagnose_build_failure('some novel error'), '')


class UpstreamRefPinning(unittest.TestCase):
    """固定上游版本（回退手段）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_file = updater.UPSTREAM_REF_FILE
        self._orig_data = config.DATA_DIR
        updater.UPSTREAM_REF_FILE = pathlib.Path(self._tmp.name) / 'ref.txt'
        config.DATA_DIR = pathlib.Path(self._tmp.name)

    def tearDown(self) -> None:
        updater.UPSTREAM_REF_FILE = self._orig_file
        config.DATA_DIR = self._orig_data
        self._tmp.cleanup()

    def test_default_is_follow_branch(self) -> None:
        self.assertEqual(updater.upstream_ref(), '')

    def test_set_and_read_commit(self) -> None:
        self.assertEqual(updater.set_upstream_ref('98b5e160'), '98b5e160')
        self.assertEqual(updater.upstream_ref(), '98b5e160')

    def test_set_and_read_tag(self) -> None:
        self.assertEqual(updater.set_upstream_ref('v1.2.3'), 'v1.2.3')

    def test_full_sha_accepted(self) -> None:
        sha = '4a80249f6ffb7eea087ee86a5e6ade57432bb895'
        self.assertEqual(updater.set_upstream_ref(sha), sha)

    def test_clear_restores_branch_tracking(self) -> None:
        updater.set_upstream_ref('98b5e160')
        self.assertEqual(updater.set_upstream_ref(''), '')
        self.assertEqual(updater.upstream_ref(), '')
        self.assertFalse(updater.UPSTREAM_REF_FILE.exists(), '取消固定应删除文件')

    def test_rejects_injection(self) -> None:
        """该值会进入 git 参数位，必须严格校验。"""
        for bad in ('a;rm -rf /', '$(whoami)', 'a b', 'a\nb', '../x', '-flag', '`id`', 'a|b'):
            with self.assertRaises(ValueError, msg=bad):
                updater.set_upstream_ref(bad)
        self.assertEqual(updater.upstream_ref(), '', '非法输入不应落盘')

    def test_status_exposes_ref(self) -> None:
        updater.set_upstream_ref('98b5e160')
        self.assertEqual(updater.read_status().get('upstream_ref'), '98b5e160')


if __name__ == '__main__':
    unittest.main()
