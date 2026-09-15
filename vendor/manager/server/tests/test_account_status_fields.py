"""账号运行状态字段的透传（冷却剩余时间 / 被限流模型清单）。

背景：上游 `/status` 一直提供 `cool_remaining_sec`（冷却剩余秒数）与
`rate_limited_models`（被限流的模型清单），但管理端只取了 `cooling` 布尔值，
界面上只显示「冷却中」——用户既不知道要等多久，也不知道是哪个模型被限。

上游 2026-09-15 的改动让冷却时长**对齐上游明说的重置时刻**（不再靠固定基数
+ 指数退避猜），这个值的可信度进一步提高，值得展示出来。

本文件锁住：这两个字段要如实透传，且**缺省/异常输入不崩**（上游旧版本或
字段缺失时界面仍要正常）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server.services import wb2api  # noqa: E402


class CoolRemainingPassthroughTest(unittest.TestCase):
    """cool_remaining_sec 应透传为正数秒；缺失/0/负数 → None（不显示倒计时）。"""

    def _merge(self, pool_item: dict) -> dict:
        accounts = [{'uid': 'u1'}]
        wb2api.merge_pool_status(accounts, {'accounts': [dict(pool_item, uid='u1')]})
        return accounts[0]

    def test_positive_seconds_passthrough(self) -> None:
        a = self._merge({'cooling': True, 'cool_remaining_sec': 1800})
        self.assertTrue(a['cooling'])
        self.assertEqual(a['cool_remaining_sec'], 1800)

    def test_missing_field_is_none(self) -> None:
        """上游旧版本没有这个字段 → None，界面不显示倒计时（而不是显示 0 分钟）。"""
        a = self._merge({'cooling': True})
        self.assertIsNone(a['cool_remaining_sec'])

    def test_zero_and_negative_are_none(self) -> None:
        """0/负数没有展示意义（已到期），统一成 None 交给界面判断。"""
        for v in (0, -5):
            a = self._merge({'cooling': True, 'cool_remaining_sec': v})
            self.assertIsNone(a['cool_remaining_sec'], f'{v} 应归一为 None')

    def test_garbage_is_none_not_crash(self) -> None:
        """上游字段类型异常时不能让整页崩掉。"""
        for v in ('abc', None, [], {}):
            a = self._merge({'cooling': True, 'cool_remaining_sec': v})
            self.assertIsNone(a['cool_remaining_sec'], f'{v!r} 应归一为 None')

    def test_float_seconds_truncated_to_int(self) -> None:
        a = self._merge({'cooling': True, 'cool_remaining_sec': 90.7})
        self.assertEqual(a['cool_remaining_sec'], 90)


class RateLimitedModelsPassthroughTest(unittest.TestCase):
    """被限流的模型清单要原样透传（多模型时各自恢复时刻不同）。"""

    def _merge(self, pool_item: dict) -> dict:
        accounts = [{'uid': 'u1'}]
        wb2api.merge_pool_status(accounts, {'accounts': [dict(pool_item, uid='u1')]})
        return accounts[0]

    def test_list_passthrough(self) -> None:
        models = [
            {'model': 'glm-5.2', 'until': '2026-09-15T10:00:00+08:00'},
            {'model': 'deepseek-v4', 'reset_at': '2026-09-15T11:00:00+08:00'},
        ]
        a = self._merge({'cooling': True, 'rate_limited_models': models})
        self.assertEqual(len(a['rate_limited_models']), 2)
        self.assertEqual(a['rate_limited_models'][0]['model'], 'glm-5.2')

    def test_missing_is_empty_list(self) -> None:
        """缺失时给空列表而非 None —— 前端直接 map 不会炸。"""
        a = self._merge({'cooling': True})
        self.assertEqual(a['rate_limited_models'], [])

    def test_garbage_is_empty_list(self) -> None:
        for v in ('x', None, 5, {}):
            a = self._merge({'cooling': True, 'rate_limited_models': v})
            self.assertEqual(a['rate_limited_models'], [], f'{v!r} 应归一为 []')


if __name__ == '__main__':
    unittest.main()
