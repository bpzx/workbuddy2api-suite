#!/usr/bin/env python3
"""容器化部署下的「一键更新」替身。

===== 这是本项目新写的文件，不是上游代码 =====

背景
----
manager 上游的 `deploy/update.py` 是**裸机部署**的更新流程，它依赖 `git`
检出、`systemctl restart`、以及从 GitHub Release 下载 tar.gz 覆盖自身代码。

上游 v1.0.33+ 也提供了容器版流程（`WB_RUN_MODE=docker`：替换代码 → 容器退出
→ 由 compose 的 `restart` 策略拉起）。但那套流程是为**本地 build 的镜像**
设计的；本发行版用的是 **ghcr 预构建镜像**，容器内替换的代码会在下次
`docker compose pull` 时被镜像层覆盖 —— 结果是"版本号变了、代码还是旧的"，
比不更新更难排查。

因此这里用替身脚本占位：它**什么都不改动**，只如实告诉用户正确的更新方式
（在宿主机 `pull && up -d`）。

为什么用「覆盖」而不是「改上游代码」
------------------------------------
本项目原则是 vendor/ 下的上游代码一字不改（见 UPSTREAMS.md），
这样同步上游才是一条命令的事。所以更新方式在**构建期**用本文件覆盖
`/opt/manager/deploy/update.py`（见 docker/Dockerfile），
vendor 源码保持原样。

> 注：上游的 `start_update` 已用 `can_control_docker()` 做前置检查，
> 「更新上游 / 全部」在本发行版会被**上游自己拦住**并提示宿主机操作
> （因为我们经 socket 代理访问 docker 且关闭了 `/info` 端点）。
> 本替身只需处理绕不过去的「仅更新管理端」这一路。

输出契约
--------
manager 的 `server/services/updater.py` 会以子进程方式调用本脚本
（`update.py --target manager|upstream|both`），并读取状态文件
（`WB_UPDATE_STATUS`，默认 `$WB_DATA_DIR/update-status.json`）展示进度。
因此本脚本必须写出**同样结构**的状态 JSON —— 字段与上游 Reporter 对齐，
否则界面会读不到、显示成「更新进程异常中断」。

本脚本**不写以下字段**，理由见各自注释：
  * `version`    —— 写了会覆盖 read_status() 算出的真实版本
  * `updater_found` —— 由 read_status() 按文件是否存在判定，本就为 True
"""
from __future__ import annotations

import argparse
import json
import os
import time
from pathlib import Path

# 与上游 updater.py 的环境变量约定保持一致（见 vendor/manager/server/services/updater.py）
DATA_DIR = Path(os.environ.get('WB_DATA_DIR') or '/data')
STATUS_FILE = Path(os.environ.get('WB_UPDATE_STATUS') or DATA_DIR / 'update-status.json')

TARGET_LABEL = {
    'manager': '管理端',
    'upstream': '上游网关',
    'both': '全部',
}


def _write_status(state: dict) -> None:
    """原子写入状态文件（tmp + replace），与上游 Reporter.flush 同法。"""
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix('.tmp')
        tmp.write_text(json.dumps(state, ensure_ascii=False), encoding='utf-8')
        tmp.replace(STATUS_FILE)
    except OSError:
        pass


def main() -> int:
    parser = argparse.ArgumentParser(description='workbuddy2api-suite 容器化更新替身')
    parser.add_argument('--target', default='both', choices=['manager', 'upstream', 'both'])
    args = parser.parse_args()

    started = time.time()
    target_label = TARGET_LABEL.get(args.target, args.target)

    logs = [
        {'level': 'info', 'text': f'收到更新请求：{target_label}（target={args.target}）'},
        {
            'level': 'warn',
            'text': '本发行版用 **ghcr 上的预构建镜像**，容器内不做代码更新。'
                    '即使替换了容器内的代码，下次 docker compose pull 也会被镜像层覆盖，'
                    '结果是"版本号变了、代码还是旧的"——比不更新更难排查。',
        },
        {'level': 'info', 'text': '请在**宿主机**本项目目录下执行：'},
        {'level': 'info', 'text': '    docker compose pull'},
        {'level': 'info', 'text': '    docker compose up -d'},
        {
            'level': 'info',
            'text': '更新镜像前请先查看新版捆绑的上游 commit：'
                    'docker inspect <镜像> | grep upstream，或阅读 UPSTREAMS.md。',
        },
        {
            'level': 'info',
            'text': '若还需要同步上游新版本，先在本项目目录跑 '
                    './scripts/sync-upstreams.sh，再重新构建并发布镜像。',
        },
        {
            'level': 'info',
            'text': '账号凭证、上游配置与密钥数据都保存在 /data 卷中，'
                    '上述操作不会影响它们。',
        },
    ]

    state = {
        'running': False,
        # ok=False 是诚实的结论：本次「更新」确实没有发生。
        # 不谎报成功，否则用户会以为已在跑新版本。
        'ok': False,
        'target': args.target,
        'step': '容器化部署：请使用 docker compose 更新',
        'logs': [
            {'ts': int(started), 'level': item['level'], 'text': item['text']}
            for item in logs
        ],
        'started_at': int(started),
        'finished_at': int(time.time()),
        'duration': round(time.time() - started, 1),
        'pid': os.getpid(),
        # 有意不写 'version'：updater.read_status() 先算好当前版本（取自
        # server/main.py 的 app.version，即 manager 的真实版本号），再用
        # status.update(文件内容) 覆盖。本脚本在容器里读不到那个版本号，
        # 若写一个 'unknown' 进去，反而会把界面上正确的 vX.Y.Z 覆盖成 unknown。
        #
        # 同理不写 'updater_found'：read_status() 按 deploy/update.py 是否
        # 存在来判定，本文件就在那个位置，本就会是 True（界面不报警告）。
        # 签名状态：本流程不涉及发布包，如实标 none 而不是 verified
        'signature': {'status': 'none', 'detail': '容器化部署不经过 Release 发布包'},
    }

    for item in logs:
        print(f'[{time.strftime("%H:%M:%S")}] {item["text"]}', flush=True)
    _write_status(state)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
