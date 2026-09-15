"""请求体上限的回归（跟随上游配置 + 读取时强制截断）。

真实问题：用户通过反代调用时报「请求体过大（上限 8 MiB）」，但上游
`server.max_body_mb` 是**可配置**的（管理端设置页也能改）。本端当时把它写死
8 MiB，于是成了隐性瓶颈——用户调大上游上限后，请求仍在本端先被 413 拦掉。

另一个漏洞：原先只看 `Content-Length` 判断大小。分块传输时该头缺失、也
可以伪造，因此超大请求体仍会被整段读进内存（正是当初要防的内存耗尽）。
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import sys
import tempfile
import unittest

from starlette.requests import Request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.routers import gateway  # noqa: E402


def _reset_cache() -> None:
    gateway._body_limit_cache.update({'at': 0.0, 'bytes': 0})


def _request(body: bytes, headers: dict | None = None) -> Request:
    scope = {
        'type': 'http',
        'method': 'POST',
        'path': '/v1/chat/completions',
        'headers': [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
    }
    sent = {'done': False}

    async def receive():
        if sent['done']:
            return {'type': 'http.disconnect'}
        sent['done'] = True
        return {'type': 'http.request', 'body': body, 'more_body': False}

    return Request(scope, receive)


class BodyLimitResolution(unittest.TestCase):
    """上限应跟随上游 server.max_body_mb。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg
        _reset_cache()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        _reset_cache()
        self._tmp.cleanup()

    def _mb(self, value) -> int:
        if value == 'missing':
            self.cfg.unlink(missing_ok=True)
        else:
            self.cfg.write_text(json.dumps(value), encoding='utf-8')
        _reset_cache()
        return gateway.max_body_bytes() // 1024 // 1024

    def test_follows_config(self) -> None:
        self.assertEqual(self._mb({'server': {'max_body_mb': 32}}), 32)
        self.assertEqual(self._mb({'server': {'max_body_mb': 64}}), 64)

    def test_missing_config_falls_back(self) -> None:
        """读不到配置不能导致拒绝服务，退回默认 8 MiB。"""
        self.assertEqual(self._mb('missing'), gateway.DEFAULT_MAX_BODY_MB)

    def test_invalid_values_fall_back(self) -> None:
        for bad in ({'server': {}}, {'server': {'max_body_mb': 0}},
                    {'server': {'max_body_mb': -5}}, {'server': {'max_body_mb': 'x'}}, {}):
            self.assertEqual(self._mb(bad), gateway.DEFAULT_MAX_BODY_MB, repr(bad))

    def test_result_is_cached_then_refreshes(self) -> None:
        self.assertEqual(self._mb({'server': {'max_body_mb': 32}}), 32)
        # 改文件但不清缓存 → 仍读旧值
        self.cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
        self.assertEqual(gateway.max_body_bytes() // 1024 // 1024, 32, 'TTL 内应命中缓存')
        # 缓存过期 → 生效新值
        gateway._body_limit_cache['at'] = 0.0
        self.assertEqual(gateway.max_body_bytes() // 1024 // 1024, 1)


class BodyLimitEnforcement(unittest.TestCase):
    """大小校验必须落在实际读取上，而不只是信任 Content-Length。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = pathlib.Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg
        # 设为 1 MiB，便于构造超限体
        self.cfg.write_text(json.dumps({'server': {'max_body_mb': 1}}), encoding='utf-8')
        _reset_cache()
        self.big = json.dumps(
            {'model': 'x', 'messages': [{'role': 'user', 'content': 'a' * (2 * 1024 * 1024)}]}
        ).encode()
        self.small = json.dumps({'model': 'x', 'messages': []}).encode()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        _reset_cache()
        self._tmp.cleanup()

    def _read(self, body: bytes, headers: dict | None = None):
        return asyncio.run(gateway._read_json_body(_request(body, headers)))

    def test_too_large_with_content_length(self) -> None:
        _, err = self._read(self.big, {'content-length': str(len(self.big))})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_too_large_without_content_length(self) -> None:
        """分块/缺头时原先可绕过，现在必须也拦。"""
        _, err = self._read(self.big, {})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_too_large_with_forged_content_length(self) -> None:
        """伪造偏小的 Content-Length 不能绕过。"""
        _, err = self._read(self.big, {'content-length': '100'})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 413)

    def test_small_body_passes(self) -> None:
        body, err = self._read(self.small, {'content-length': str(len(self.small))})
        self.assertIsNone(err)
        self.assertIsInstance(body, dict)

    def test_non_json_is_400(self) -> None:
        _, err = self._read(b'not json', {'content-length': '8'})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_non_object_is_400(self) -> None:
        raw = json.dumps([1, 2, 3]).encode()
        _, err = self._read(raw, {'content-length': str(len(raw))})
        self.assertIsNotNone(err)
        self.assertEqual(err.status_code, 400)

    def test_error_message_has_guidance(self) -> None:
        """报错要说清怎么改，而不是只丢一个 413。"""
        _, err = self._read(self.big, {})
        msg = json.loads(err.body)['error']['message']
        self.assertIn('上限', msg)
        self.assertIn('max_body_mb', msg)


if __name__ == '__main__':
    unittest.main()
