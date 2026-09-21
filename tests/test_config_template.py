"""配置模板必须与上游 `config.example.json` 保持同一键集。

为什么单列一个测试：我们的模板本质是"上游 example + 容器化的覆盖值"，
而**人工复制的键列表必然漂移**。这个项目上已经真实发生过两次：

  1. 上游 **BREAKING 移除** `server.max_body_mb`（请求体改为无上限，见
     `cmd/server/main.go` 的注释与 `TestMaxBodyLegacyKeyIgnored`），
     而模板里还留着 `8` —— 生成的 config.json 会**声称一个不存在的 8MB 限制**。
     上游为了兼容旧配置容忍了这个键，所以它不会报错，只会**静默地说错话**。
  2. 上游 `pool` 新增了 `max_in_flight_global` / `degrade_threshold` /
     `degrade_cooldown` / `degrade_cooldown_max` / `cost_explore_interval`，
     模板里没有。

第 2 条当时没造成行为问题（上游对缺字段会套默认值，见 `cmd/server/config.go`
的 normalize），但这只是**碰巧安全**：哪天上游改掉默认值策略，或某个零值不再是
"回退"语义，我们就会静默走偏。

因此这里比对的是**递归键路径集合**（不只是顶层键），新增、删除、改名、挪层级
都能抓到。上游一旦调整 config 结构，这个测试会红，提示同步模板。
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_TEMPLATE = _REPO / 'docker' / 'wb2api.config.template.json'
_UPSTREAM_EXAMPLE = _REPO / 'vendor' / 'wb2api' / 'config.example.json'


def _key_paths(node, prefix: str = '') -> set[str]:
    """把嵌套配置压成 a.b.c 形式的键路径集合；忽略我们自己的 `_comment`。"""
    paths: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if key == '_comment':
                continue
            path = f'{prefix}{key}'
            paths.add(path)
            paths |= _key_paths(value, f'{path}.')
    return paths


class ConfigTemplateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.template = json.loads(_TEMPLATE.read_text(encoding='utf-8'))
        self.upstream = json.loads(_UPSTREAM_EXAMPLE.read_text(encoding='utf-8'))

    def test_upstream_example_looks_sane(self) -> None:
        """先确认读到了真东西，否则下面的比对会在空集上"通过"。"""
        self.assertGreater(len(_key_paths(self.upstream)), 40)

    def test_key_paths_match_upstream_example(self) -> None:
        want = _key_paths(self.upstream)
        got = _key_paths(self.template)
        missing = sorted(want - got)
        extra = sorted(got - want)
        self.assertEqual(
            missing, [],
            '模板缺少上游有的配置项（生成的 config.json 会缺字段，'
            f'目前靠上游的默认值兜着，但这是隐患）：{missing}',
        )
        self.assertEqual(
            extra, [],
            '模板里有上游已经没有的配置项（生成的 config.json 会说错话）：'
            f'{extra}',
        )

    def test_legacy_max_body_mb_stays_removed(self) -> None:
        """上游已 BREAKING 移除 max_body_mb（请求体无上限），别顺手加回来。

        单独钉一条：这个键最容易被"看起来该有"而误加，而它已经不再生效 ——
        留着只会让生成的配置声称一个不存在的 8MB 限制。
        上游容忍旧键（`TestMaxBodyLegacyKeyIgnored`），所以不会报错，只会误导。
        """
        self.assertNotIn('server.max_body_mb', _key_paths(self.template))
        self.assertEqual(self.template.get('server'), None,
                         'server 段已无有效字段，不应保留')

    def test_container_overrides_are_present(self) -> None:
        """entrypoint 只会覆写这三个字段（见 docker/entrypoint-wb2api.sh），
        所以模板里必须留出它们，且路径默认落在数据卷内。"""
        self.assertEqual(self.template['api_key'], 'REPLACED_AT_FIRST_BOOT')
        self.assertTrue(str(self.template['auth_dir']).startswith('/data'),
                        'auth_dir 必须是容器内数据卷路径')
        self.assertTrue(str(self.template['state_file']).startswith('/data'),
                        'state_file 必须是容器内数据卷路径')

    def test_listen_matches_container_port(self) -> None:
        """wb2api 在容器内监听 7863；compose 的 expose 与 healthcheck 都按它写。"""
        self.assertEqual(self.template['listen'], ':7863')


if __name__ == '__main__':
    unittest.main()
