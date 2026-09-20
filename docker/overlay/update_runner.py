#!/usr/bin/env python3
"""一键更新的实际执行者（一次性 helper 容器，跑完即被 --rm 清理）。

【SUITE-OVERLAY】本文件是本发行版新写的，不是上游代码。

由 updater 侧车用 `docker run --rm` 派生出来，**不属于 compose 项目** ——
这一点是刻意的：`docker compose up -d` 会重建包括 updater 在内的所有服务，
如果由 updater 自己执行，它会在命令跑到一半时被重建掉，既拿不到结果也写不
完状态。放在项目外的 helper 里执行就没有这个自杀竞争。

执行内容：
    docker compose -f <SUITE_DIR>/docker-compose.yml \\
        --project-directory <SUITE_DIR> pull
    ... up -d

`SUITE_DIR` 是**宿主机的绝对路径**，且以同一路径挂进本容器 —— 这样 compose
把 `./data` 这类相对路径解析成宿主机路径时才正确（若挂到别的路径，compose
算出来的会是容器内的路径，创建的兄弟容器就会绑到宿主机上一个错误的空目录）。

进度写进 `WB_UPDATE_STATUS` 指向的状态文件，字段与上游 deploy/update.py 的
Reporter 对齐，管理端的日志面板可以直接显示。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

SUITE_DIR = Path(os.environ.get('SUITE_DIR') or '')
STATUS_FILE = Path(os.environ.get('WB_UPDATE_STATUS') or '/data/manager/suite-update-status.json')
COMPOSE_FILE = SUITE_DIR / 'docker-compose.yml'

# 状态文件写完要把属主交还给运行管理端的那个 uid。
#
# 为什么必须做：helper 以 **root** 跑（它要访问宿主的 docker socket，而非 root
# 用户通常不在宿主 docker 组里，连不上 socket），而它用 tmp + replace 写状态
# 文件会让目标文件变成 **root 所有**。之后侧车（uid 10001）再写"初始状态"就会
# permission denied —— 表现为"第一次更新能用、第二次点不动"，而且日志里只有
# 一句权限错误，很难联想到是 helper 留下的文件属主问题。
#
# 与 docker/Dockerfile 里的 APP_UID 保持一致。
APP_UID = int(os.environ.get('SUITE_APP_UID') or 10001)
APP_GID = int(os.environ.get('SUITE_APP_GID') or 10001)

# 写入节流：更新过程可能输出上百行，每行都落一次盘没有必要
WRITE_INTERVAL = 0.5
LOG_LIMIT = 400


def _hand_off_to_app(path: Path) -> None:
    """把刚写的文件交给 app 用户，避免 root 属主卡住侧车后续写入。

    只在 Linux 上有意义（helper 容器就是 Linux）。Windows **没有 `os.chown`**
    （是属性缺失，不是 OSError）—— 本地开发跑这个脚本时直接跳过：那种情况下
    也不存在"root 写的文件"这回事。这个函数**绝不能抛异常**，否则调用方
    `flush()` 的 replace 就不会执行，状态文件根本写不出来。
    """
    try:
        os.chmod(path, 0o664)
    except OSError:
        pass
    chown = getattr(os, 'chown', None)
    if chown is None:
        return
    try:
        chown(path, APP_UID, APP_GID)
    except OSError:
        # 非 root 时 chown 必然失败，不是错误
        pass


def log_line(text: str) -> None:
    """同时写进度文件和 stdout（stdout 进 docker logs，便于排查）。"""
    print(f'[runner] {text}', flush=True)


class Reporter:
    def __init__(self) -> None:
        self.started = time.time()
        self.logs: list[dict] = []
        self.step = '正在准备'
        self.target_version = ''
        self._last_write = 0.0

    def add(self, text: str, level: str = 'info') -> None:
        text = (text or '').rstrip()
        if not text:
            return
        self.logs.append({'ts': int(time.time()), 'level': level, 'text': text})
        if len(self.logs) > LOG_LIMIT:
            self.logs = self.logs[-LOG_LIMIT:]
        log_line(text)

    def set_step(self, step: str) -> None:
        self.step = step
        self.flush(force=True)

    def payload(self, running: bool, ok: bool | None) -> dict:
        now = time.time()
        return {
            'running': running,
            'ok': ok,
            'target': 'suite',
            'step': self.step,
            'logs': self.logs,
            'started_at': int(self.started),
            'finished_at': None if running else int(now),
            'duration': 0.0 if running else round(now - self.started, 1),
            'target_version': self.target_version,
        }

    def flush(self, *, running: bool = True, ok: bool | None = None, force: bool = False) -> None:
        now = time.time()
        if not force and now - self._last_write < WRITE_INTERVAL:
            return
        self._last_write = now
        try:
            STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATUS_FILE.with_suffix('.json.tmp')
            tmp.write_text(
                json.dumps(self.payload(running, ok), ensure_ascii=False),
                encoding='utf-8',
            )
            # 先交还属主再 replace —— 否则状态文件会变成 root 所有
            _hand_off_to_app(tmp)
            tmp.replace(STATUS_FILE)
        except Exception as exc:  # noqa: BLE001
            log_line(f'写入状态文件失败：{exc}')


def run_streamed(rep: Reporter, args: list[str]) -> int:
    """跑一条命令并把输出逐行喂给 Reporter。返回退出码。"""
    rep.add(f'$ {" ".join(args)}')
    try:
        proc = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, bufsize=1, encoding='utf-8', errors='replace',
        )
    except FileNotFoundError as exc:
        rep.add(f'无法执行 {args[0]}：{exc}', 'error')
        return 127
    assert proc.stdout is not None
    for line in proc.stdout:
        rep.add(line)
        rep.flush()
    proc.wait()
    return proc.returncode


def detect_target_version(rep: Reporter) -> None:
    """拉取完成后，从新镜像的 OCI 标签读出目标版本号。"""
    try:
        out = subprocess.run(
            ['docker', 'inspect', '--format',
             '{{index .Config.Labels "org.opencontainers.image.version"}}',
             _compose_image()],
            capture_output=True, text=True, timeout=30,
        )
        if out.returncode == 0:
            rep.target_version = (out.stdout or '').strip()
    except Exception:  # noqa: BLE001
        pass


def _compose_image() -> str:
    """compose 里实际使用的镜像名（优先 .env 的 SUITE_IMAGE）。"""
    env = read_dotenv()
    return env.get('SUITE_IMAGE') or os.environ.get('SUITE_IMAGE') or ''


def read_dotenv() -> dict:
    """极简 .env 读取：只为拿 SUITE_IMAGE 用来查标签，不做插值。"""
    out: dict = {}
    try:
        for raw in (SUITE_DIR / '.env').read_text(encoding='utf-8').splitlines():
            line = raw.strip()
            if not line or line.startswith('#') or '=' not in line:
                continue
            k, _, v = line.partition('=')
            out[k.strip()] = v.strip().strip('"').strip("'")
    except Exception:  # noqa: BLE001
        pass
    return out


def main() -> int:
    rep = Reporter()

    if not SUITE_DIR or not COMPOSE_FILE.is_file():
        rep.add(
            f'找不到 compose 文件（SUITE_DIR={SUITE_DIR or "未设置"}）。'
            '请确认 updater 服务的 `.:/suite` 挂载指向套件目录。',
            'error',
        )
        rep.flush(running=False, ok=False, force=True)
        return 1

    compose = [
        'docker', 'compose',
        '-f', str(COMPOSE_FILE),
        '--project-directory', str(SUITE_DIR),
    ]

    # 先自检 compose 插件：镜像里若没装成功，这里给出明确原因而不是让用户
    # 面对一句 "exec: docker-compose: not found"
    rep.set_step('检查 docker compose 可用性')
    try:
        probe = subprocess.run(
            ['docker', 'compose', 'version'],
            capture_output=True, text=True, timeout=30,
        )
        if probe.returncode != 0:
            raise RuntimeError((probe.stderr or probe.stdout or '').strip()[:200])
        rep.add((probe.stdout or '').strip())
    except Exception as exc:  # noqa: BLE001
        rep.add(f'docker compose 不可用：{exc}', 'error')
        rep.flush(running=False, ok=False, force=True)
        return 1

    # ── 1) 拉取新镜像 ──
    rep.set_step('正在拉取新镜像')
    if run_streamed(rep, [*compose, 'pull']) != 0:
        rep.set_step('拉取镜像失败')
        rep.flush(running=False, ok=False, force=True)
        return 1
    detect_target_version(rep)

    # ── 2) 重建容器 ──
    # 这一步会重建全部服务，**包括 updater 自己**；helper 在 compose 项目之外，
    # 所以不会被重建掉，可以安心把这一步跑完。
    rep.set_step('正在重建容器')
    if run_streamed(rep, [*compose, 'up', '-d']) != 0:
        rep.set_step('重建容器失败')
        rep.flush(running=False, ok=False, force=True)
        return 1

    rep.set_step('更新完成')
    rep.add('镜像已更新，容器已重建并运行新版本。')
    if rep.target_version:
        rep.add(f'当前套件版本：{rep.target_version}')
    rep.flush(running=False, ok=True, force=True)
    return 0


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        # 兜底：任何未预期异常都要留下痕迹，否则界面只会显示"更新进程异常中断"
        try:
            rep = Reporter()
            rep.add(f'更新脚本异常退出：{exc}', 'error')
            rep.flush(running=False, ok=False, force=True)
        except Exception:  # noqa: BLE001
            pass
        raise SystemExit(1) from exc
