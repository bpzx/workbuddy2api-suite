"""上游配置读写的回归测试：确保可视化字段不会把 config.json 写坏。

重点覆盖三个曾经的坑：
  1. *_hours 必须写成 []int（曾按数字间隔处理，会把 [9,21] 写成 6）
  2. 时长字段必须写成字符串（曾把 breaker_cooldown 当成秒数写整数）
  3. 只提交改动过的字段，不能覆盖同一段里其他未展示的键

运行：python -m unittest discover -s server -t . -p 'test_*.py'
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import wb2api  # noqa: E402


def write_cfg(path: Path, obj: dict) -> None:
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding='utf-8')


def read_cfg(path: Path) -> dict:
    return json.loads(path.read_text(encoding='utf-8'))


class UpstreamConfigRoundTrip(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg_path = Path(self._tmp.name) / 'config.json'
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self.cfg_path

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        self._tmp.cleanup()

    # ── 时刻数组 ─────────────────────────────────────────
    def test_hours_written_as_int_array(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {'checkin_hours': [9, 21]}})
        wb2api.save_upstream_config({'schedule': {'travel_hours': [9, 21]}})
        got = read_cfg(self.cfg_path)['schedule']['travel_hours']
        self.assertEqual(got, [9, 21])
        self.assertIsInstance(got, list)

    def test_hours_accepts_string_of_numbers(self) -> None:
        """前端按字符串提交时也能落到正确类型（兜底解析）。"""
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'schedule': {'activity_hours': [10]}})
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['activity_hours'], [10])

    def test_hours_dedup_and_sort(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'schedule': {'keepalive_hours': [22, 9, 22]}})
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['keepalive_hours'], [9, 22])

    def test_hours_reject_out_of_range(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {'checkin_hours': [9]}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': [24]}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': [-1]}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': []}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': 6}})
        # 拒绝后原配置保持不变
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['checkin_hours'], [9])

    # ── 时长字符串 ───────────────────────────────────────
    def test_duration_fields_stay_strings(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config(
            {
                'cooldown': {'soft_rate': '600s', 'soft_rate_max': '2h'},
                'pool': {'breaker_cooldown': '30m', 'breaker_cooldown_max': '6h'},
                'session_sticky': {'ttl': '30m', 'gc_interval': '5m'},
            }
        )
        cfg = read_cfg(self.cfg_path)
        self.assertEqual(cfg['cooldown']['soft_rate'], '600s')
        self.assertEqual(cfg['pool']['breaker_cooldown'], '30m')
        self.assertEqual(cfg['session_sticky']['ttl'], '30m')

    def test_duration_reject_bad_format(self) -> None:
        write_cfg(self.cfg_path, {'pool': {'breaker_cooldown': '30m'}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'breaker_cooldown': '600'}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'cooldown': {'soft_rate': 'soon'}})
        self.assertEqual(read_cfg(self.cfg_path)['pool']['breaker_cooldown'], '30m')

    # ── 不覆盖同段其他键 ─────────────────────────────────
    def test_partial_patch_preserves_siblings(self) -> None:
        write_cfg(
            self.cfg_path,
            {
                'schedule': {'checkin_hours': [9, 21], 'keepalive_hours': [22]},
                'pool': {'max_in_flight': 3},
            },
        )
        wb2api.save_upstream_config({'schedule': {'checkin_hours': [8]}})
        cfg = read_cfg(self.cfg_path)
        self.assertEqual(cfg['schedule']['checkin_hours'], [8])
        self.assertEqual(cfg['schedule']['keepalive_hours'], [22])
        self.assertEqual(cfg['pool']['max_in_flight'], 3)

    def test_bool_switch_round_trip(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {'checkin_enabled': True}})
        wb2api.save_upstream_config({'schedule': {'checkin_enabled': False}})
        self.assertIs(read_cfg(self.cfg_path)['schedule']['checkin_enabled'], False)

    def test_unknown_section_ignored(self) -> None:
        write_cfg(self.cfg_path, {'api_key': 'secret'})
        wb2api.save_upstream_config({'api_key': 'hacked'})
        self.assertEqual(read_cfg(self.cfg_path)['api_key'], 'secret')

    # ── prompt / server / upstream（新增段）─────────────────
    def test_prompt_mode_round_trip(self) -> None:
        write_cfg(self.cfg_path, {'prompt': {'mode': 'custom'}})
        wb2api.save_upstream_config({'prompt': {'mode': 'passthrough'}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'passthrough')

    def test_prompt_mode_normalised_and_validated(self) -> None:
        write_cfg(self.cfg_path, {'prompt': {'mode': 'custom'}})
        wb2api.save_upstream_config({'prompt': {'mode': 'Passthrough'}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'passthrough')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'prompt': {'mode': 'replace'}})
        # 拒绝后保持原值
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], 'passthrough')

    def test_prompt_file_rejects_control_chars(self) -> None:
        write_cfg(self.cfg_path, {'prompt': {'file': ''}})
        wb2api.save_upstream_config({'prompt': {'file': '/etc/prompt.md'}})
        self.assertEqual(read_cfg(self.cfg_path)['prompt']['file'], '/etc/prompt.md')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'prompt': {'file': '/a\nb'}})

    def test_both_prompt_modes_still_accepted(self) -> None:
        """两种模式都必须能写入 —— 上游只是改了**缺省值**，没有删掉 custom。

        上游 2026-09-14 把 prompt.mode 缺省从 custom 改成 passthrough（透传
        客户端原始 system），显式配 custom 仍受支持。管理端不该因为默认值
        变了就拒绝其中任何一个。
        """
        for mode in ('passthrough', 'custom'):
            write_cfg(self.cfg_path, {'prompt': {'mode': 'passthrough'}})
            wb2api.save_upstream_config({'prompt': {'mode': mode}})
            self.assertEqual(read_cfg(self.cfg_path)['prompt']['mode'], mode)

    def test_empty_prompt_section_is_not_invented(self) -> None:
        """配置里没有 prompt 段时，不该被管理端凭空造出来。

        这条守的是「UI 默认值 = 上游默认值」这个不变量：前端把缺省字段显示为
        默认值，保存时只提交**改动过**的键；这里从后端确认「没改就不写」，
        否则老配置会在一次无关的保存里多出 prompt 段（虽然值相同，但属于
        不该发生的写入）。
        """
        write_cfg(self.cfg_path, {'pool': {'max_in_flight': 3}})
        wb2api.save_upstream_config({'pool': {'max_in_flight': 5}})
        cfg = read_cfg(self.cfg_path)
        self.assertNotIn('prompt', cfg, '不该凭空写入 prompt 段')
        self.assertEqual(cfg['pool']['max_in_flight'], 5)

    def test_server_max_body_mb_validation(self) -> None:
        write_cfg(self.cfg_path, {'server': {'max_body_mb': 8}})
        wb2api.save_upstream_config({'server': {'max_body_mb': 32}})
        self.assertEqual(read_cfg(self.cfg_path)['server']['max_body_mb'], 32)
        for bad in (0, -1, 257, 'many'):
            with self.assertRaises(ValueError):
                wb2api.save_upstream_config({'server': {'max_body_mb': bad}})
        self.assertEqual(read_cfg(self.cfg_path)['server']['max_body_mb'], 32)

    def test_upstream_user_agent_round_trip(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {'user_agent': 'MyClient/1.0'}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['user_agent'], 'MyClient/1.0')
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'upstream': {'user_agent': 'bad\nua'}})

    def test_new_sections_do_not_touch_others(self) -> None:
        """保存 prompt 不应影响 timeout 等同段的其他键。"""
        write_cfg(
            self.cfg_path,
            {'upstream': {'timeout_seconds': 120, 'idle_timeout_seconds': 300}},
        )
        wb2api.save_upstream_config({'upstream': {'user_agent': 'X/1'}})
        cfg = read_cfg(self.cfg_path)['upstream']
        self.assertEqual(cfg['timeout_seconds'], 120)
        self.assertEqual(cfg['idle_timeout_seconds'], 300)
        self.assertEqual(cfg['user_agent'], 'X/1')

    # ── 整数字段区间（前端 max 只是输入框属性，服务端必须兜底）──
    def test_activity_report_count_range(self) -> None:
        write_cfg(self.cfg_path, {'schedule': {}})
        # 合法：1（旧行为）~ 50
        for v in (1, 5, 20, 50):
            wb2api.save_upstream_config({'schedule': {'activity_report_count': v}})
            self.assertEqual(read_cfg(self.cfg_path)['schedule']['activity_report_count'], v)
        # 非法：越界 / 零 / 负数 / 非整数 / bool
        for bad in (0, -1, 51, 999, 100000, 'x', 3.5, True):
            with self.assertRaises(ValueError, msg=f'{bad!r} 应被拒绝'):
                wb2api.save_upstream_config({'schedule': {'activity_report_count': bad}})
        # 拒绝后配置保持最后一次合法值
        self.assertEqual(read_cfg(self.cfg_path)['schedule']['activity_report_count'], 50)

    def test_pool_int_ranges(self) -> None:
        write_cfg(self.cfg_path, {'pool': {}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'max_in_flight': 999}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'breaker_threshold': 0}})
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'pool': {'idle_weight_max': -1}})
        # 合法值可通过
        wb2api.save_upstream_config({'pool': {'max_in_flight': 3, 'breaker_threshold': 3}})
        self.assertEqual(read_cfg(self.cfg_path)['pool']['max_in_flight'], 3)

    def test_pool_float_range(self) -> None:
        write_cfg(self.cfg_path, {'pool': {}})
        for v in (0, 0.5, 100.0):
            wb2api.save_upstream_config({'pool': {'idle_weight_per_hour': v}})
        self.assertEqual(read_cfg(self.cfg_path)['pool']['idle_weight_per_hour'], 100.0)
        for bad in (-1, 101, 'fast', True):
            with self.assertRaises(ValueError, msg=f'{bad!r} 应被拒绝'):
                wb2api.save_upstream_config({'pool': {'idle_weight_per_hour': bad}})

    # ── upstream 段的凭据与新增字段 ─────────────────────
    def test_device_token_masked_in_view(self) -> None:
        """device_token 是设备风控凭据，绝不能明文下发。"""
        write_cfg(self.cfg_path, {
            'upstream': {'device_token': 'DEVICE-SECRET-abcdef123456', 'client_name': 'WorkBuddy'},
        })
        view = wb2api.load_upstream_config()
        raw = json.dumps(view, ensure_ascii=False)
        self.assertNotIn('DEVICE-SECRET-abcdef123456', raw, '凭据不应出现在接口返回里')
        self.assertTrue(view['upstream'].get('has_device_token'))
        self.assertIn('device_token_masked', view['upstream'])

    def test_device_token_empty_keeps_original(self) -> None:
        """留空表示保持原值——前端拿到的是掩码，不能被当成清空。"""
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'ORIG'}})
        wb2api.save_upstream_config({'upstream': {'device_token': ''}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['device_token'], 'ORIG')

    def test_device_token_none_clears(self) -> None:
        """显式 null 才清除（否则没有办法删掉它）。"""
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'ORIG', 'client_name': 'X'}})
        wb2api.save_upstream_config({'upstream': {'device_token': None}})
        up = read_cfg(self.cfg_path)['upstream']
        self.assertNotIn('device_token', up)
        self.assertEqual(up['client_name'], 'X', '不应误删同段其他键')

    def test_device_token_new_value_replaces(self) -> None:
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'OLD'}})
        wb2api.save_upstream_config({'upstream': {'device_token': 'NEW'}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['device_token'], 'NEW')

    def test_device_token_rejects_bad_value(self) -> None:
        write_cfg(self.cfg_path, {'upstream': {'device_token': 'ORIG'}})
        for bad in ('a' + chr(10) + 'b', 'x' * 600):
            with self.assertRaises(ValueError):
                wb2api.save_upstream_config({'upstream': {'device_token': bad}})
        self.assertEqual(read_cfg(self.cfg_path)['upstream']['device_token'], 'ORIG')

    def test_upstream_new_text_fields(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {
            'client_name': 'WorkBuddy', 'client_version': '5.5.4',
            'cli_version': '2.137.1', 'device_token_file': '/etc/tok',
        }})
        up = read_cfg(self.cfg_path)['upstream']
        self.assertEqual(up['client_name'], 'WorkBuddy')
        self.assertEqual(up['client_version'], '5.5.4')
        self.assertEqual(up['cli_version'], '2.137.1')
        self.assertEqual(up['device_token_file'], '/etc/tok')

    def test_upstream_passthrough_ip_bool(self) -> None:
        write_cfg(self.cfg_path, {})
        wb2api.save_upstream_config({'upstream': {'passthrough_ip': True}})
        self.assertIs(read_cfg(self.cfg_path)['upstream']['passthrough_ip'], True)
        with self.assertRaises(ValueError):
            wb2api.save_upstream_config({'upstream': {'passthrough_ip': 'yes'}})

    def test_missing_config_refuses_to_write(self) -> None:
        with self.assertRaises(FileNotFoundError):
            wb2api.save_upstream_config({'schedule': {'checkin_hours': [9]}})
        self.assertFalse(self.cfg_path.exists())


class TokenTTL(unittest.TestCase):
    """从 JWT 解出令牌总时长（供进度条按真实比例展示）。"""

    @staticmethod
    def _jwt(iat: int, exp: int) -> str:
        import base64, json as _json
        def seg(obj):
            raw = _json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        return f'{seg({"alg": "RS256"})}.{seg({"iat": iat, "exp": exp})}.sig'

    def test_reads_lifetime(self) -> None:
        ttl = wb2api.token_ttl_seconds(self._jwt(1000, 1000 + 60 * 86400))
        self.assertEqual(ttl, 60 * 86400)

    def test_garbage_returns_none(self) -> None:
        for bad in ('', 'not-a-jwt', 'a.b.c', 'onlyonesegment'):
            self.assertIsNone(wb2api.token_ttl_seconds(bad), bad)

    def test_missing_or_invalid_claims(self) -> None:
        import base64, json as _json
        def seg(obj):
            raw = _json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        # 缺 exp
        self.assertIsNone(wb2api.token_ttl_seconds(f'{seg({})}.{seg({"iat": 1})}.s'))
        # exp <= iat
        self.assertIsNone(wb2api.token_ttl_seconds(f'{seg({})}.{seg({"iat": 100, "exp": 50})}.s'))

    def test_issued_at_reads_iat(self) -> None:
        """签发时间（≈ 最近一次刷新）用于区分「刚续期」与「从没刷新过」。

        背景：界面的「有效期」是剩余时间，刷新会重新拉满，所以单看天数分不清
        「刚被保活续期」和「一直用着当初扫码的长令牌」——后者才是隐患账号。
        """
        iat = 1_700_000_000
        self.assertEqual(wb2api.token_issued_at(self._jwt(iat, iat + 7 * 86400)), iat)

    def test_issued_at_garbage_returns_none(self) -> None:
        for bad in ('', 'not-a-jwt', 'a.b.c', 'two.parts'):
            self.assertIsNone(wb2api.token_issued_at(bad), bad)

    def test_issued_at_matches_ttl_source(self) -> None:
        """两者必须来自同一个令牌，避免显示与进度条基准不一致。"""
        iat, exp = 1_700_000_000, 1_700_000_000 + 60 * 86400
        tok = self._jwt(iat, exp)
        self.assertEqual(wb2api.token_issued_at(tok), iat)
        self.assertEqual(wb2api.token_ttl_seconds(tok), exp - iat)


class ExpiryFallback(unittest.TestCase):
    """JWT 解不出时的兜底：用 auth 文件 mtime 推算窗口，而不是假定 60 天。

    原来的兜底是「解不出就当 60 天」，问题在于：一个 7 天的令牌按 60 天算，
    进度条只显示 12%，看着像快过期——属于误报。而 JWT 解不出并非罕见：
    扁平形手写 auth 文件的 accessToken 常常不是标准 JWT。

    用 mtime 是有依据的：上游刷新 token 后会**原子写回**该文件，安装器写盘
    也走同一路径，因此 mtime ≈ 最近一次写入/刷新时刻，exp - mtime 即本次
    有效期的近似值。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, name: str, uid: str, expires_at: int, token: str,
               mtime: int) -> Path:
        p = Path(self._tmp.name) / name
        p.write_text(json.dumps({
            'account': {'uid': uid, 'nickname': uid},
            'auth': {'accessToken': token, 'expiresAt': expires_at},
        }), encoding='utf-8')
        os.utime(p, (mtime, mtime))
        return p

    def _acct(self, uid: str) -> dict:
        return next(a for a in wb2api.list_auth_accounts() if a['uid'] == uid)

    @staticmethod
    def _jwt(iat: int, exp: int) -> str:
        import base64
        def seg(obj):
            raw = json.dumps(obj).encode()
            return base64.urlsafe_b64encode(raw).decode().rstrip('=')
        return f'{seg({"alg": "RS256"})}.{seg({"iat": iat, "exp": exp})}.sig'

    def test_mtime_fallback_uses_file_window_not_60_days(self) -> None:
        import time
        now = int(time.time())
        # 非 JWT + 7 天有效期 + 刚写入
        self._write('workbuddy-1.json', '1', now + 7 * 86400, 'not-a-jwt', now)
        a = self._acct('1')
        self.assertEqual(a['ttl_seconds'], 7 * 86400)
        self.assertEqual(a['issued_at'], now)

    def test_jwt_takes_priority_over_mtime(self) -> None:
        import time
        now = int(time.time())
        # JWT 说 7 天窗口、3 天前签发；mtime 设成现在也不能覆盖 JWT
        token = self._jwt(now - 3 * 86400, now + 4 * 86400)
        self._write('workbuddy-2.json', '2', now + 4 * 86400, token, now)
        a = self._acct('2')
        self.assertEqual(a['ttl_seconds'], 7 * 86400)
        self.assertEqual(a['issued_at'], now - 3 * 86400)

    def test_no_usable_signal_stays_none(self) -> None:
        """mtime 晚于 expiresAt（异常数据）时不能算出负数窗口。"""
        import time
        now = int(time.time())
        self._write('workbuddy-3.json', '3', now - 100, 'not-a-jwt', now)
        a = self._acct('3')
        self.assertIsNone(a['ttl_seconds'])
        self.assertIsNone(a['issued_at'])


if __name__ == '__main__':
    unittest.main()
