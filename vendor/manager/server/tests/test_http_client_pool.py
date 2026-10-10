"""出站客户端要**复用**，且不能跨事件循环复用（issue #144）。

报障：Windows 原生部署上切换页面要 1~3 秒，根因是每次出站都新建 httpx.AsyncClient
——在那台机器上「仅构造 + aclose」就要约 1 秒（加载系统证书库建 SSL 上下文），
而账号/状态/模型这些接口每次都要问上游几次。

这里钉住四件事：
  1. 同一个循环 + 同一组超时/代理 → **同一个客户端对象**（复用的全部意义）；
  2. 不同超时组合 → 各自一个（不能把 5 秒超时和 30 秒超时混成一个）；
  3. `async with` 退出时**不关闭**（否则等于每次还是新建）；
  4. 跨事件循环不复用（httpx 的连接池绑在创建它的循环上；同步路由里
     `asyncio.run` 另起循环那条路——见 modelcatalog.fetch_ids_blocking——
     拿到旧循环的客户端就会炸）。
"""
from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from server import config  # noqa: E402


class HttpClientReuseTest(unittest.TestCase):
    def setUp(self) -> None:
        config._CLIENTS.clear()
        self.addCleanup(config._CLIENTS.clear)

    def test_same_config_borrows_the_same_client(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as first:
                pass
            async with config.http_client(10, connect=3) as second:
                pass
            return first, second

        first, second = asyncio.run(scenario())
        self.assertIs(first, second,
                      '两次取到的不是同一个客户端 —— 等于每次还是新建（issue #144）')

    def test_borrowed_client_stays_open(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as client:
                pass
            return client

        client = asyncio.run(scenario())
        self.assertFalse(client.is_closed,
                         '借用期间被关掉了：连接池每次都得重建，复用的意义就没了')

    def test_different_timeouts_get_different_clients(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as slow:
                pass
            async with config.http_client(5, connect=2) as quick:
                pass
            return slow, quick

        slow, quick = asyncio.run(scenario())
        self.assertIsNot(slow, quick,
                         '不同超时配置共用一个客户端会把二者的超时混在一起')
        self.assertEqual(slow.timeout.connect, 3)
        self.assertEqual(quick.timeout.connect, 2)

    def test_different_proxy_gets_its_own_client(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3, proxy='') as direct:
                pass
            async with config.http_client(10, connect=3, proxy='http://127.0.0.1:7890') as via:
                pass
            return direct, via

        direct, via = asyncio.run(scenario())
        self.assertIsNot(direct, via, '走代理由的账号不能复用直连的客户端')

    def test_never_shares_across_event_loops(self) -> None:
        """跨循环复用会炸（连接池绑在旧循环上）—— 必须各建一个。"""
        async def one():
            async with config.http_client(10, connect=3) as client:
                return client

        first = asyncio.run(one())
        second = asyncio.run(one())
        self.assertIsNot(first, second,
                         '第二个事件循环拿到了前一个循环的客户端 —— 这正是同步路由'
                         '另起循环时最危险的那种复用')
        self.assertFalse(second.is_closed, '第二个循环里的客户端必须能用')

    def test_no_new_client_construction_on_second_borrow(self) -> None:
        """计数式断言：第二次借用时**没有**再构造 httpx.AsyncClient。"""
        import httpx

        real_init = httpx.AsyncClient.__init__
        built: list[int] = []

        def counting_init(self, *a, **kw):  # noqa: ANN001
            built.append(1)
            return real_init(self, *a, **kw)

        async def scenario():
            for _ in range(3):
                async with config.http_client(30, connect=3) as client:
                    pass
            return client

        with mock.patch.object(httpx.AsyncClient, '__init__', counting_init):
            client = asyncio.run(scenario())
        self.assertEqual(len(built), 1,
                         f'三次借用构造了 {len(built)} 个客户端，应当只建一次')
        asyncio.run(_aclose(client))

    def test_close_clients_closes_and_clears(self) -> None:
        async def scenario():
            async with config.http_client(10, connect=3) as client:
                pass
            await config.close_clients()
            return client

        client = asyncio.run(scenario())
        self.assertTrue(client.is_closed, 'close_clients 之后仍开着')
        self.assertEqual(config._CLIENTS, {}, '池没清空')

    def test_outside_event_loop_is_not_cached(self) -> None:
        """同步上下文（没有正在跑的循环）不缓存：不留绑定不明的客户端。"""
        direct = config.http_client(10, connect=3)
        self.assertTrue(hasattr(direct, 'aclose'),
                        '同步上下文应直接返回可用的客户端（而不是借用包装）')
        self.assertEqual(config._CLIENTS, {}, '同步上下文不该往池里塞东西')
        asyncio.run(_aclose(direct))


    def test_closing_a_borrowed_client_does_not_kill_the_shared_one(self) -> None:
        """自己 `client = http_client(...)` 再 `aclose()` 的写法不能真的关掉共享客户端。

        网关 / Anthropic 兼容 / 测试台 / Responses 四处就是这种写法。共享之后，
        那里的 aclose 一旦真关，会把别人正在用的连接池一起关掉 —— 并发下就是
        「偶发请求失败」。借来的客户端只支持「归还」。
        """

        async def scenario():
            async with config.http_client(10, connect=3) as first:
                pass
            borrowed = config.http_client(10, connect=3)   # 不用 async with 的写法
            await borrowed.aclose()                        # 旧写法里的 finally 分支
            async with config.http_client(10, connect=3) as again:
                pass
            return first, again

        first, again = asyncio.run(scenario())
        self.assertFalse(first.is_closed, '借来的客户端被 aclose 真关掉了')
        self.assertIs(first, again, '关过一次之后没从池里复用 —— 退化成每次新建')

    def test_borrowed_wrapper_forwards_client_calls(self) -> None:
        """包装要能当客户端用（post/stream/headers…），否则不用 async with 的写法会崩。"""
        async def scenario():
            borrowed = config.http_client(10, connect=3)
            return borrowed, borrowed.timeout, borrowed.headers

        borrowed, timeout, headers = asyncio.run(scenario())
        self.assertTrue(hasattr(borrowed, 'post') and hasattr(borrowed, 'stream'),
                        '属性转发没生效，`client.post(...)` 会 AttributeError')
        self.assertEqual(timeout.connect, 3)
        self.assertIn('user-agent', {k.lower() for k in headers})
        asyncio.run(config.close_clients())


async def _aclose(client) -> None:  # noqa: ANN001
    await client.aclose()


if __name__ == '__main__':
    unittest.main()
