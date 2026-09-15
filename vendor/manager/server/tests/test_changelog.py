"""更新日志解析（CHANGELOG.md → 结构化数据）测试。

界面「更新日志」页依赖这里的解析结果，格式为项目固定的三级结构：
`## [版本] - 日期` / `### 分类` / `- 条目`（两级缩进视为子项）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from server.services import changelog

SAMPLE = """# 更新日志

<!-- 说明 -->

## [未发布]

### 安全

- 修复 **device_token** 明文下发
- 子项示例
  - 这里是子项内容

### 计划中

- 等上游稳定后一起发版

---

## [1.0.15] - 2026-09-13

### 修复

- 请求体上限改为跟随配置 `server.max_body_mb`
  补充的续行说明

---

## [1.0.0] - 2026-08-01

### 新增

- 首个版本
"""


class ParseChangelogTest(unittest.TestCase):
    def test_versions_in_order(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        self.assertEqual([v['version'] for v in versions], ['未发布', '1.0.15', '1.0.0'])

    def test_unreleased_flag_and_date(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        self.assertTrue(versions[0]['unreleased'])
        self.assertEqual(versions[0]['date'], '')
        self.assertFalse(versions[1]['unreleased'])
        self.assertEqual(versions[1]['date'], '2026-09-13')

    def test_sections_and_items(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        unreleased = versions[0]
        self.assertEqual([s['title'] for s in unreleased['sections']], ['安全', '计划中'])
        sec = unreleased['sections'][0]
        self.assertEqual(len(sec['items']), 3)
        self.assertEqual(sec['items'][0]['level'], 0)
        self.assertEqual(sec['items'][0]['text'], '修复 **device_token** 明文下发')
        # 两个空格缩进视为子项
        self.assertEqual(sec['items'][2]['level'], 1)
        self.assertEqual(sec['items'][2]['text'], '这里是子项内容')

    def test_continuation_line_is_joined(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        v15 = versions[1]
        item = v15['sections'][0]['items'][0]
        self.assertEqual(item['text'], '请求体上限改为跟随配置 `server.max_body_mb` 补充的续行说明')

    def test_separator_and_top_matter_ignored(self) -> None:
        versions = changelog.parse_changelog(SAMPLE)
        # `---` 与文件头（标题/注释）不应产生版本或条目
        self.assertEqual(len(versions), 3)
        for v in versions:
            for s in v['sections']:
                for it in s['items']:
                    self.assertNotIn('---', it['text'])

    def test_empty_and_garbage_input(self) -> None:
        self.assertEqual(changelog.parse_changelog(''), [])
        self.assertEqual(changelog.parse_changelog(None), [])  # type: ignore[arg-type]
        # 没有版本标题时，正文不归属任何版本
        self.assertEqual(changelog.parse_changelog('- 游离条目\n### 无主分类'), [])


class LoadChangelogTest(unittest.TestCase):
    def test_missing_file_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / 'CHANGELOG.md'
            with mock.patch.object(changelog.config, 'ROOT', Path(tmp)):
                data = changelog.load_changelog()
            self.assertFalse(data['available'])
            self.assertIn('未找到更新日志文件', data['error'])
            self.assertIn(missing.name, data['error'])
            self.assertEqual(data['versions'], [])

    def test_loads_and_limits_versions(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            blocks = []
            for i in range(changelog._MAX_VERSIONS + 5):
                blocks.append(f'## [1.{i}.0] - 2026-01-01\n\n### 新增\n\n- 第 {i} 项\n')
            (Path(tmp) / 'CHANGELOG.md').write_text('\n'.join(blocks), encoding='utf-8')
            with mock.patch.object(changelog.config, 'ROOT', Path(tmp)):
                data = changelog.load_changelog()
            self.assertTrue(data['available'])
            self.assertEqual(len(data['versions']), changelog._MAX_VERSIONS)
            self.assertEqual(data['total'], changelog._MAX_VERSIONS + 5)
            self.assertTrue(data['truncated'])
            # 保留的是文件开头的（最新）版本
            self.assertEqual(data['versions'][0]['version'], '1.0.0')

    def test_read_error_reports_unavailable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'CHANGELOG.md'
            path.write_text('## [1.0.0]\n\n### 新增\n\n- x\n', encoding='utf-8')
            with mock.patch.object(changelog.config, 'ROOT', Path(tmp)), \
                    mock.patch.object(Path, 'read_text', side_effect=OSError('boom')):
                data = changelog.load_changelog()
            self.assertFalse(data['available'])
            self.assertIn('读取更新日志失败', data['error'])

    def test_falls_back_to_server_copy(self) -> None:
        """老部署升级后根目录可能没有 CHANGELOG.md，此时用 server/ 里的副本。

        这不是假想场景：v1.0.16 之前的更新器只替换 server/、web/out/、
        deploy/ 与 .version，不碰根目录文件——实测真有部署报「未找到更新日志
        文件」，且因为已是最新版、再点更新也不会补上。发布打包因此会在
        server/ 放一份副本，这里锁定该回退必须生效。
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server').mkdir()
            (root / 'server' / 'CHANGELOG.md').write_text(
                '## [9.9.9] - 2026-01-01\n\n### 修复\n\n- 来自 server 副本\n',
                encoding='utf-8',
            )
            with mock.patch.object(changelog.config, 'ROOT', root):
                data = changelog.load_changelog()
            self.assertTrue(data['available'])
            self.assertEqual(data['versions'][0]['version'], '9.9.9')
            self.assertTrue(data['path'].endswith(str(Path('server') / 'CHANGELOG.md')))

    def test_root_copy_wins_over_server_copy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / 'server').mkdir()
            (root / 'CHANGELOG.md').write_text(
                '## [8.8.8] - 2026-01-01\n\n### 新增\n\n- 根目录优先\n', encoding='utf-8')
            (root / 'server' / 'CHANGELOG.md').write_text(
                '## [9.9.9] - 2026-01-01\n\n### 新增\n\n- server 副本\n', encoding='utf-8')
            with mock.patch.object(changelog.config, 'ROOT', root):
                data = changelog.load_changelog()
            self.assertEqual(data['versions'][0]['version'], '8.8.8')
            self.assertFalse(data['path'].endswith(str(Path('server') / 'CHANGELOG.md')))


if __name__ == '__main__':
    unittest.main()
