#!/bin/sh
# updater 侧车容器入口（本文件是本项目新写的，不是上游代码）
#
# 职责：以 root 修正数据目录属主 → gosu 降权到 app → 启动侧车 HTTP 服务。
#
# 为什么这个容器持有 docker socket（与 manager 的对比）
# ----------------------------------------------------
# 「一键更新」要拉镜像并重建容器，必须持有 docker socket 的拉取/创建能力。
# 本设计的取舍是**把 socket 只交给这个最小组件**，而让公网暴露的 manager
# 完全不碰 socket（它只能向本侧车发一个无参数的固定触发请求，带 token 校验）。
# manager 是经 Cloudflare Tunnel 对外的那个，攻击面最大，不该拿这种能力。
#
# 本容器**不发布端口**（compose 里只有 expose），只挂在 compose 内部网络。
set -eu

APP_UID=10001
APP_GID=10001

DATA_DIR="${WB_SUITE_DATA_DIR:-/data}"
MANAGER_DATA="${WB_DATA_DIR:-${DATA_DIR}/manager}"
PORT="${WB_UPDATER_PORT:-7865}"

# ── 步骤 0：修正数据目录属主（仅当以 root 启动时）──────────
# 与 manager/wb2api 入口同一套处理：数据目录默认是宿主 bind mount，由 Docker
# 或宿主创建时属主为 root，而服务以 uid 10001 运行，不修正就写不了状态文件。
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$MANAGER_DATA"
    if ! chown -R "$APP_UID:$APP_GID" "$DATA_DIR" 2>/dev/null; then
        echo "[suite][WARN] 无法修正 $DATA_DIR 的属主（宿主目录可能是只读挂载或 NFS）。" >&2
        echo "[suite][WARN] 若随后出现 permission denied，请在宿主机执行：" >&2
        echo "[suite][WARN]   sudo chown -R $APP_UID:$APP_GID <宿主机数据目录>" >&2
    fi

    exec gosu "$APP_UID:$APP_GID" "$0" "$@"
fi

mkdir -p "$MANAGER_DATA"

echo "[suite] 启动 updater 侧车（:$PORT，状态=$MANAGER_DATA/suite-update-status.json）"
if [ -z "${SUITE_UPDATER_TOKEN:-}" ]; then
    echo "[suite][WARN] 未设置 SUITE_UPDATER_TOKEN —— 所有更新请求都会被拒绝。" >&2
    echo "[suite][WARN] 请在 .env 里设置一个随机值（openssl rand -hex 24）后重启本服务。" >&2
fi
# 这里刻意提示一句：socket 直连是有意为之，不是漏配
echo "[suite] 持有 $([ -S /var/run/docker.sock ] && echo 'docker socket（用于 pull + 重建容器）' || echo '**无** docker socket —— 一键更新不可用')"

exec python3 /opt/suite/suite_updater.py
