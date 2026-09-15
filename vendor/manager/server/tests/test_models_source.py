"""模型列表来源判定的回归测试。

背景：上游 `/v1/models` 优先用池里随机一个健康账号**动态拉取**（成功缓存 1h），
**失败则回退到编译进二进制的静态表**，并有 5 分钟负缓存。两者外观一样，
但静态表是写死的、数量少得多。曾出现「只显示 6 个模型」的情况，界面却标
「来自上游实时列表」，让人以为是自己账号或配置坏了——实际是上游在走回退。

判据取上游内部实现细节：动态条目带 `max_output_tokens`（上游 modelList 的
动态分支才写该键），静态表条目没有。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import wb2api  # noqa: E402


def dyn(mid: str) -> dict:
    """动态条目：带 max_output_tokens"""
    return {'id': mid, 'object': 'model', 'created': 1753600000,
            'owned_by': 'workbuddy', 'context_length': 131072,
            'max_output_tokens': 8192}


def static(mid: str) -> dict:
    """静态回退条目：没有 max_output_tokens"""
    return {'id': mid, 'object': 'model', 'created': 1753600000,
            'owned_by': 'workbuddy', 'context_length': 131072}


class ModelsSourceTest(unittest.TestCase):
    def test_dynamic_when_any_entry_has_max_output_tokens(self) -> None:
        self.assertEqual(wb2api.models_source([dyn('glm-5.2'), dyn('kimi-k2.7')]), 'dynamic')

    def test_dynamic_even_if_only_one_entry_has_the_key(self) -> None:
        """上游动态条目都带该键；只要有一条带，整体就是动态。"""
        self.assertEqual(wb2api.models_source([dyn('a'), static('b')]), 'dynamic')

    def test_static_when_no_entry_has_the_key(self) -> None:
        items = [static(x) for x in ('glm-5.2', 'glm-5.1', 'glm-5v-turbo',
                                     'kimi-k2.7', 'minimax-m3', 'hy3')]
        self.assertEqual(wb2api.models_source(items), 'static')

    def test_empty_is_unknown_not_static(self) -> None:
        """空列表不能判定成静态回退——那会把「取不到」误报成「上游在回退」。"""
        self.assertEqual(wb2api.models_source([]), 'unknown')

    def test_non_list_and_garbage_are_unknown(self) -> None:
        for bad in (None, {}, 'x', 42, [1, 2, 3], ['a', None]):
            self.assertEqual(wb2api.models_source(bad), 'unknown', repr(bad))

    def test_entries_without_id_are_unknown(self) -> None:
        """结构不像模型条目时不下结论，交给前端用中性文案。"""
        self.assertEqual(wb2api.models_source([{'foo': 'bar'}]), 'unknown')


if __name__ == '__main__':
    unittest.main()
