"""修正 Starlette `GZipMiddleware` 对 `q=0` 的误判。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

`GZipMiddleware` 用 `"gzip" in accept_encoding` 做子串判断（starlette 0.49.1
的 `__call__`）。于是 `Accept-Encoding: gzip;q=0` 这种**明确拒绝** gzip 的请求
仍然会收到 `Content-Encoding: gzip` —— 客户端要么解压失败，要么把压缩字节当
明文，属于协议违规。浏览器一般不发 q=0，但 API 客户端、代理与压测工具会。

处理方式：只在协商前做一次归一化 —— 若所有 `gzip` / `x-gzip` 词条的质量因子
都 <= 0，就把这些词条摘掉再交给内建中间件，让它走 identity 路径。压缩本身仍
完全交给 Starlette，不复制它的流式实现。正常浏览器（不带 q=0）的请求头一字
不改，行为零变化。

之所以不自己实现整套 gzip ASGI 中间件：内建件已经处理好了 SSE 排除、小响应
阈值、流式分块与 `Content-Length` 更新，重写一遍只会引入更多风险。
"""
from __future__ import annotations

from typing import Iterable

SUITE_MARKER = '【SUITE-OVERLAY】'

_GZIP_NAMES = ('gzip', 'x-gzip')


def _tokens(header: str) -> Iterable[tuple[str, float]]:
    """展开 Accept-Encoding：`(编码名小写, q)`。

    q 缺省为 1；非法 q 也按 1 处理（保守：请求方既然点名了这个编码，就别因为
    q 写坏而拒绝压缩 —— 与 wb2api 侧 `qAllows` 的口径一致）。
    """
    for part in header.split(','):
        fields = part.split(';')
        name = fields[0].strip().lower()
        if not name:
            continue
        q = 1.0
        for kv in fields[1:]:
            key, sep, value = kv.partition('=')
            if not sep or key.strip().lower() != 'q':
                continue
            try:
                q = float(value.strip())
            except ValueError:
                q = 1.0
        yield name, q


def accepts_gzip(header: str) -> bool:
    """客户端是否**明确**接受 gzip（q>0）。`*` 通配不算接受，与两边既有口径一致。"""
    return any(name in _GZIP_NAMES and q > 0 for name, q in _tokens(header))


def strip_declined_gzip(header: str) -> str:
    """去掉 `gzip` / `x-gzip` 词条，保留其余编码与原始写法。"""
    kept: list[str] = []
    for part in header.split(','):
        name = part.split(';', 1)[0].strip().lower()
        if not name or name in _GZIP_NAMES:
            continue
        kept.append(part.strip())
    return ', '.join(kept)


class GzipQ0Normalizer:
    """ASGI 外壳：在 `GZipMiddleware` **之前**摘掉被 q=0 拒绝的 gzip 词条。

    中间件顺序：Starlette 后注册的在外层（`add_middleware` 插到列表头部），
    所以本类必须在 `GZipMiddleware` **之后**注册。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get('type') == 'http':
            headers = list(scope.get('headers') or [])
            values = [
                value.decode('latin-1')
                for key, value in headers
                if key.lower() == b'accept-encoding'
            ]
            joined = ', '.join(values)
            # 只有「点名了 gzip、但全部 q<=0」才需要改写；没有 gzip 词条或
            # 有一条 q>0 时保持请求头原样，避免影响正常客户端。
            named = any(name in _GZIP_NAMES for name, _ in _tokens(joined))
            if named and not accepts_gzip(joined):
                headers = [
                    (key, value)
                    for key, value in headers
                    if key.lower() != b'accept-encoding'
                ]
                rest = strip_declined_gzip(joined)
                if rest:
                    headers.append((b'accept-encoding', rest.encode('latin-1')))
                scope = dict(scope)
                scope['headers'] = headers
        await self.app(scope, receive, send)
