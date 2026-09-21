"""配置增量补齐（config_merge）的测试。

这个模块的存在理由是一个真实事故：上游新增 `pool.cost_explore_interval` 后，
老部署的 `/data/config.json` 里没有这个键，设置页的输入框就显示出**字面量
`undefined`**（成因见模块注释）。所以这里除了常规合并语义，还专门复现该场景。
"""
from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_MERGE = _REPO / 'docker' / 'overlay' / 'config_merge.py'


def _load():
    spec = importlib.util.spec_from_file_location('suite_config_merge', str(_MERGE))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


merge = _load()


class MergeMissingTest(unittest.TestCase):
    def test_adds_missing_nested_key(self) -> None:
        """复现事故场景：老配置缺 pool 下的新键，必须被补上。"""
        template = {'pool': {'max_in_flight': 3, 'cost_explore_interval': '30m'}}
        config = {'pool': {'max_in_flight': 3}}
        added = merge.merge_missing(template, config)
        self.assertEqual(added, ['pool.cost_explore_interval'])
        self.assertEqual(config['pool']['cost_explore_interval'], '30m')

    def test_adds_whole_missing_subtree(self) -> None:
        template = {'pool': {'a': 1, 'b': 2}}
        config: dict = {}
        added = merge.merge_missing(template, config)
        self.assertEqual(sorted(added), ['pool'])
        self.assertEqual(config['pool'], {'a': 1, 'b': 2})

    def test_never_overwrites_existing_values(self) -> None:
        """只补不改 —— 已有值一律保留，这比"补一个键"重要得多。"""
        template = {'pool': {'max_in_flight': 3, 'new_key': 'x'}, 'listen': ':7863'}
        config = {'pool': {'max_in_flight': 99, 'user_key': 'keep'}, 'listen': ':9999'}
        added = merge.merge_missing(template, config)
        self.assertEqual(added, ['pool.new_key'])
        self.assertEqual(config['pool']['max_in_flight'], 99, '用户改过的值被覆盖了')
        self.assertEqual(config['pool']['user_key'], 'keep', '用户自加的键被删了')
        self.assertEqual(config['listen'], ':9999', '用户改过的顶层值被覆盖了')

    def test_type_mismatch_is_left_alone(self) -> None:
        """用户把对象写成了字符串（或反之）时不动它 —— 那是人的决定。"""
        template = {'pool': {'a': 1}}
        config = {'pool': 'oops'}
        added = merge.merge_missing(template, config)
        self.assertEqual(added, [])
        self.assertEqual(config['pool'], 'oops')

    def test_lists_are_leaves(self) -> None:
        template = {'schedule': {'checkin_hours': [9, 21]}}
        config = {'schedule': {}}
        added = merge.merge_missing(template, config)
        self.assertEqual(added, ['schedule.checkin_hours'])
        self.assertEqual(config['schedule']['checkin_hours'], [9, 21])

    def test_api_key_is_never_added(self) -> None:
        """模板里的 api_key 是占位符，补进去等于写了个无效密钥。"""
        template = {'api_key': 'REPLACED_AT_FIRST_BOOT', 'listen': ':7863'}
        config: dict = {}
        added = merge.merge_missing(template, config)
        self.assertEqual(added, ['listen'])
        self.assertNotIn('api_key', config)

    def test_comment_is_never_added(self) -> None:
        """`_comment` 是我们写在模板里的内部说明，不是上游配置项 ——
        补进用户配置会让日志里"补齐 N 个上游新增配置项"变成假话。"""
        template = {'_comment': '我们的说明', 'listen': ':7863'}
        config: dict = {}
        added = merge.merge_missing(template, config)
        self.assertEqual(added, ['listen'])
        self.assertNotIn('_comment', config)

    def test_idempotent(self) -> None:
        template = {'pool': {'a': 1, 'b': {'c': 2}}}
        config = {'pool': {'a': 1}}
        self.assertEqual(merge.merge_missing(template, config), ['pool.b'])
        self.assertEqual(merge.merge_missing(template, config), [], '第二次不该再补')


class CliTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.dir = Path(self._td.name)
        self.template = self.dir / 'template.json'
        self.config = self.dir / 'config.json'

    def _write(self, path: Path, data: dict) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')

    def test_cli_adds_key_and_writes_backup(self) -> None:
        self._write(self.template, {'listen': ':7863', 'pool': {'new': '30m'}})
        self._write(self.config, {'listen': ':7863', 'pool': {}})
        rc = merge.main(['config_merge.py', str(self.template), str(self.config)])
        self.assertEqual(rc, 0)
        after = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(after['pool']['new'], '30m')
        # 备份必须是**改动前**的内容
        backup = self.config.with_suffix('.json.bak')
        self.assertTrue(backup.is_file(), '写盘前应留一份备份')
        self.assertEqual(json.loads(backup.read_text(encoding='utf-8')),
                         {'listen': ':7863', 'pool': {}})
        # 不留临时文件
        self.assertFalse(self.config.with_suffix('.json.tmp').exists())

    def test_cli_does_not_touch_file_when_nothing_to_add(self) -> None:
        body = {'listen': ':7863'}
        self._write(self.template, {'listen': ':7863'})
        self._write(self.config, body)
        before = self.config.read_text(encoding='utf-8')
        rc = merge.main(['config_merge.py', str(self.template), str(self.config)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.config.read_text(encoding='utf-8'), before,
                         '没有缺失键时不该重写文件（避免无谓的 mtime 变化与备份）')
        self.assertFalse(self.config.with_suffix('.json.bak').exists())

    def test_cli_tolerates_missing_files(self) -> None:
        """文件不全由入口脚本负责，本脚本静默放过（不该让启动失败）。"""
        self._write(self.template, {'listen': ':7863'})
        rc = merge.main(['config_merge.py', str(self.template), str(self.dir / 'nope.json')])
        self.assertEqual(rc, 0)

    def test_cli_tolerates_broken_json(self) -> None:
        self._write(self.template, {'listen': ':7863'})
        self.config.write_text('{ not json', encoding='utf-8')
        rc = merge.main(['config_merge.py', str(self.template), str(self.config)])
        self.assertEqual(rc, 0)
        self.assertEqual(self.config.read_text(encoding='utf-8'), '{ not json',
                         '解析失败时不该改动文件')

    def test_cli_tolerates_unwritable_config(self) -> None:
        """只读挂载时只告警，不能让容器起不来。"""
        self._write(self.template, {'listen': ':7863', 'pool': {'new': 1}})
        self._write(self.config, {'listen': ':7863'})
        from unittest import mock
        with mock.patch.object(merge.os, 'replace', side_effect=OSError('read-only')):
            rc = merge.main(['config_merge.py', str(self.template), str(self.config)])
        self.assertEqual(rc, 0, '写盘失败也必须返回 0')


class RealTemplateTest(unittest.TestCase):
    """拿仓库里真实的模板跑一遍，确认它对"旧配置"的补齐符合预期。"""

    def test_real_template_fills_the_reported_missing_key(self) -> None:
        template = json.loads(
            (_REPO / 'docker' / 'wb2api.config.template.json').read_text(encoding='utf-8'))
        # 模拟事故现场的旧 config：照旧模板生成过，因此缺 pool 的新键
        stale = json.loads(json.dumps(template))
        for key in ('cost_explore_interval', 'max_in_flight_global',
                    'degrade_threshold', 'degrade_cooldown', 'degrade_cooldown_max'):
            stale['pool'].pop(key, None)
        stale.pop('admin', None)
        added = merge.merge_missing(template, stale)
        self.assertIn('pool.cost_explore_interval', added)
        self.assertIn('admin', added)
        self.assertEqual(stale['pool']['cost_explore_interval'], '30m',
                         '补齐后设置页就不该再显示 undefined')


if __name__ == '__main__':
    unittest.main()
