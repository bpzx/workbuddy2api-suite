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


class PruneRemovedTest(unittest.TestCase):
    """清理"上游已移除"的死键（真实案例：`server.max_body_mb`）。

    为什么这件事值得有测试：它**删**东西，而删除写错的代价比补齐写错大得多
    （补错只是多个键，删错会让真正生效的配置消失）。所以这里既钉"该删的删掉"，
    也钉"不该动的绝不能动"。
    """

    def test_removes_dead_leaf_and_its_empty_shell(self) -> None:
        """复现事故现场：只剩死键的 `server` 段应连同死键一起消失。"""
        template = {'listen': ':7863', 'pool': {'max_in_flight': 3}}
        config = {'listen': ':7863', 'pool': {'max_in_flight': 3},
                  'server': {'max_body_mb': 8}}
        removed = merge.prune_removed(template, config)
        self.assertEqual(removed, [('server.max_body_mb', '8')])
        self.assertNotIn('server', config, '只剩死键的空壳应一并摘掉')

    def test_keeps_template_keys_and_never_touches_values(self) -> None:
        """模板里有的键一个都不能少；值（哪怕是"看起来不对"的值）不是本函数的职责。"""
        template = {'pool': {'max_in_flight': 3}, 'upstream': {'timeout_seconds': 120}}
        config = {'pool': {'max_in_flight': 0}, 'upstream': {'timeout_seconds': 300}}
        self.assertEqual(merge.prune_removed(template, config), [])
        self.assertEqual(config['pool']['max_in_flight'], 0)
        self.assertEqual(config['upstream']['timeout_seconds'], 300)

    def test_does_not_drop_preexisting_empty_section(self) -> None:
        """本来就空的段落不摘 —— 只摘"被本次清理清空"的空壳。"""
        template = {'listen': ':7863'}
        config = {'listen': ':7863', 'experiments': {}}
        self.assertEqual(merge.prune_removed(template, config), [])
        self.assertIn('experiments', config)

    def test_reports_nested_dead_leaves_one_by_one(self) -> None:
        """逐叶子报告（不把整段糊成一行），便于对照原值。"""
        template = {'pool': {'a': 1}}
        config = {'pool': {'a': 1}, 'legacy': {'x': 1, 'y': {'z': 'v'}}}
        removed = dict(merge.prune_removed(template, config))
        self.assertEqual(sorted(removed), ['legacy.x', 'legacy.y.z'])
        self.assertNotIn('legacy', config)

    def test_api_key_and_comment_are_never_pruned(self) -> None:
        template = {'listen': ':7863'}
        config = {'listen': ':7863', 'api_key': 'sk-real', '_comment': 'note'}
        self.assertEqual(merge.prune_removed(template, config), [])
        self.assertEqual(config['api_key'], 'sk-real')

    def test_sensitive_value_is_redacted_in_report(self) -> None:
        """日志会打出死键原值；凭据类只打键名。"""
        template = {'a': 1}
        config = {'a': 1, 'legacy': {'my_token': 'supersecret', 'plain': 'ok'}}
        removed = dict(merge.prune_removed(template, config))
        self.assertEqual(removed['legacy.my_token'], '<已隐去>')
        self.assertEqual(removed['legacy.plain'], '"ok"')

    def test_long_value_is_truncated(self) -> None:
        template: dict = {}
        config = {'big': 'x' * 200}
        (path, value), = merge.prune_removed(template, config)
        self.assertEqual(path, 'big')
        self.assertLessEqual(len(value), 60)

    def test_type_mismatch_keeps_user_value(self) -> None:
        """模板里是对象、用户写成了字符串 —— 保留（与补齐同一原则）。"""
        template = {'pool': {'a': 1}}
        config = {'pool': 'not-an-object'}
        self.assertEqual(merge.prune_removed(template, config), [])
        self.assertEqual(config['pool'], 'not-an-object')

    def test_idempotent(self) -> None:
        template = {'listen': ':7863'}
        config = {'listen': ':7863', 'server': {'max_body_mb': 8}}
        self.assertEqual(len(merge.prune_removed(template, config)), 1)
        self.assertEqual(merge.prune_removed(template, config), [], '第二次不该再清')


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

    def test_cli_prunes_dead_key_and_writes_backup(self) -> None:
        self._write(self.template, {'listen': ':7863', 'pool': {'max_in_flight': 3}})
        self._write(self.config, {'listen': ':7863', 'pool': {'max_in_flight': 3},
                                  'server': {'max_body_mb': 8}})
        rc = merge.main(['config_merge.py', str(self.template), str(self.config)])
        self.assertEqual(rc, 0)
        after = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertNotIn('server', after)
        self.assertEqual(after['pool']['max_in_flight'], 3, '清理不该动其它键')
        backup = self.config.with_suffix('.json.bak')
        self.assertTrue(backup.is_file(), '写盘前应留一份备份')
        self.assertEqual(json.loads(backup.read_text(encoding='utf-8'))['server'],
                         {'max_body_mb': 8}, '备份必须是改动前的内容')

    def test_cli_does_add_and_prune_in_one_pass(self) -> None:
        """补与清在同一次写盘里完成，只留一份备份。"""
        self._write(self.template, {'listen': ':7863', 'pool': {'new': 1}})
        self._write(self.config, {'listen': ':7863', 'server': {'max_body_mb': 8}})
        rc = merge.main(['config_merge.py', str(self.template), str(self.config)])
        self.assertEqual(rc, 0)
        after = json.loads(self.config.read_text(encoding='utf-8'))
        self.assertEqual(after['pool']['new'], 1)
        self.assertNotIn('server', after)
        self.assertEqual(json.loads(self.config.with_suffix('.json.bak').read_text(encoding='utf-8')),
                         {'listen': ':7863', 'server': {'max_body_mb': 8}})

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
    """拿仓库里真实的模板跑一遍，确认它对"旧配置"的补齐与清理都符合预期。"""

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

    def test_real_template_prunes_the_reported_dead_key(self) -> None:
        """真实案例：`server.max_body_mb` 是上游已移除的键（请求体已无上限），必须清掉。

        这是"死键会说话"的现场：留着它会让人以为还存在一个 8MB 请求体上限。
        """
        template = json.loads(
            (_REPO / 'docker' / 'wb2api.config.template.json').read_text(encoding='utf-8'))
        stale = json.loads(json.dumps(template))
        # 旧模板生成的配置就长这样：多出一个 server 段
        stale.setdefault('server', {})['max_body_mb'] = 8

        removed = dict(merge.prune_removed(template, stale))
        self.assertEqual(removed, {'server.max_body_mb': '8'})
        self.assertNotIn('server', stale)

        # 关键不变式：清理之后，键集必须与模板**完全一致**（一个不多、一个不少）
        def paths(node: object, prefix: str = ''):
            if isinstance(node, dict):
                for k, v in node.items():
                    yield from paths(v, f'{prefix}.{k}' if prefix else k)
            else:
                yield prefix

        self.assertEqual(sorted(paths(template)), sorted(paths(stale)))


if __name__ == '__main__':
    unittest.main()
