"""`check_device_token.py` 的测试（启动时报告缺设备风控凭据的账号数）。

判据必须与 Go 侧读取一致：`device_token` 在 auth 文件的**顶层**，扁平形与
「插件 OAuth 嵌套形」都如此（`vendor/wb2api/internal/auth/auth.go`）。
嵌在 `auth` 段**里面**的 token 上游读不到，所以这里也必须算作"缺"——
否则我们会告诉使用者"没问题"，而实际上网关不会发送 `X-Device-Token`。
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_CHECK = _REPO / 'docker' / 'overlay' / 'check_device_token.py'


def _load():
    spec = importlib.util.spec_from_file_location('suite_device_token_check', str(_CHECK))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


check = _load()


class FindMissingTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)

    def _write(self, name: str, payload) -> None:
        p = self.dir / name
        if isinstance(payload, str):
            p.write_text(payload, encoding='utf-8')
        else:
            p.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')

    def test_flat_form_with_token_is_ok(self) -> None:
        self._write('a.json', {'uid': 'u1', 'device_token': 'tk'})
        self.assertEqual(check.find_missing(self.dir), ([], 1))

    def test_flat_form_without_token_is_missing(self) -> None:
        self._write('a.json', {'uid': 'u1'})
        missing, total = check.find_missing(self.dir)
        self.assertEqual(missing, ['a'])
        self.assertEqual(total, 1)

    def test_nested_form_top_level_token_is_ok(self) -> None:
        """插件 OAuth 嵌套形：device_token 与 auth/account 平级。"""
        self._write('b.json', {'auth': {'accessToken': 'x'}, 'account': {'uid': 'u1'},
                               'device_token': 'tk'})
        self.assertEqual(check.find_missing(self.dir), ([], 1))

    def test_token_nested_inside_auth_counts_as_missing(self) -> None:
        """嵌在 auth 段里上游读不到 —— 必须算缺失，不能给使用者假安心。"""
        self._write('c.json', {'auth': {'accessToken': 'x', 'device_token': 'tk'},
                               'account': {'uid': 'u1'}})
        missing, _ = check.find_missing(self.dir)
        self.assertEqual(missing, ['c'])

    def test_blank_token_counts_as_missing(self) -> None:
        for raw in ('', '   ', '\n'):
            with self.subTest(raw=repr(raw)):
                self._write('d.json', {'device_token': raw})
                missing, _ = check.find_missing(self.dir)
                self.assertEqual(missing, ['d'])

    def test_broken_json_is_skipped_not_reported_missing(self) -> None:
        """坏文件由别的检查负责报；这里不猜，也不能因此谎报"缺凭据"。"""
        self._write('good.json', {'device_token': 'tk'})
        self._write('broken.json', '{ not json')
        missing, total = check.find_missing(self.dir)
        self.assertEqual(missing, [])
        self.assertEqual(total, 2, '总数应包含所有 *.json（含坏的）')

    def test_only_json_files_counted(self) -> None:
        self._write('a.json', {'device_token': 'tk'})
        (self.dir / 'notes.txt').write_text('x', encoding='utf-8')
        (self.dir / 'sub').mkdir()
        _, total = check.find_missing(self.dir)
        self.assertEqual(total, 1)

    def test_empty_dir(self) -> None:
        self.assertEqual(check.find_missing(self.dir), ([], 0))

    def test_missing_dir_is_not_an_error(self) -> None:
        """目录不存在（首次启动）不能报错，也不能崩。"""
        self.assertEqual(check.find_missing(self.dir / 'nope'), ([], 0))


class MainOutputTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)

    def _run(self, *args: str) -> tuple[int, str]:
        err = StringIO()
        with redirect_stderr(err):
            rc = check.main(['check_device_token.py', *args])
        return rc, err.getvalue()

    def test_silent_when_all_have_token(self) -> None:
        (self.dir / 'a.json').write_text(json.dumps({'device_token': 'tk'}), encoding='utf-8')
        rc, out = self._run(str(self.dir))
        self.assertEqual(rc, 0)
        self.assertEqual(out, '', '全部带凭据时不该输出（避免刷屏）')

    def test_reports_count_when_some_missing(self) -> None:
        (self.dir / 'a.json').write_text(json.dumps({'device_token': 'tk'}), encoding='utf-8')
        (self.dir / 'b.json').write_text(json.dumps({'uid': 'u'}), encoding='utf-8')
        rc, out = self._run(str(self.dir))
        self.assertEqual(rc, 0, '提示性检查永远返回 0，不能让容器起不来')
        self.assertIn('1/2', out)
        self.assertIn('device_token', out)

    def test_never_prints_token_values(self) -> None:
        """日志里不能出现凭据本身 —— 只报数量。"""
        (self.dir / 'a.json').write_text(
            json.dumps({'device_token': 'SUPER-SECRET-TOKEN'}), encoding='utf-8')
        (self.dir / 'b.json').write_text(json.dumps({'uid': 'u'}), encoding='utf-8')
        _, out = self._run(str(self.dir))
        self.assertNotIn('SUPER-SECRET-TOKEN', out)

    def test_silent_on_missing_dir(self) -> None:
        rc, out = self._run(str(self.dir / 'nope'))
        self.assertEqual((rc, out), (0, ''))


if __name__ == '__main__':
    unittest.main()
