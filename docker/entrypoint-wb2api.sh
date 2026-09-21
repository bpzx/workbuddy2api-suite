#!/bin/sh
# workbuddy2api 容器入口（本文件是本项目新写的，不是上游代码）
#
# 职责：
#   0. 以 root 修正数据目录属主，再用 gosu 降权到 app 运行（见下）
#   1. 首次启动生成 /data/config.json（从模板，api_key 随机化）
#   2. 已存在时：**增量补齐**上游新增的配置项（只补缺失键，绝不改已有值），
#      然后做只读的路径校验，发现路径不对时明确告警
#   3. 启动上游二进制
#
# 为什么不整个重新生成、也不覆盖已有值：manager 的设置页会写这个文件，
# 启动时悄悄改用户写下的配置比"缺一个键"危险得多。所以只补缺失的键 ——
# 用户能从日志里看到补了哪几个，而不是配置被静默改造。
#
# 为什么需要"增量补齐"这一步：config.json 只在首次启动生成，之后不再重新生成。
# 上游一旦新增配置项，老部署里就没有那些键，设置页会因此显示字面量 `undefined`
# （成因见 docker/overlay/config_merge.py 的注释，那是真实发生过的事故）。
#
# ── 关于「先 root 再降权」──────────────────────────────────
# compose 默认把数据放在宿主目录（bind mount），该目录由 Docker/宿主创建时
# 属主是 root，而服务以 uid 10001（app）运行，于是连建 auths/ 都会
# permission denied，服务启动即失败。named volume 没有这个问题（Docker 会
# 从镜像继承属主），所以这是改用宿主目录后必须补上的一步。
# 修正属主需要 root，因此入口以 root 启动、chown 完立刻用 gosu 降权，
# 服务进程本身仍以非 root 运行（不会因为这一步放大攻击面）。
set -eu

APP_UID=10001
APP_GID=10001

DATA_DIR="${WB_SUITE_DATA_DIR:-/data}"
AUTH_DIR="${DATA_DIR}/auths"
CONFIG="${DATA_DIR}/config.json"
STATE_FILE="${DATA_DIR}/state.json"
TEMPLATE="/opt/suite/wb2api.config.template.json"

# ── 步骤 0：修正数据目录属主（仅当以 root 启动时）──────────
if [ "$(id -u)" = "0" ]; then
    mkdir -p "$DATA_DIR" "$AUTH_DIR"
    # 只 chown 数据目录，不碰 /opt（那部分是构建期已归 app 的只读代码）
    if ! chown -R "$APP_UID:$APP_GID" "$DATA_DIR" 2>/dev/null; then
        echo "[suite][WARN] 无法修正 $DATA_DIR 的属主（宿主目录可能是只读挂载或 NFS）。" >&2
        echo "[suite][WARN] 若随后出现 permission denied，请在宿主机执行：" >&2
        echo "[suite][WARN]   sudo chown -R $APP_UID:$APP_GID <宿主机数据目录>" >&2
    fi

    # 降权执行自身（第二次进来 uid != 0，走下面的正常流程）
    exec gosu "$APP_UID:$APP_GID" "$0" "$@"
fi

mkdir -p "$AUTH_DIR"

if [ ! -f "$CONFIG" ]; then
    echo "[suite] 首次启动：生成 $CONFIG（api_key 随机化）"
    API_KEY="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"
    python3 - "$TEMPLATE" "$CONFIG" "$API_KEY" "$AUTH_DIR" "$STATE_FILE" <<'PY'
import json
import sys

template, out, api_key, auth_dir, state_file = sys.argv[1:6]
with open(template, encoding='utf-8') as fh:
    cfg = json.load(fh)
cfg['api_key'] = api_key
cfg['auth_dir'] = auth_dir
cfg['state_file'] = state_file
with open(out, 'w', encoding='utf-8') as fh:
    json.dump(cfg, fh, ensure_ascii=False, indent=2)
    fh.write('\n')
PY
    chmod 600 "$CONFIG"
    echo "[suite] api_key 已写入 $CONFIG（查看：docker compose exec wb2api cat $CONFIG）"
else
    echo "[suite] 复用已存在的 $CONFIG"

    # 增量补齐：把模板里有、config.json 里没有的键补上（只补不改，写前留 .bak）。
    # 失败不影响启动 —— 上游对缺字段会套默认值，只是设置页可能显示 undefined。
    python3 /opt/suite/config_merge.py "$TEMPLATE" "$CONFIG" || true

    # 只读校验：auth_dir / state_file 必须落在数据卷内。
    # 典型误用：把裸机部署的 ./auths 或 /opt/workbuddy2api/auths 原样搬进来，
    # 上游会照常启动、但一个账号都读不到，表现为"账号列表为空"且无任何报错。
    python3 - "$CONFIG" "$DATA_DIR" <<'PY' || true
import json
import os
import sys

config, data_dir = sys.argv[1], os.path.abspath(sys.argv[2])
try:
    with open(config, encoding='utf-8') as fh:
        cfg = json.load(fh)
except Exception as exc:  # noqa: BLE001
    print(f'[suite][WARN] 无法解析 {config}：{exc}', file=sys.stderr)
    sys.exit(0)

def resolve(value: str) -> str:
    # 相对路径按上游的工作目录（/opt/wb2api）解析，与 wb2api 自身一致
    if os.path.isabs(value):
        return os.path.abspath(value)
    return os.path.abspath(os.path.join('/opt/wb2api', value))

for key in ('auth_dir', 'state_file'):
    raw = str(cfg.get(key) or '')
    if not raw:
        continue
    resolved = resolve(raw)
    if not (resolved == data_dir or resolved.startswith(data_dir + os.sep)):
        print(
            f'[suite][WARN] config.{key}="{raw}" 解析为 {resolved}，不在数据卷 {data_dir} 内。\n'
            f'[suite][WARN] 账号或状态将无法持久化；请改为 "{data_dir}/'
            + ('auths' if key == 'auth_dir' else 'state.json')
            + f'"（可在 manager 设置页或直接编辑 {config}）',
            file=sys.stderr,
        )
PY
fi

cd /opt/wb2api
echo "[suite] 启动 workbuddy2api（config=$CONFIG）"
exec /opt/wb2api/wb2api -config "$CONFIG"
