"""更新前可见的 Release 正文与包内上游标识（issue #217）。"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import updater


class ReleaseNotesTest(unittest.TestCase):
    def test_release_body_and_upstream_marker_are_exposed(self) -> None:
        uid = 'a' * 64
        body = '### 新增\n\n- 需要配套上游\n\n<!-- upstream-id: %s -->' % uid
        rel = {'tag_name': 'v9.9.9', 'html_url': 'https://example/rel',
               'published_at': '2026-10-18T00:00:00Z', 'assets': [],
               'body': body}
        with mock.patch.object(updater, '_gh_get', return_value=rel):
            out = updater._fetch_remote_versions()
        mgr = out['manager']
        self.assertIn('需要配套上游', mgr['release_notes'])
        self.assertNotIn('upstream-id', mgr['release_notes'], '隐藏标记不该显示给用户')
        self.assertEqual(mgr['upstream_id'], uid)
        self.assertEqual(mgr['published_at'], '2026-10-18T00:00:00Z')

    def test_missing_marker_is_empty_not_guessed(self) -> None:
        rel = {'tag_name': 'v9.9.9', 'assets': [], 'body': '普通说明'}
        with mock.patch.object(updater, '_gh_get', return_value=rel):
            out = updater._fetch_remote_versions()
        self.assertEqual(out['manager']['upstream_id'], '')

    def test_local_upstream_id_reads_pack_marker(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / '.upstream-id').write_text('b' * 64 + '\n', encoding='utf-8')
            with mock.patch.object(updater, '_upstream_dir', return_value=Path(tmp)):
                self.assertEqual(updater._local_upstream_id(), 'b' * 64)
        with mock.patch.object(updater, '_upstream_dir', return_value=Path(tmp) / 'missing'):
            self.assertEqual(updater._local_upstream_id(), '')


if __name__ == '__main__':
    unittest.main()
