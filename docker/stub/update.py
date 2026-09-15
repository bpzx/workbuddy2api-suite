#!/usr/bin/env python3
"""容器化部署下的「一键更新」替身。

===== 这是本项目新写的文件，不是上游代码 =====

背景
----
manager 上游的 `deploy/update.py` 是**裸机部署**的更新流程，它依赖三样在
容器里不存在的东西：

  1. `git` 检出上游仓库（容器内没有上游的 .git，代码是构建期快照进来的）
  2. `systemctl restart` 重启服务（容器内没有 systemd，PID 1 是 entrypoint）
  3. 从 GitHub Release 下载 tar.gz 并验签后覆盖自身代码（容器是只读镜像层）

在容器里硬跑那套流程，最坏情况是**把运行中的容器代码改坏**（下载、解压、
替换 server/ 全部会成功执行，只有最后的 systemctl 失败）。因此这里用一个
替身脚本占位：它**什么都不改动**，只如实告诉用户正确的更新方式。

为什么用「覆盖」而不是「改上游代码」
------------------------------------
本项目的原则是 vendor/ 下的上游代码一字不改（见 UPSTREAMS.md），
这样同步上游才是一条命令的事。所以更新方式在**构建期**用本文件覆盖
`/opt/manager/deploy/update.py`（见 docker/Dockerfile），
vendor 源码保持原样。

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
            'text': '本部署由 Docker Compose 管理，容器内不支持一键更新'
                    '（无 git、无 systemd，镜像层只读）。',
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
