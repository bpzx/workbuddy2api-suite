"""版本（realm）适配的回归测试：国内版 / 国际版。

上游从 2026-09-14 起单实例同时支持两套上游（共用账号池，按账号 realm 或
模型名前缀路由）。这里锁定几个容易写错、且写错就会「打错上游」的点：

  1. realm 判定顺序：显式字段 > 域名后缀；**逃生门（enabled=false）恒 cn**
  2. 落盘用的 resolve_realm 不受逃生门影响（否则会把国际版账号永久写成 cn）
  3. 端点分派：billing 的路径候选顺序**两边相反**（照上游实现，不是笔误）
  4. 国际版没有签到体系 → 直接跳过，不打上游请求
  5. 登录 state 的 realm 一致性（防止跨版本串用授权码）
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402
from server.services import realm, tencent  # noqa: E402


class RealmDetectTest(unittest.TestCase):
    def test_explicit_field_wins(self) -> None:
        self.assertEqual(realm.resolve_realm('global', 'www.codebuddy.cn'), 'global')
        self.assertEqual(realm.resolve_realm('cn', 'www.workbuddy.ai'), 'cn')

    def test_domain_fallback(self) -> None:
        self.assertEqual(realm.resolve_realm(None, 'www.workbuddy.ai'), 'global')
        self.assertEqual(realm.resolve_realm(None, 'api.workbuddy.ai'), 'global')
        self.assertEqual(realm.resolve_realm(None, 'WORKBUDDY.AI'), 'global')
        self.assertEqual(realm.resolve_realm(None, 'www.codebuddy.cn'), 'cn')

    def test_empty_is_cn(self) -> None:
        """存量账号既无 realm 也无 domain → cn，保证升级后行为不变。"""
        self.assertEqual(realm.resolve_realm(None, ''), 'cn')
        self.assertEqual(realm.resolve_realm(None, None), 'cn')

    def test_unknown_explicit_falls_back_to_domain(self) -> None:
        self.assertEqual(realm.resolve_realm('bogus', 'www.workbuddy.ai'), 'global')

    def test_escape_hatch_forces_cn(self) -> None:
        """逃生门（global.enabled=false）下 Realm() 恒 cn，但落盘解析不受影响。"""
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': False, 'chat_base': '', 'billing_base': ''}):
            self.assertEqual(realm.realm_of({'realm': 'global'}), 'cn')
            self.assertEqual(realm.realm_of({'domain': 'www.workbuddy.ai'}), 'cn')
            self.assertFalse(realm.global_enabled())
            # 关键：落盘时必须仍按真实归属解析，否则国际版账号会被写成 cn
            self.assertEqual(realm.resolve_realm('global', ''), 'global')

    def test_enabled_by_default(self) -> None:
        self.assertTrue(realm.global_enabled())


class EndpointTest(unittest.TestCase):
    def test_chat_base_per_realm(self) -> None:
        self.assertEqual(realm.chat_base('cn'), 'https://copilot.tencent.com')
        self.assertEqual(realm.chat_base('global'), 'https://www.workbuddy.ai')

    def test_billing_base_per_realm(self) -> None:
        self.assertEqual(realm.billing_base('cn'), 'https://www.codebuddy.cn')
        self.assertEqual(realm.billing_base('global'), 'https://www.workbuddy.ai')

    def test_chat_paths(self) -> None:
        self.assertEqual(realm.chat_paths('cn'), ['/v2/chat/completions'])
        self.assertEqual(realm.chat_paths('global'),
                         ['/console/chat/completions', '/v2/chat/completions'])

    def test_billing_path_order_is_reversed(self) -> None:
        """国际版无 /v2 前缀优先，国内版只有 /v2——两者顺序相反，照上游实现。"""
        self.assertEqual(realm.billing_paths('cn', 'user-resource'),
                         ['/v2/billing/meter/get-user-resource'])
        self.assertEqual(realm.billing_paths('global', 'user-resource'),
                         ['/billing/meter/get-user-resource',
                          '/v2/billing/meter/get-user-resource'])
        self.assertEqual(realm.billing_paths('cn', 'daily-checkin'),
                         ['/v2/billing/meter/daily-checkin'])

    def test_origin_and_ua_per_realm(self) -> None:
        cn = realm.headers('cn')
        gl = realm.headers('global')
        self.assertEqual(cn['Origin'], 'https://www.codebuddy.cn')
        self.assertEqual(gl['Origin'], 'https://www.workbuddy.ai')
        # 品牌段不同：国际版是 "WorkBuddy AI"
        self.assertIn('WorkBuddy/', cn['User-Agent'])
        self.assertIn('WorkBuddy AI/', gl['User-Agent'])
        self.assertNotIn('WorkBuddy AI', cn['User-Agent'])

    # ── 出站风控头对齐上游 2026-09-14 的改动（D1/D5/D6）──
    #
    # 管理端有一批请求**绕过上游直连腾讯**（扫码登录、签到、积分、trial、
    # 注册）。上游给它自己的出站加了这些头；我们这条路若不跟，就会成为
    # 唯一「形态不像官方客户端」的流量，被风控挑出来的代价是账号被封。

    def test_outbound_risk_control_headers_present(self) -> None:
        """D1：X-CodeBuddy-Request 是官方客户端的风控闸门头，所有请求必带。"""
        for r in ('cn', 'global'):
            h = realm.headers(r)
            self.assertEqual(h.get('X-CodeBuddy-Request'), '1', f'{r} 缺风控闸门头')

    def test_accept_language_switches_by_realm(self) -> None:
        """D5：按账号域切语言标识（官方客户端就是这么发的）。"""
        self.assertEqual(realm.headers('cn')['Accept-Language'], 'zh-CN')
        self.assertEqual(realm.headers('global')['Accept-Language'], 'en-US')

    def test_accept_tightened_for_non_stream(self) -> None:
        """D6：非流式收紧为 application/json，不再带宽松的 text/plain, */*。"""
        for r in ('cn', 'global'):
            accept = realm.headers(r)['Accept']
            self.assertEqual(accept, 'application/json', f'{r} 的 Accept 未收紧')
            self.assertNotIn('text/plain', accept)

    def test_chat_stream_overrides_accept(self) -> None:
        """流式路径才声明 event-stream —— 由调用方覆盖（probe_account 等）。"""
        h = realm.headers('cn', 'tok')
        self.assertEqual(h['Authorization'], 'Bearer tok')
        # 确认「能覆盖」这件事本身成立（调用方据此改写）
        h['Accept'] = 'application/json, text/event-stream'
        self.assertIn('text/event-stream', h['Accept'])

    def test_static_cn_headers_match_realm_headers(self) -> None:
        """config.TENCENT_HEADERS 目前无调用点，但必须与 realm.headers 同口径。

        否则将来有人照着它取值，就发出与风控口径矛盾的请求 —— 这类"看起来
        能用"的静态常量最容易成为下一个坑。
        """
        static = config.TENCENT_HEADERS
        live = realm.headers('cn')
        for key in ('Accept', 'Accept-Language', 'X-CodeBuddy-Request',
                    'Content-Type', 'X-Requested-With', 'Origin', 'Referer'):
            self.assertEqual(static.get(key), live.get(key),
                             f'TENCENT_HEADERS.{key} 与 realm.headers("cn") 不一致')

    def test_custom_base_overrides_default(self) -> None:
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True,
                                             'chat_base': 'https://intl.example.com',
                                             'billing_base': 'https://bill.example.com'}):
            realm.invalidate()
            self.assertEqual(realm.chat_base('global'), 'https://intl.example.com')
            self.assertEqual(realm.billing_base('global'), 'https://bill.example.com')
        realm.invalidate()

    def test_cn_base_unaffected_by_global_config(self) -> None:
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': True,
                                             'chat_base': 'https://intl.example.com',
                                             'billing_base': ''}):
            self.assertEqual(realm.chat_base('cn'), 'https://copilot.tencent.com')


class CheckinSupportTest(unittest.TestCase):
    def test_cn_supported_global_not(self) -> None:
        self.assertTrue(realm.supports_checkin('cn'))
        self.assertFalse(realm.supports_checkin('global'))

    def test_global_checkin_skips_without_request(self) -> None:
        """国际版签到必须直接返回，不能打上游请求（避免风控）。"""
        import asyncio
        called = []

        class Boom:
            def __init__(self, *a, **k):
                called.append(1)

        with mock.patch.object(config, 'http_client', Boom):
            code, msg = asyncio.run(tencent.checkin('tok', 'global'))
        self.assertEqual(code, -2)
        self.assertIn('国际版', msg)
        self.assertEqual(called, [], '不应发起任何 HTTP 请求')


class StateRealmTest(unittest.TestCase):
    """扫码 state 的版本一致性：跨版本轮询必须被拒绝。"""

    def setUp(self) -> None:
        tencent._state_cache.clear()

    def tearDown(self) -> None:
        tencent._state_cache.clear()

    def test_registered_realm_recorded(self) -> None:
        import asyncio
        import time
        # 直接写缓存模拟 start_login 的登记
        tencent._state_cache['s1'] = (time.time(), 'global')
        self.assertEqual(tencent.state_realm('s1'), 'global')
        self.assertTrue(tencent.is_pending('s1'))
        # 用国内版去轮询 → 明确报不匹配，而不是继续往下走
        out = asyncio.run(tencent.poll_login('s1', 'cn'))
        self.assertEqual(out['status'], 'realm_mismatch')
        self.assertEqual(out['expected'], 'global')
        self.assertEqual(out['got'], 'cn')
        # 不 drop：用户切回去还能继续用
        self.assertTrue(tencent.is_pending('s1'))

    def test_unknown_state_invalid(self) -> None:
        import asyncio
        out = asyncio.run(tencent.poll_login('nope', 'cn'))
        self.assertEqual(out['status'], 'invalid')


class WriteAuthFileTest(unittest.TestCase):
    """落盘要带 realm，且用不含逃生门的解析。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._orig = config.AUTH_DIR
        config.AUTH_DIR = Path(self._tmp.name)

    def tearDown(self) -> None:
        config.AUTH_DIR = self._orig
        self._tmp.cleanup()

    def _write(self, **kw) -> dict:
        acct = {
            'uid': kw.get('uid', '123'),
            'access_token': 'tok',
            'refresh_token': 'rt',
            'expires_at': 9999999999,
            'domain': kw.get('domain', ''),
            'realm': kw.get('realm'),
            'nickname': 'N',
            'enterprise_id': '',
        }
        name, _ = tencent.write_auth_file(acct)
        return json.loads((config.AUTH_DIR / name).read_text(encoding='utf-8'))

    def test_realm_written_into_auth_object(self) -> None:
        d = self._write(realm='global', domain='www.workbuddy.ai')
        self.assertEqual(d['auth']['realm'], 'global')
        # 与 domain 同级（上游就是从这里读的）
        self.assertIn('domain', d['auth'])
        self.assertNotIn('realm', d)

    def test_domain_inferred_when_realm_missing(self) -> None:
        d = self._write(domain='www.workbuddy.ai')
        self.assertEqual(d['auth']['realm'], 'global')

    def test_cn_default(self) -> None:
        d = self._write(domain='www.codebuddy.cn')
        self.assertEqual(d['auth']['realm'], 'cn')

    def test_write_not_affected_by_escape_hatch(self) -> None:
        """逃生门开着时也不能把国际版账号写成 cn（上游专门警告过）。"""
        with mock.patch.object(realm, '_read_global_config',
                               return_value={'enabled': False, 'chat_base': '', 'billing_base': ''}):
            d = self._write(realm='global', domain='www.workbuddy.ai')
        self.assertEqual(d['auth']['realm'], 'global')


class GlobalConfigSectionTest(unittest.TestCase):
    """global 段此前不在写入白名单里，保存会被静默吞掉。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._path = Path(self._tmp.name) / 'config.json'
        self._path.write_text(json.dumps({'upstream': {}, 'server': {}}), encoding='utf-8')
        self._orig = config.UPSTREAM_CONFIG
        config.UPSTREAM_CONFIG = self._path
        realm.invalidate()

    def tearDown(self) -> None:
        config.UPSTREAM_CONFIG = self._orig
        realm.invalidate()
        try:
            self._tmp.cleanup()
        except PermissionError:
            pass

    def _save(self, patch: dict) -> dict:
        from server.services import wb2api
        return wb2api.save_upstream_config(patch)

    def test_global_section_is_persisted(self) -> None:
        self._save({'global': {'enabled': True, 'chat_base': 'https://intl.example.com/'}})
        saved = json.loads(self._path.read_text(encoding='utf-8'))
        self.assertIn('global', saved, 'global 段被静默丢弃了')
        # 归一化：去掉尾部斜杠
        self.assertEqual(saved['global']['chat_base'], 'https://intl.example.com')

    def test_invalid_values_rejected(self) -> None:
        for bad, why in [
            ({'enabled': 'yes'}, 'enabled 必须是布尔'),
            ({'chat_base': 'not-a-url'}, '需以 http 开头'),
            ({'chat_base': 'http://a\nb'}, '不能含控制字符'),
        ]:
            with self.assertRaises(ValueError, msg=why):
                self._save({'global': bad})

    def test_saving_invalidates_realm_cache(self) -> None:
        """改完 base 应立即生效，而不是等 10 秒缓存过期。"""
        self._save({'global': {'chat_base': 'https://intl.example.com'}})
        self.assertEqual(realm.chat_base('global'), 'https://intl.example.com')


if __name__ == '__main__':
    unittest.main()
