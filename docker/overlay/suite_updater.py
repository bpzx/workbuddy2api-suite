#!/usr/bin/env python3
"""updater 侧车：本套件**唯一**持有 docker socket 的组件。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

===== 它为什么存在 =====
「一键更新」要拉新镜像并**重建容器**，这必然要求持有 docker socket 的
拉取/创建能力。本设计的核心取舍是：

  * **公网暴露面（manager）完全不碰 socket**。manager 是经 Cloudflare
    Tunnel 对外的那一个，攻击面最大；它只能向本侧车发一个**无参数**的
    固定触发请求（token 校验），拿不到任意 docker 能力。
  * socket 只交给本文件（约 350 行、只做一件事、不对外发布端口）。

===== 为什么派 helper 容器，而不是自己跑 compose =====
`docker compose up -d` 会重建 `updater` 服务**自身**。若在本容器里直接跑，
命令执行到一半自己就被重建掉了，拿不到结果、状态也写不完。因此这里改为
**派生一个一次性的 helper 容器**（`--rm`，不属 compose 项目），由它执行
`pull && up -d` 重建全部服务 —— helper 不会被自己重建掉。

===== 为什么用 docker CLI 而不是直接调 Engine API =====
本镜像已经带了 docker CLI（构建期从 docker:29-cli 取），compose 插件也是
我们自己装的。用它比手写 unix socket 上的 HTTP/1.1（还要处理 chunked 响应）
简单得多、出错面小得多。这是自有组件，用 CLI 没有额外的信任问题。

===== 宿主机路径从哪来 =====
compose 里写的是 `.:/suite`，`.` 会被 Docker 解析成**宿主机绝对路径**并记在
容器的 `.Mounts[].Source` 里。本侧车启动后 `docker inspect` 自己就能读回来，
因此**用户不需要配 SUITE_DIR** —— 也就不存在"路径配错"这种故障模式。
"""
from __future__ import annotations

import hmac
import json
import os
import shutil
import subprocess
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# ── 配置 ─────────────────────────────────────────────────
DOCKER = shutil.which('docker') or 'docker'
PORT = int(os.environ.get('WB_UPDATER_PORT') or 7865)
TOKEN = os.environ.get('SUITE_UPDATER_TOKEN') or ''

# 与 manager 用**同一个** WB_DATA_DIR（compose 里都是 /data/manager），
# 这样两边对状态文件路径的理解天然一致，不需要额外约定一个变量。
DATA_DIR = Path(os.environ.get('WB_DATA_DIR') or '/data/manager')
STATUS_FILE = DATA_DIR / 'suite-update-status.json'

# 套件目录在容器内的挂载点（compose 里挂的是 `.:/suite`）
SUITE_MOUNT = '/suite'
DATA_MOUNT = '/data'

HELPER_NAME = 'suite-update-helper'
DOCKER_SOCK = '/var/run/docker.sock'
RUNNER = '/opt/suite/update_runner.py'

# 状态里 logs 最多保留多少行（写入量控制；界面上够看即可）
LOG_LIMIT = 400


def log(msg: str) -> None:
    """侧车自身的日志（docker logs 可见）。"""
    print(f'[updater] {msg}', flush=True)


# ── docker 命令封装 ───────────────────────────────────────
def _run(args: list[str], timeout: int = 60) -> tuple[bool, str]:
    """跑一条 docker 命令。返回 (是否成功, stdout 或错误信息)。"""
    try:
        proc = subprocess.run(
            [DOCKER, *args], capture_output=True, text=True, timeout=timeout,
        )
    except FileNotFoundError:
        return False, f'找不到 docker 命令（{DOCKER}）'
    except subprocess.TimeoutExpired:
        return False, f'docker {" ".join(args[:2])} 超时（{timeout}s）'
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:200]
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or '').strip()
        return False, err[:300] or f'docker 退出码 {proc.returncode}'
    return True, (proc.stdout or '').strip()


def _socket_available() -> bool:
    """docker socket 是否挂进来了。抽成函数是为了可测（测试里替换它，
    而不是去改 DOCKER_SOCK 常量 —— 后者会把常量值带进 docker run 参数）。"""
    return Path(DOCKER_SOCK).exists()


def _self_info() -> dict:
    """从自己的 container config 里读出镜像名与两个挂载的宿主机路径。"""
    hostname = os.environ.get('HOSTNAME') or 'updater'
    ok, out = _run(['inspect', hostname, '--format', '{{json .}}'], timeout=15)
    if not ok:
        return {'error': out}
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return {'error': 'docker inspect 返回的不是 JSON'}
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return {'error': 'docker inspect 结果结构异常'}

    mounts = {
        str(m.get('Destination') or ''): str(m.get('Source') or '')
        for m in (data.get('Mounts') or []) if isinstance(m, dict)
    }
    return {
        'image': str((data.get('Config') or {}).get('Image') or ''),
        'suite_dir': mounts.get(SUITE_MOUNT) or '',
        'host_data': mounts.get(DATA_MOUNT) or '',
    }


def _helper_running() -> bool:
    """helper 容器是否还在跑 —— 这是"更新是否在进行"的权威判据。

    比 pid 可靠：helper 是独立容器，更新完成即退出并被 --rm 清掉，
    不存在"进程号被复用"或"容器重建导致误判"的问题。
    """
    ok, out = _run(
        ['ps', '--filter', f'name=^/{HELPER_NAME}$', '--format', '{{.Names}}'],
        timeout=15,
    )
    return ok and bool(out.strip())


# ── 状态文件 ─────────────────────────────────────────────
def _read_status() -> dict | None:
    try:
        data = json.loads(STATUS_FILE.read_text(encoding='utf-8'))
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001
        return None


def _write_status(data: dict) -> None:
    """原子写：先写临时文件再 replace，避免读到写了一半的 JSON。"""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        tmp.replace(STATUS_FILE)
    except Exception as exc:  # noqa: BLE001
        log(f'写状态文件失败：{exc}')


def _initial_status(step: str) -> dict:
    """侧车在 helper 启动前先写一条，让界面立刻有反馈。

    字段与上游 deploy/update.py 的 Reporter 对齐（running/ok/step/logs/
    started_at/finished_at/duration/target_version），这样界面上的日志面板
    与"进程消失但已落地"的判断都能直接复用。
    """
    return {
        'running': True,
        'ok': None,
        'target': 'suite',
        'step': step,
        'logs': [{'ts': int(time.time()), 'level': 'info', 'text': step}],
        'started_at': int(time.time()),
        'finished_at': None,
        'duration': 0.0,
        'target_version': '',
    }


def _fail_status(step: str, detail: str) -> dict:
    st = _initial_status(step)
    st['running'] = False
    st['ok'] = False
    st['finished_at'] = int(time.time())
    st['logs'].append({'ts': int(time.time()), 'level': 'error', 'text': detail})
    return st


# ── 触发更新 ─────────────────────────────────────────────
def _spawn_helper() -> tuple[bool, str]:
    """派生一次性 helper 容器执行 compose pull && up -d。"""
    if not _socket_available():
        return False, (
            f'容器内没有 {DOCKER_SOCK} —— updater 服务缺少 socket 挂载，'
            '无法执行更新。请检查 docker-compose.yml 里 updater 服务的 volumes。'
        )

    info = _self_info()
    if info.get('error'):
        return False, f'无法读取自身容器信息：{info["error"]}'
    image = info.get('image') or os.environ.get('SUITE_IMAGE') or ''
    suite_dir = info.get('suite_dir') or ''
    host_data = info.get('host_data') or ''

    if not image:
        return False, '无法确定自身镜像名，无法拉起 helper 容器'
    if not suite_dir:
        return False, (
            f'读不到 {SUITE_MOUNT} 的宿主机路径 —— updater 服务缺少 '
            '`.:/suite` 挂载（compose 文件里少了它，就无法在宿主机上执行 compose）。'
        )
    if not host_data:
        return False, (
            f'读不到 {DATA_MOUNT} 的宿主机路径 —— updater 服务缺少数据目录挂载，'
            '更新进度将无法被管理端读到。'
        )

    log(f'镜像={image} 套件目录={suite_dir} 数据目录={host_data}')

    args = [
        'run', '-d', '--rm',
        '--name', HELPER_NAME,
        # 刻意**不加 --user**：helper 以 root 跑。原因是它要访问宿主 docker
        # socket，而 socket 通常是 root:docker 0600/0660，uid 10001 不在宿主的
        # docker 组里，连不上。代价是它写出的文件属主是 root —— 这一点由
        # update_runner.py 在写完状态文件后 chown 回 10001 解决（否则侧车
        # 第二次写初始状态会 permission denied）。
        # helper 需要 socket 才能在宿主机侧重建容器
        '-v', f'{DOCKER_SOCK}:{DOCKER_SOCK}',
        # 套件目录**按同一个绝对路径**挂进去：compose 里 `./data` 这类相对路径
        # 会以 --project-directory 为基准解析，路径一致才能解析出正确的宿主机路径。
        # 只读即可 —— compose 只读它，不需要写。
        '-v', f'{suite_dir}:{suite_dir}:ro',
        '-v', f'{host_data}:{DATA_MOUNT}',
        '-e', f'SUITE_DIR={suite_dir}',
        # 与侧车自己看状态文件用的是同一个绝对路径（两边都挂了数据目录）
        '-e', f'WB_UPDATE_STATUS={STATUS_FILE}',
        '-e', f'TZ={os.environ.get("TZ") or "Asia/Shanghai"}',
        # 与镜像其余服务一致，用 tini 当 PID 1 再跑脚本：
        # helper 会反复 fork `docker compose` 子进程，需要有人回收它们，
        # 也需要正确的信号转发（直接用 --entrypoint 指向脚本会让它当 PID 1，
        # 在 Linux 上默认忽略 SIGTERM 且不回收子进程）。
        '--entrypoint', '/usr/bin/tini',
        image,
        '--', RUNNER,
    ]
    ok, out = _run(args, timeout=90)
    if not ok:
        return False, f'拉起 helper 容器失败：{out}'
    log(f'helper 已启动：{out[:12]}')
    return True, '更新已开始'


def start_update() -> tuple[int, dict]:
    """处理一次更新请求，返回 (HTTP 状态码, 响应体)。"""
    if not TOKEN:
        return 500, {'ok': False, 'message': '侧车未配置 SUITE_UPDATER_TOKEN，拒绝执行'}
    # 幂等保护：helper 还在跑就直接拒绝，避免并发重建
    if _helper_running():
        return 409, {'ok': False, 'message': '已有更新任务正在执行'}

    # 先落一条状态：helper 启动有延迟，界面需要立刻看到反馈
    _write_status(_initial_status('正在准备更新（拉起 helper 容器）'))

    ok, message = _spawn_helper()
    if not ok:
        _write_status(_fail_status('更新未能开始', message))
        return 500, {'ok': False, 'message': message}

    return 202, {'ok': True, 'message': message}


def read_status() -> tuple[int, dict]:
    """当前状态。`running` 以 helper 容器是否存在为准。"""
    running = _helper_running()
    data = _read_status()

    if data and not running and data.get('running'):
        # helper 没了但状态还说在跑 —— 两种情况：
        #   1) 更新成功，最后一步重建容器把 helper 自己所在的项目重建掉了；
        #   2) helper 异常退出。
        # 用 ok 是否已写入来区分，并补上结束时间（否则界面"上次更新"永远显示从未）。
        data = dict(data)
        data['running'] = False
        if data.get('ok') is None:
            data['ok'] = False
            data['step'] = data.get('step') or '更新进程异常中断'
        if not data.get('finished_at'):
            try:
                data['finished_at'] = int(STATUS_FILE.stat().st_mtime)
            except Exception:  # noqa: BLE001
                data['finished_at'] = int(time.time())
        if data.get('started_at'):
            data['duration'] = max(
                0.0, float(data['finished_at']) - float(data['started_at']),
            )

    return 200, {'running': running, 'status': data}


# ── HTTP ─────────────────────────────────────────────────
class Handler(BaseHTTPRequestHandler):
    server_version = 'suite-updater'
    protocol_version = 'HTTP/1.1'

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authed(self) -> bool:
        """定长比较，避免时序侧信道。"""
        header = self.headers.get('Authorization') or ''
        given = header[7:] if header.lower().startswith('bearer ') else ''
        return bool(TOKEN) and hmac.compare_digest(given, TOKEN)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split('?', 1)[0]
        # 健康检查不校验 token：它不泄露任何信息，且要被 compose healthcheck 用
        if path == '/healthz':
            self._send(200, {'ok': True})
            return
        if path == '/status':
            if not self._authed():
                self._send(401, {'ok': False, 'message': 'token 无效'})
                return
            code, payload = read_status()
            self._send(code, payload)
            return
        self._send(404, {'ok': False, 'message': '未知路径'})

    def do_POST(self) -> None:  # noqa: N802
        path = self.path.split('?', 1)[0]
        if path != '/update':
            self._send(404, {'ok': False, 'message': '未知路径'})
            return
        if not self._authed():
            self._send(401, {'ok': False, 'message': 'token 无效'})
            return
        code, payload = start_update()
        self._send(code, payload)

    def log_message(self, fmt: str, *args) -> None:  # noqa: A003
        # 默认实现往 stderr 打一行一条，噪音大；统一走 log()
        log(f'{self.address_string()} {fmt % args}')


def main() -> int:
    if not TOKEN:
        # 不直接退出：让 /healthz 可用，界面能显示"侧车在、但没配 token"，
        # 比容器起不来（界面显示"不可用"却查不出原因）更好排查。
        log('⚠️ 未设置 SUITE_UPDATER_TOKEN —— 所有更新请求都会被拒绝。'
            '请在 .env 里设置一个随机值后重启本服务。')
    log(f'监听 :{PORT}，状态文件 {STATUS_FILE}')
    try:
        ThreadingHTTPServer(('0.0.0.0', PORT), Handler).serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
