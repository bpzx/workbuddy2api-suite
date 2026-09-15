#!/bin/sh
# workbuddy-manager 容器入口（本文件是本项目新写的，不是上游代码）
#
# 职责：
#   0. 以 root 修正数据目录属主，再用 gosu 降权到 app 运行（见下）
#   1. 准备数据目录
#   2. 启动 uvicorn
#
# 与裸机部署的差别（这些正是容器化要替换掉的部分）：
#   * 不启动 systemd，PID 1 是 tini；服务重启靠 compose 的 restart 策略
#   * 不编译前端：web/out 是构建期由多阶段构建产出的静态导出
#   * 不执行 install.sh（那套是宿主机安装流程，容器里不需要也不适用）
#
# ── 关于「先 root 再降权」──────────────────────────────────
# compose 默认把数据放在宿主目录（bind mount），该目录由 Docker/宿主创建时
# 属主是 root，而服务以 uid 10001（app）运行，于是写 manager.db / users.json
# 都会 permission denied。named volume 没有这个问题（Docker 从镜像继承属主），
# 所以这是改用宿主目录后必须补上的一步。
# 修正属主需要 root，故入口以 root 启动、chown 完立刻 gosu 降权，
# 服务进程本身仍以非 root 运行。
set -eu

APP_UID=10001
APP_GID=10001

DATA_DIR="${WB_SUITE_DATA_DIR:-/data}"
MANAGER_DATA="${DATA_DIR}/manager"
PORT="${WB_MANAGER_PORT:-7864}"

# ── 步骤 0：修正数据目录属主（仅当以 root 启动时）──────────
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

echo "[suite] 启动 workbuddy-manager（:$PORT，数据=$MANAGER_DATA）"
echo "[suite] 上游网关：${WB2API_BASE:-未设置}  容器名：${WB2API_CONTAINER:-未设置}"
if [ -n "${WB_HTTP_PROXY:-}" ]; then
    echo "[suite] 出口代理：$WB_HTTP_PROXY（内网请求自动直连，见 docker/patches/apply.py）"
    # Go 侧（wb2api）只认 HTTP_PROXY/HTTPS_PROXY，不认 ALL_PROXY，这里提示一下
    echo "[suite] 提示：wb2api 出站走 HTTPS_PROXY/HTTP_PROXY；若代理只配在 ALL_PROXY 里，网关不会走代理"
fi

cd /opt/manager
exec python3 -m uvicorn server.main:app --host 0.0.0.0 --port "$PORT" --no-access-log
