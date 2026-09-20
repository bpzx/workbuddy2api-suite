"""上游重载编排：把「改配置 / 增删账号后需要重启容器」收敛为自动、可观测的动作。

背景
----
workbuddy2api 只在进程启动时读取 config.json 与扫描 auths/ 目录
（见 cmd/server/main.go：auth.LoadDir + p.SyncToDir + 各 SetXxx 注入），
之后不再重读，也不处理 SIGHUP。因此任何影响这两者的改动都必须重启容器。

**一处例外**（上游 2026-09-18 起）：auths 目录加了热加载——每 5 秒轮询目录指纹，
凭证文件增删改会自动重新加载，那时不必重启（见其 internal/pool/watch.go）。
但 **config.json 仍然只读一次**，所以改配置依旧必须重启；账号相关的改动我们
照旧触发一次重启，好处是新旧上游都能立即生效（新版不必等那 5 秒轮询）。

为什么可以直接自动重启
--------------------
- 实测 `docker restart` 到服务可用约 0.45 秒
- 上游实现了优雅停机：收到 SIGTERM 先 p.Flush() 落盘，再 srv.Shutdown(5s)
  等在途请求结束，因此不会掐断正在进行的对话
- 容器 restart 策略为 unless-stopped

多次连续改动会合并为一次重启，避免并发重启互相干扰。
"""
from __future__ import annotations

import asyncio
import time

from . import wb2api

# 合并窗口：这段时间内的多次请求只触发一次重启
COALESCE_SECONDS = 0.8

_state: dict = {
    'running': False,
    'pending': False,
    'last_at': 0.0,
    'last_ok': None,
    'last_message': '',
    'restart_count': 0,
}
_pending = False
_worker: asyncio.Task | None = None
_lock = asyncio.Lock()


def state() -> dict:
    """当前重载状态，供前端展示。"""
    return dict(_state)


async def _worker_loop() -> None:
    global _pending
    async with _lock:
        _state['running'] = True
        try:
            while _pending:
                _pending = False
                _state['pending'] = False
                # 等一小段，把紧挨着的多次改动合并成一次重启
                await asyncio.sleep(COALESCE_SECONDS)
                if _pending:
                    # 等待期间又有新请求，重新开始计时
                    continue
                ok, message = await wb2api.restart_container()
                _state['restart_count'] += 1
                _state['last_at'] = time.time()
                _state['last_ok'] = ok
                _state['last_message'] = message
        finally:
            _state['running'] = False
            _state['pending'] = False


def request_restart() -> bool:
    """请求一次上游重载（幂等：短时间内多次调用只重启一次）。

    立即返回，不阻塞请求；实际重启在后台完成。
    返回 False 表示没有运行中的事件循环，无法调度后台任务。
    """
    global _pending, _worker
    _pending = True
    _state['pending'] = True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # 没有运行中的事件循环：无法后台调度，交由调用方决定是否同步执行
        return False
    if _worker is None or _worker.done():
        _worker = loop.create_task(_worker_loop())
    return True


async def restart_now() -> tuple[bool, str]:
    """立即重启并等待结果，供「重启上游」这类显式操作使用。"""
    return await wb2api.restart_container()
