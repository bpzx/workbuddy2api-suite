"""「并发取数」状态推导的行为测试（前端逻辑，借 Node 执行）。

为什么值得有：这段逻辑错起来**界面不报错**，只是变得不对劲——
  · 拿「请求在飞」当加载态 → 有心跳轮询的页面每 30 秒闪一次骨架；
  · 刷新时整体替换而不是合并 → 心跳里任何一个请求超时就把内容清空；
  · 首屏全失败时不报错 → 用户看到「暂无账号」，而数据其实是没取到。
三条都表现为「看起来只是卡了一下」，没人会为此提 issue。

为什么用 Python 包一层：本仓库的测试套件是 Python 的（`pytest server/tests/`），
而这段逻辑在前端。没有 Node（或版本太旧）时**跳过**而不是失败——后端开发者
不该因为机器上没装 Node 就跑不了整个测试套件。

跑的是 `web/lib/async-state.test.mjs`，它直接 import `.ts` 源码（Node ≥ 22.6
的 type stripping），因此测的是**真实实现**，不是复制一份逻辑。之所以把待测
逻辑单独放在 `web/lib/async-state.ts`，就是为了让它**零依赖**：那个 .mjs 只需
Node 就能跑，不需要 `web/node_modules`（hook 本体 import 了 react，直接测它
会把「没装前端依赖」变成测试失败，而不是跳过）。

除行为测试外，本文件还钉三条源码级不变式——它们要么在行为测试里表达不出来
（顺序、文件是否存在），要么是「测试别空转」的前提。
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

_ROOT = Path(__file__).resolve().parents[2]
_SCRIPT = _ROOT / 'web' / 'lib' / 'async-state.test.mjs'
_PURE = _ROOT / 'web' / 'lib' / 'async-state.ts'
_HOOK = _ROOT / 'web' / 'lib' / 'use-async-data.ts'
_DASHBOARD = _ROOT / 'web' / 'app' / '(main)' / 'dashboard' / 'page.tsx'
_NODE = shutil.which('node')

# 只跑 .mjs，不碰 .ts —— Node 的 type stripping 从 22.6 起才有
_MIN_MAJOR = 22


def _node_major() -> int | None:
    if not _NODE:
        return None
    try:
        out = subprocess.run([_NODE, '--version'], capture_output=True,
                             text=True, timeout=15).stdout.strip()
        return int(out.lstrip('v').split('.')[0])
    except Exception:  # noqa: BLE001
        return None


_MAJOR = _node_major()


def _code(path: Path) -> str:
    """读出源码并**剥掉注释**。

    源码级断言必须先剥注释：实现里正解释着「不要这么写」，直接搜全文会把这句
    说明本身当成违规。（`test_display_prefs.py` 的时区守卫第一次写就踩了这个坑，
    这里照抄它的做法。）
    """
    src = path.read_text(encoding='utf-8')
    src = re.sub(r'/\*.*?\*/', '', src, flags=re.S)   # 块注释
    return re.sub(r'//[^\n]*', '', src)               # 行注释


class AsyncStateBehaviourTest(unittest.TestCase):
    """跑 Node 侧的行为测试：状态推导与逐项合并的真实实现。"""

    @unittest.skipUnless(_NODE, '未安装 node，跳过前端逻辑测试')
    @unittest.skipUnless(_MAJOR is not None and _MAJOR >= _MIN_MAJOR,
                         f'需要 node ≥ {_MIN_MAJOR}（type stripping），当前 {_MAJOR}')
    def test_state_derivation(self) -> None:
        self.assertTrue(_SCRIPT.is_file(), f'缺少测试脚本: {_SCRIPT}')
        proc = subprocess.run(
            [_NODE, '--experimental-strip-types', str(_SCRIPT)],
            capture_output=True, text=True, timeout=120, cwd=str(_ROOT),
        )
        out = (proc.stdout or '') + (proc.stderr or '')
        self.assertEqual(proc.returncode, 0, f'取数状态推导不符合预期：\n{out}')
        self.assertIn('all passed', out, f'脚本没有跑到通过：\n{out}')


class AsyncStateInvariantTest(unittest.TestCase):
    """行为测试盖不到的不变式（顺序、文件存在性、测试是否空转）。"""

    def test_hook_uses_the_tested_module(self) -> None:
        """hook 必须真的调用 async-state 的两个函数。

        否则上面那个 .mjs 测的是「一份没人用的实现」——测试全绿，而页面上跑的是
        hook 里另写的一套。这种空转最难发现：断言全过，问题照旧。
        """
        code = _code(_HOOK)
        for fn in ('asyncFlags(', 'splitSettled('):
            self.assertIn(fn, code,
                          f'{_HOOK.name} 没有调用 {fn}——被测逻辑与线上逻辑已经脱节')

    def test_refresh_merges_instead_of_replacing(self) -> None:
        """刷新时必须把新值**合并**到已有值上，不能整体替换。

        整体替换的后果：心跳里任何一个请求超时，都会把已经显示出来的内容清空
        ——用户正看着的数字突然整页消失，而失败的可能只是 5 份数据里的 1 份。
        """
        code = _code(_HOOK)
        self.assertIn('...prev', code,
                      '刷新分支没有合并上一次的值：某个请求失败会把内容清空')
        self.assertIn('fresh.values', code, '没看到把本次结果并进去')

    def test_dashboard_guards_before_rendering_content(self) -> None:
        """仪表盘的首屏守卫必须出现在「暂无账号」之前。

        顺序就是这条不变式的全部：内容区里的空状态（「暂无账号」「暂无调用数据」）
        在数据还没取到时渲染出来，等于告诉用户「你没有账号」——而事实是还没取到。
        所以守卫必须**先**返回，把内容挡在后面。
        """
        code = _code(_DASHBOARD)
        guard = code.find('isInitialFailed || isInitialLoading')
        self.assertGreaterEqual(guard, 0, '仪表盘没有首屏守卫（骨架/错误分支）')

        for liar in ("t('dashboard.noAccounts')", "t('dashboard.noCallData')"):
            at = code.find(liar)
            self.assertGreaterEqual(at, 0, f'仪表盘里找不到 {liar}，断言前提不成立')
            self.assertLess(
                guard, at,
                f'首屏守卫排在 {liar} 之后——数据没取到时用户会看到这句「没有数据」，'
                '而事实是还没取到',
            )

        # 失败也必须有可见出口，不能只弹一个几秒后消失的 toast
        self.assertIn('LoadError', code, '仪表盘没有把加载失败显示出来')

    def test_no_route_level_loading_tsx(self) -> None:
        """不要创建 `app/(main)/loading.tsx`。

        Next.js 官方 Platform Support 表里，`loading.js` 在 **Static export** 一栏
        是 **No**：它是 Suspense fallback，依赖服务端流式渲染，而本项目的生产产物
        正是静态导出（`npm run build:export` / Dockerfile），10 个页面又都是
        `'use client'` + `useEffect` 取数，构建期边界立即 resolve。

        也就是说这个文件在开发服务器上「看起来能用」，上线后是死的——最坏的一种
        失败：本地验证通过、线上没效果。首屏加载态请走 useAsyncAll 的
        isInitialLoading。
        """
        offender = _ROOT / 'web' / 'app' / '(main)' / 'loading.tsx'
        self.assertFalse(
            offender.exists(),
            '静态导出下 loading.tsx 不生效（官方 Platform Support: Static export → No），'
            '首屏加载态请用 useAsyncAll 的 isInitialLoading',
        )


if __name__ == '__main__':
    unittest.main()
