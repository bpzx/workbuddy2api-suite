"""构建期改动的**预检**：在原始 `vendor/` 上验证全部前提。

与其它测试的区别：这里读的是**未修改的** vendor 快照（不是打过补丁的树），
目的是把"上游重构了、锚点失效"这类问题**在本地就暴露**出来。

apply.py 自己也会在构建时做同样的校验并 fail-fast，那为什么还要这个测试？
  * 反馈快得多（毫秒级，不需要起 Docker / 等 CI）；
  * 失败出现在测试报告里，而不是构建日志深处；
  * 逼着我们**把补丁的意图写成断言**（例如"替换后那条 toast 必须消失"），
    而不只是"锚点能匹配" —— 后者在替换内容写错时照样通过。

注：这些校验读的是仓库里的 `vendor/`，所以本测试必须在**仓库根**能解析到
`vendor/` 的前提下运行（CI 里由 `-s tests -t tests` 从仓库根跑，满足）。
"""
from __future__ import annotations

import importlib.util
import json
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_VENDOR = _REPO / 'vendor'
_OVERLAY = _REPO / 'docker' / 'overlay'
_APPLY = _REPO / 'docker' / 'patches' / 'apply.py'


def _load_apply():
    spec = importlib.util.spec_from_file_location('suite_apply_for_test', str(_APPLY))
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


apply = _load_apply()


class TextPatchPreflightTest(unittest.TestCase):
    def test_anchors_exist_exactly_once(self) -> None:
        edits = apply.build_text_edits(_VENDOR)
        # 先确认没在空集上通过
        self.assertGreaterEqual(len(edits), 4, '文本补丁数量异常少，检查 build_text_edits')

        for path, old, new, label in edits:
            with self.subTest(patch=label):
                self.assertTrue(path.is_file(), f'{label}：找不到上游文件 {path}')
                text = path.read_text(encoding='utf-8')
                count = text.count(old)
                self.assertEqual(
                    count, 1,
                    f'{label}：锚点在 {path.name} 里出现 {count} 次（应为 1）——'
                    '上游很可能重构了这段代码，请更新 apply.py 里对应补丁的锚点',
                )
                self.assertNotIn(
                    new, text,
                    f'{label}：{path.name} 似乎已经是我们打过补丁的内容了 ——'
                    'vendor/ 不是干净的上游快照',
                )

    def test_patch_targets_are_the_expected_files(self) -> None:
        """把"改了哪些上游文件"固定下来：新增一个补丁时这里会失败，提醒复核清单。"""
        files = sorted({str(p.relative_to(_VENDOR)).replace('\\', '/')
                        for p, *_ in apply.build_text_edits(_VENDOR)})
        self.assertEqual(files, [
            'manager/server/config.py',
            'manager/server/main.py',
            'manager/web/components/common/layout/ManagementBar.tsx',
            'wb2api/internal/upstream/transport.go',
        ])


class OverlayPreflightTest(unittest.TestCase):
    def test_overlays_are_plannable(self) -> None:
        """覆写前提：源文件在、目标**已存在**（上游改名要立刻失败）、且尚未被覆写。

        上游若删掉或改名了 UpdatePanel.tsx，这里会抛 PatchError。
        """
        ops = apply._plan_overlays(_VENDOR, _OVERLAY)
        self.assertEqual(len(ops), len(apply.OVERLAYS))
        self.assertGreaterEqual(len(ops), 1)

    def test_additions_are_plannable(self) -> None:
        """新增前提：目标**不存在**（上游若自行实现了同一件事，这里会失败）。"""
        ops = apply._plan_additions(_VENDOR, _OVERLAY)
        self.assertEqual(len(ops), len(apply.ADDITIONS))

    def test_addition_targets_absent_from_vendor(self) -> None:
        for _src, rel_dst, _label in apply.ADDITIONS:
            with self.subTest(target=rel_dst):
                self.assertFalse((_VENDOR / rel_dst).exists(),
                                 f'{rel_dst} 已在上游快照里存在 —— 应改用上游实现')


class I18nPreflightTest(unittest.TestCase):
    def test_merge_is_plannable_for_all_locales(self) -> None:
        ops = apply._plan_i18n(_VENDOR, _OVERLAY)
        # 与上游实际存在的语系文件数比对，而不是硬编码 5 —— 上游新增语言时
        # apply.py 会失败（提示补译文），这条断言不该跟着一起误导
        on_disk = len(list((_VENDOR / apply.I18N_LOCALES_DIR).glob('*.json')))
        self.assertGreaterEqual(on_disk, 5)
        self.assertEqual(len(ops), on_disk)

    def test_panel_only_uses_defined_keys(self) -> None:
        """面板里写死的 t('suiteUpdate.*') 必须都在文案源里定义。

        漏了会渲染成键名给用户看；上游 test_web_i18n 也会红，但那是补丁之后的事。
        """
        panel = (_OVERLAY / apply.OVERLAYS[0][0]).read_text(encoding='utf-8')
        used = set(re.findall(r"t\('suiteUpdate\.([A-Za-z0-9_]+)'", panel))
        src = json.loads((_OVERLAY / apply.I18N_SOURCE).read_text(encoding='utf-8'))
        defined = set(src['locales']['zh-CN'])
        self.assertTrue(used, '没扫到任何 suiteUpdate 键，正则可能失效了')
        self.assertEqual(sorted(used - defined), [], '面板引用了未定义的文案键')
        self.assertEqual(sorted(defined - used), [], '文案源里有没人用的死键')


class PatchIntentTest(unittest.TestCase):
    """断言补丁的**意图**，而不只是"锚点能匹配"。"""

    def test_management_bar_patch_removes_the_toast(self) -> None:
        """替换后上游那条"发现新版本"的 toast 调用必须彻底消失。

        只验证锚点可匹配是不够的：替换文本若写错（比如把 notify 调用留在里面），
        锚点照样匹配成功，而弹窗依旧出现。
        """
        bar = (_VENDOR / 'manager' / 'web' / 'components' / 'common' / 'layout'
               / 'ManagementBar.tsx').read_text(encoding='utf-8')
        patched = bar.replace(apply.MANAGEMENT_BAR_OLD, apply.MANAGEMENT_BAR_NEW, 1)
        self.assertNotEqual(patched, bar, '替换没有生效')
        self.assertNotIn("notify.warn(t('update.newVersion')", patched,
                         'toast 调用仍在 —— 弹窗不会被去掉')
        self.assertNotIn('workbuddy-manager:update-notified', patched,
                         '会话标记仍在 —— 说明整个 useEffect 没被移除')
        self.assertIn('集成补丁', apply.MANAGEMENT_BAR_NEW,
                      '替换文本缺少补丁标记，CI 的断言会失去意义')

    def test_go_patch_sets_proxy_field(self) -> None:
        self.assertIn('Proxy: http.ProxyFromEnvironment', apply.GO_NEW)

    def test_manager_patch_adds_direct_mounts(self) -> None:
        self.assertIn('_internal_direct_mounts', apply.MANAGER_NEW)
        self.assertIn('kwargs[\'mounts\']', apply.MANAGER_NEW)


if __name__ == '__main__':
    unittest.main()
