"""updater 侧车与 helper 的测试。

这两个文件的运行位置比较特殊，先说明白：

  * `docker/overlay/suite_updater.py` —— 侧车服务，运行时 COPY 到
    `/opt/suite/suite_updater.py`；**不在** vendor 树里，所以直接从仓库路径加载。
  * `docker/overlay/update_runner.py` —— 一次性 helper 的入口。

两者的模块级常量都从环境变量读取，因此加载前先设好环境再 exec_module。
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
_OVERLAY = _REPO / 'docker' / 'overlay'
_SIDECAR = _OVERLAY / 'suite_updater.py'
_RUNNER = _OVERLAY / 'update_runner.py'
_PANEL = _OVERLAY / 'web' / 'components' / 'common' / 'settings' / 'UpdatePanel.tsx'


def _load(name: str, path: Path, **env: str):
    """按给定环境变量加载一个模块（每次都是全新的模块对象）。"""
    old = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update({k: str(v) for k, v in env.items()})
        spec = importlib.util.spec_from_file_location(name, str(path))
        assert spec and spec.loader
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return mod


class OverlayMarkerTest(unittest.TestCase):
    """apply.py 会断言落位后的文件含该标记；这里连"源文件本身有标记"一起守住。"""

    def test_every_overlay_file_carries_the_marker(self) -> None:
        files = [
            _SIDECAR,
            _RUNNER,
            _OVERLAY / 'config_merge.py',
            _OVERLAY / 'check_device_token.py',
            _OVERLAY / 'server' / 'services' / 'suite.py',
            _OVERLAY / 'server' / 'routers' / 'suite.py',
            _PANEL,
        ]
        for f in files:
            with self.subTest(file=f.name):
                self.assertTrue(f.is_file(), f'overlay 文件缺失：{f}')
                self.assertIn('SUITE-OVERLAY', f.read_text(encoding='utf-8'),
                              f'{f.name} 缺少 SUITE-OVERLAY 标记')

    def test_update_panel_keeps_capability_flag(self) -> None:
        """上游 test_docker_deploy 断言本文件必须出现该串（按字面量检查）。

        本面板**不渲染**这个能力标志：这里根本没有"更新上游"按钮，要防的
        "点到做不到的操作"不存在，也就无需解释"为什么不提供"。它只以注释形式
        存在并说明这一决定。下面顺手守住"别把它wire回组件状态"。
        """
        src = _PANEL.read_text(encoding='utf-8')
        self.assertIn('can_update_upstream', src)
        self.assertNotIn('canControlDocker', src,
                         '能力标志不该再驱动界面状态（本面板没有上游更新按钮）')

    def test_update_panel_uses_heartbeat_not_raw_interval(self) -> None:
        """手写 setInterval 会踩上游的轮询可见性守卫，必须用现成的 useHeartbeat。

        注意只找**调用**（带括号）：文件里的注释解释了"为什么不手写 setInterval"，
        按词匹配会把注释也算上，测试就成了误报。
        """
        src = _PANEL.read_text(encoding='utf-8')
        self.assertIn('useHeartbeat(', src)
        self.assertNotIn('setInterval(', src)


class CommandShapeTest(unittest.TestCase):
    """侧车只做"派 helper"一件事，真正的 compose 由 helper 执行。"""

    def test_sidecar_spawns_a_fixed_helper(self) -> None:
        src = _SIDECAR.read_text(encoding='utf-8')
        self.assertIn('HELPER_NAME', src)
        self.assertIn("'--rm'", src)
        # 侧车自己**不跑** compose —— 那会重建到它自己，见文件头注释
        self.assertNotIn("'pull'", src)

    def test_runner_runs_compose_pull_then_up(self) -> None:
        src = _RUNNER.read_text(encoding='utf-8')
        self.assertIn("'pull'", src)
        self.assertIn("'-d'", src)
        self.assertIn("'--project-directory'", src)
        # 顺序：先 pull 再 up，不能反
        self.assertLess(src.index("'pull'"), src.index("'up'"))


class SidecarAuthTest(unittest.TestCase):
    def _authed(self, token: str, given: str) -> bool:
        m = _load('upd_sidecar_auth', _SIDECAR, SUITE_UPDATER_TOKEN=token)

        class Fake:
            def __init__(self, hdrs: dict) -> None:
                self.headers = hdrs

        hdrs = {'Authorization': given} if given else {}
        # _authed 只用到 self.headers，直接用假对象绑定即可
        return m.Handler._authed(Fake(hdrs))

    def test_requires_exact_bearer_token(self) -> None:
        self.assertTrue(self._authed('secret', 'Bearer secret'))
        self.assertFalse(self._authed('secret', 'Bearer nope'))
        self.assertFalse(self._authed('secret', 'secret'))
        self.assertFalse(self._authed('secret', ''))

    def test_no_token_configured_rejects_everything(self) -> None:
        """没配 token 时必须一律拒绝（安全默认），而不是放行。"""
        self.assertFalse(self._authed('', 'Bearer '))
        self.assertFalse(self._authed('', 'Bearer secret'))


class SidecarStartTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self.m = _load('upd_sidecar_start', _SIDECAR,
                       SUITE_UPDATER_TOKEN='secret', WB_DATA_DIR=str(self.data))

    def _status(self) -> dict:
        return json.loads((self.data / 'suite-update-status.json').read_text(encoding='utf-8'))

    def test_rejects_while_helper_running(self) -> None:
        """并发保护：helper 还在跑就拒绝，避免两次重建互相踩。"""
        with mock.patch.object(self.m, '_helper_running', return_value=True):
            code, body = self.m.start_update()
        self.assertEqual(code, 409)
        self.assertIn('已有更新任务', body['message'])

    def test_function_rejects_when_no_token(self) -> None:
        m = _load('upd_sidecar_notoken', _SIDECAR,
                  SUITE_UPDATER_TOKEN='', WB_DATA_DIR=str(self.data))
        code, body = m.start_update()
        self.assertEqual(code, 500)
        self.assertFalse(body['ok'])
        self.assertIn('SUITE_UPDATER_TOKEN', body['message'])

    def test_missing_socket_is_actionable(self) -> None:
        """socket 没挂进来时要指出是 compose 的问题，而不是让用户猜。"""
        with mock.patch.object(self.m, '_helper_running', return_value=False), \
             mock.patch.object(self.m, '_socket_available', return_value=False):
            code, body = self.m.start_update()
        self.assertEqual(code, 500)
        self.assertIn('docker-compose.yml', body['message'])

    def test_missing_suite_mount_is_actionable(self) -> None:
        """读不到 /suite 的宿主机路径时，要指出缺的是哪个挂载。"""
        with mock.patch.object(self.m, '_helper_running', return_value=False), \
             mock.patch.object(self.m, '_socket_available', return_value=True), \
             mock.patch.object(self.m, '_self_info',
                               return_value={'image': 'img:1', 'suite_dir': '',
                                             'host_data': '/h/data'}):
            code, body = self.m.start_update()
        self.assertEqual(code, 500)
        self.assertIn('/suite', body['message'])

    def test_missing_data_mount_is_actionable(self) -> None:
        with mock.patch.object(self.m, '_helper_running', return_value=False), \
             mock.patch.object(self.m, '_socket_available', return_value=True), \
             mock.patch.object(self.m, '_self_info',
                               return_value={'image': 'img:1', 'suite_dir': '/h/s',
                                             'host_data': ''}):
            code, body = self.m.start_update()
        self.assertEqual(code, 500)
        self.assertIn('/data', body['message'])

    def test_spawn_failure_recorded_in_status(self) -> None:
        with mock.patch.object(self.m, '_helper_running', return_value=False), \
             mock.patch.object(self.m, '_spawn_helper', return_value=(False, '炸了')):
            code, _ = self.m.start_update()
        self.assertEqual(code, 500)
        st = self._status()
        self.assertFalse(st['running'])
        self.assertFalse(st['ok'])
        self.assertTrue(st['finished_at'])
        self.assertEqual(st['logs'][-1]['level'], 'error')

    def test_success_writes_initial_status_and_returns_202(self) -> None:
        with mock.patch.object(self.m, '_helper_running', return_value=False), \
             mock.patch.object(self.m, '_spawn_helper', return_value=(True, '更新已开始')):
            code, body = self.m.start_update()
        self.assertEqual(code, 202)
        self.assertTrue(body['ok'])
        st = self._status()
        self.assertTrue(st['running'])
        self.assertIsNone(st['ok'])
        self.assertEqual(st['target'], 'suite')


class SpawnArgsTest(unittest.TestCase):
    """helper 的挂载参数是这套设计里最容易踩的坑，单独测。"""

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self.m = _load('upd_sidecar_args', _SIDECAR,
                       SUITE_UPDATER_TOKEN='secret', WB_DATA_DIR=str(self.data))

    def _spawn(self, info: dict) -> list[str]:
        seen: list[list[str]] = []

        def fake_run(args, timeout=60):
            seen.append(args)
            return True, 'cid1234567890'

        with mock.patch.object(self.m, '_socket_available', return_value=True), \
             mock.patch.object(self.m, '_self_info', return_value=info), \
             mock.patch.object(self.m, '_run', side_effect=fake_run):
            ok, msg = self.m._spawn_helper()
        self.assertTrue(ok, msg)
        return seen[0]

    def test_suite_dir_mounted_at_identical_absolute_path(self) -> None:
        """套件目录必须按**同一个绝对路径**挂给 helper。

        否则 compose 会把 `./data` 之类相对路径解析成容器内路径，创建的兄弟
        容器就绑到宿主机上一个错误的空目录 —— 而且不会报错，只是数据"消失"。
        """
        args = self._spawn({'image': 'ghcr.io/o/r:1',
                            'suite_dir': '/host/suite', 'host_data': '/host/data'})
        self.assertIn('/host/suite:/host/suite:ro', args)

    def test_socket_and_data_mounted(self) -> None:
        args = self._spawn({'image': 'ghcr.io/o/r:1',
                            'suite_dir': '/host/suite', 'host_data': '/host/data'})
        self.assertIn('/var/run/docker.sock:/var/run/docker.sock', args)
        self.assertIn('/host/data:/data', args)

    def test_entrypoint_and_image(self) -> None:
        args = self._spawn({'image': 'ghcr.io/o/r:1',
                            'suite_dir': '/host/suite', 'host_data': '/host/data'})
        self.assertEqual(args[0:2], ['run', '-d'])
        self.assertIn('--rm', args)
        # tini 当 PID 1（与镜像其余服务一致），脚本作为它的参数
        self.assertIn('--entrypoint', args)
        self.assertIn('/usr/bin/tini', args)
        self.assertEqual(args[-1], '/opt/suite/update_runner.py')
        # 镜像名必须在脚本路径之前（docker run 的 IMAGE 位于 COMMAND 之前）
        self.assertLess(args.index('ghcr.io/o/r:1'), args.index('/opt/suite/update_runner.py'))

    def test_helper_owns_its_own_name(self) -> None:
        """helper 名字不能和 compose 的服务名撞（否则会被 up -d 当成项目容器）。"""
        args = self._spawn({'image': 'img', 'suite_dir': '/s', 'host_data': '/d'})
        self.assertIn(self.m.HELPER_NAME, args)
        self.assertNotIn('updater', args)


class SidecarStatusTest(unittest.TestCase):
    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self.m = _load('upd_sidecar_status', _SIDECAR,
                       SUITE_UPDATER_TOKEN='secret', WB_DATA_DIR=str(self.data))

    def _write(self, payload: dict) -> None:
        (self.data / 'suite-update-status.json').write_text(
            json.dumps(payload), encoding='utf-8')

    def test_running_follows_helper_container(self) -> None:
        with mock.patch.object(self.m, '_helper_running', return_value=True):
            _, body = self.m.read_status()
        self.assertTrue(body['running'])
        with mock.patch.object(self.m, '_helper_running', return_value=False):
            _, body = self.m.read_status()
        self.assertFalse(body['running'])

    def test_reconciles_orphaned_running_state(self) -> None:
        """helper 已消失但状态还写着 running：要收尾，否则界面永远卡在"更新中"。"""
        self._write({'running': True, 'ok': None, 'step': '正在重建容器',
                     'started_at': 1000, 'finished_at': None, 'logs': []})
        with mock.patch.object(self.m, '_helper_running', return_value=False):
            _, body = self.m.read_status()
        st = body['status']
        self.assertFalse(st['running'])
        self.assertFalse(st['ok'])
        self.assertTrue(st['finished_at'])
        self.assertGreaterEqual(st['duration'], 0.0)

    def test_keeps_ok_when_helper_finished_normally(self) -> None:
        self._write({'running': True, 'ok': True, 'step': '更新完成',
                     'started_at': 1000, 'finished_at': None, 'logs': []})
        with mock.patch.object(self.m, '_helper_running', return_value=False):
            _, body = self.m.read_status()
        self.assertTrue(body['status']['ok'])
        self.assertFalse(body['status']['running'])

    def test_no_status_file_is_not_an_error(self) -> None:
        """首次部署时还没有状态文件，不该报错 —— 界面显示"暂无日志"即可。"""
        with mock.patch.object(self.m, '_helper_running', return_value=False):
            code, body = self.m.read_status()
        self.assertEqual(code, 200)
        self.assertIsNone(body['status'])


class StatusContractTest(unittest.TestCase):
    """状态文件 schema 是侧车/helper/管理端之间的契约，字段名错了界面就读不到。

    字段集刻意与上游 deploy/update.py 的 Reporter 对齐，这样管理端既有的
    日志面板与"进程消失但已落地"的判断都能直接复用。
    """

    REQUIRED = {'running', 'ok', 'step', 'logs', 'started_at', 'finished_at', 'duration'}

    def setUp(self) -> None:
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)
        self.data = Path(self._td.name)
        self.status = self.data / 'status.json'

    def test_sidecar_status_shape(self) -> None:
        m = _load('upd_sidecar_contract', _SIDECAR,
                  SUITE_UPDATER_TOKEN='t', WB_DATA_DIR=str(self.data))
        good = m._initial_status('x')
        self.assertTrue(self.REQUIRED.issubset(good))
        self.assertEqual(good['target'], 'suite')
        bad = m._fail_status('x', 'detail')
        self.assertFalse(bad['running'])
        self.assertFalse(bad['ok'])
        self.assertEqual(bad['logs'][-1]['level'], 'error')

    def test_runner_status_shape(self) -> None:
        m = _load('upd_runner_contract', _RUNNER,
                  SUITE_DIR=str(self.data), WB_UPDATE_STATUS=str(self.status))
        rep = m.Reporter()
        rep.add('hello')
        rep.set_step('步骤')
        running = rep.payload(True, None)
        self.assertTrue(self.REQUIRED.issubset(running))
        self.assertTrue(running['running'])
        self.assertIsNone(running['finished_at'])
        self.assertEqual(running['target'], 'suite')
        done = rep.payload(False, True)
        self.assertFalse(done['running'])
        self.assertIsNotNone(done['finished_at'])

    def test_runner_flush_writes_atomically(self) -> None:
        m = _load('upd_runner_flush', _RUNNER,
                  SUITE_DIR=str(self.data), WB_UPDATE_STATUS=str(self.status))
        rep = m.Reporter()
        rep.add('第一行')
        rep.flush(force=True)
        st = json.loads(self.status.read_text(encoding='utf-8'))
        self.assertEqual(st['logs'][-1]['text'], '第一行')
        # 临时文件必须已被 replace 掉，不留残渣
        self.assertFalse(self.status.with_suffix('.json.tmp').exists())

    def test_runner_hands_ownership_back_to_app(self) -> None:
        """helper 以 root 跑，写出的状态文件必须交还给 app 用户。

        不交还的后果很隐蔽：第一次更新能跑，第二次侧车写"初始状态"时
        permission denied（文件是 root 所有），表现为"按钮突然点不动"。
        """
        m = _load('upd_runner_handoff', _RUNNER,
                  SUITE_DIR=str(self.data), WB_UPDATE_STATUS=str(self.status))
        seen: list[Path] = []
        real = m._hand_off_to_app

        def spy(path):
            seen.append(Path(path))
            real(path)

        with mock.patch.object(m, '_hand_off_to_app', side_effect=spy):
            rep = m.Reporter()
            rep.add('x')
            rep.flush(force=True)
        self.assertEqual(len(seen), 1, '写状态文件时必须交还属主')
        self.assertTrue(str(seen[0]).endswith('.json.tmp'),
                        '应在 replace 之前对临时文件操作（replace 后路径已变）')

    def test_runner_log_is_capped(self) -> None:
        """日志上限：更新过程可能上百行，不设上限会让状态文件无界增长。"""
        m = _load('upd_runner_cap', _RUNNER,
                  SUITE_DIR=str(self.data), WB_UPDATE_STATUS=str(self.status))
        rep = m.Reporter()
        for i in range(m.LOG_LIMIT + 50):
            rep.add(f'line {i}')
        self.assertEqual(len(rep.logs), m.LOG_LIMIT)

    def test_runner_reads_dotenv(self) -> None:
        m = _load('upd_runner_dotenv', _RUNNER,
                  SUITE_DIR=str(self.data), WB_UPDATE_STATUS=str(self.status))
        (self.data / '.env').write_text(
            '# 注释\nSUITE_IMAGE=ghcr.io/o/r:v1.2.3\n\nQUOTED="x"\n', encoding='utf-8')
        env = m.read_dotenv()
        self.assertEqual(env['SUITE_IMAGE'], 'ghcr.io/o/r:v1.2.3')
        self.assertEqual(env['QUOTED'], 'x')

    def test_runner_gives_up_without_compose_file(self) -> None:
        """找不到 compose 文件时要写失败状态并给出可执行的排查方向。"""
        m = _load('upd_runner_nocompose', _RUNNER,
                  SUITE_DIR=str(self.data / 'nowhere'), WB_UPDATE_STATUS=str(self.status))
        with mock.patch.object(m, 'SUITE_DIR', self.data / 'nowhere'):
            rc = m.main()
        self.assertEqual(rc, 1)
        st = json.loads(self.status.read_text(encoding='utf-8'))
        self.assertFalse(st['ok'])
        self.assertIn('compose', st['logs'][-1]['text'])


if __name__ == '__main__':
    unittest.main()
