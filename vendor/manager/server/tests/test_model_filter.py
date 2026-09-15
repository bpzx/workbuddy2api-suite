"""模型清单的「非对话模型」过滤与新字段透出。

背景：上游 2026-09-14 起在它自己的模型解析里加了 `nonChatModel` 过滤——把
嵌入 / 补全 / 代码专用（`nes-` / `completion-` / `codewise-` 前缀）、
输出上限过小（`maxOutputTokens <= 256`）、以及图片生成（tags 含
`text-to-image`）这三类从可选列表剔除，理由是「选了会报 code=11102」。

管理端**直连腾讯**同一接口（比上游多拿显示名与推理档位），所以上游的过滤
不会自动惠及我们——必须自己同步，否则模型中心会列出选不了的东西，
用户点进去必然失败一次。

同时上游新增解析两个字段并透出到 /v1/models：
  * `reasoning.defaultEffort` —— thinking 决策用；空 = 未声明（上游回退硬编码）
  * `supportsImages` —— 多模态能力
"""
from __future__ import annotations

import asyncio
import json
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import modelcatalog, tencent  # noqa: E402


class _Resp:
    def __init__(self, payload) -> None:
        self._p = payload
        self.status_code = 200

    def json(self):
        return self._p


class _Client:
    """返回预置的模型接口响应。"""

    payload: dict = {}

    def __init__(self, *a, **k) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def get(self, url, **kw):
        return _Resp(_Client.payload)

    async def aclose(self):
        return None


def _models_payload(models: list[dict], cli_ids: list[str] | None = None) -> dict:
    return {
        'code': 0,
        'data': {
            'models': models,
            'agents': [{'name': 'cli', 'models': cli_ids or [m['id'] for m in models]}],
        },
    }


class NonChatFilterTest(unittest.TestCase):
    """非对话模型不得进入可选清单（对齐上游 nonChatModel）。"""

    AUTH = {'access_token': 'T', 'realm': 'cn', 'uid': 'u'}

    def setUp(self) -> None:
        p = mock.patch.object(config, 'http_client', _Client)
        p.start()
        self.addCleanup(p.stop)

    def _fetch(self, models, cli_ids=None):
        _Client.payload = _models_payload(models, cli_ids)
        return asyncio.run(tencent.fetch_models(self.AUTH))

    def test_embedding_prefix_filtered(self) -> None:
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'nes-embedding-3', 'maxInputTokens': 8192, 'maxOutputTokens': 4096},
        ])
        self.assertTrue(ok, out)
        ids = [m['id'] for m in out]
        self.assertIn('glm-5.2', ids)
        self.assertNotIn('nes-embedding-3', ids, 'nes- 前缀是嵌入模型，选了报 11102')

    def test_completion_and_codewise_filtered(self) -> None:
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'completion-basic', 'maxInputTokens': 4096, 'maxOutputTokens': 2048},
            {'id': 'codewise-7b', 'maxInputTokens': 4096, 'maxOutputTokens': 2048},
        ])
        ids = [m['id'] for m in out]
        self.assertEqual(ids, ['glm-5.2'], f'补全/代码模型应被过滤，实际 {ids}')

    def test_tiny_output_filtered(self) -> None:
        """输出上限 ≤256 视为 tiny 非对话模型（上游同此判定）。"""
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'tiny-model', 'maxInputTokens': 4096, 'maxOutputTokens': 256},
            {'id': 'tiny-model-2', 'maxInputTokens': 4096, 'maxOutputTokens': 128},
        ])
        ids = [m['id'] for m in out]
        self.assertEqual(ids, ['glm-5.2'], f'输出过小的应被过滤，实际 {ids}')

    def test_text_to_image_filtered(self) -> None:
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
            {'id': 'img-gen', 'maxInputTokens': 4096, 'maxOutputTokens': 4096,
             'tags': ['text-to-image']},
        ])
        ids = [m['id'] for m in out]
        self.assertEqual(ids, ['glm-5.2'], f'图片生成模型应被过滤，实际 {ids}')

    def test_boundary_values_kept(self) -> None:
        """边界：257 输出不算 tiny（上游是 <= 256）；无关 tag 不过滤。"""
        ok, out = self._fetch([
            {'id': 'ok-257', 'maxInputTokens': 8192, 'maxOutputTokens': 257},
            {'id': 'ok-tag', 'maxInputTokens': 8192, 'maxOutputTokens': 4096,
             'tags': ['vision', 'chat']},
        ])
        ids = sorted(m['id'] for m in out)
        self.assertEqual(ids, ['ok-257', 'ok-tag'])

    def test_internal_marker_not_leaked(self) -> None:
        """`_non_chat` 是内部标记，不能出现在返回给前端的字段里。"""
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768},
        ])
        for m in out:
            self.assertNotIn('_non_chat', m, '内部标记漏到响应里了')

    def test_all_filtered_reports_failure(self) -> None:
        """全被过滤时不能返回空清单装作成功。"""
        ok, out = self._fetch([
            {'id': 'nes-embed', 'maxInputTokens': 8192, 'maxOutputTokens': 4096},
        ])
        self.assertFalse(ok)
        self.assertIn('未返回任何可用模型', str(out))

    def test_new_fields_extracted(self) -> None:
        """defaultEffort / supportsImages 要解析出来（上游新增）。"""
        ok, out = self._fetch([
            {'id': 'glm-5.2', 'maxInputTokens': 131072, 'maxOutputTokens': 32768,
             'supportsImages': True,
             'reasoning': {'supportedEfforts': ['low', 'high'],
                           'defaultEffort': 'high'}},
            {'id': 'plain', 'maxInputTokens': 8192, 'maxOutputTokens': 4096},
        ])
        by_id = {m['id']: m for m in out}
        self.assertEqual(by_id['glm-5.2']['default_effort'], 'high')
        self.assertTrue(by_id['glm-5.2']['supports_images'])
        # 未声明时是空值，不能瞎猜一个默认
        self.assertEqual(by_id['plain']['default_effort'], '')
        self.assertFalse(by_id['plain']['supports_images'])


class CatalogFieldPassthroughTest(unittest.TestCase):
    """模型目录要把新字段带到前端（否则 UI 拿不到）。"""

    def test_decorate_keeps_new_fields(self) -> None:
        out = modelcatalog._decorate([
            {'id': 'glm-5.2', 'name': 'GLM', 'context_length': 131072,
             'max_output_tokens': 32768, 'efforts': ['high'],
             'default_effort': 'high', 'supports_images': True},
        ])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]['default_effort'], 'high')
        self.assertTrue(out[0]['supports_images'])

    def test_decorate_defaults_for_upstream_fallback(self) -> None:
        """回退来源（上游 /v1/models）没有这些字段 → 给安全默认值。"""
        out = modelcatalog._decorate([{'id': 'x', 'context_length': 100}])
        self.assertEqual(out[0]['default_effort'], '')
        self.assertFalse(out[0]['supports_images'])


if __name__ == '__main__':
    unittest.main()
