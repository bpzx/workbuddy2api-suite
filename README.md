# workbuddy2api-suite

把 [workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) 与
[workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)
整合为**一个镜像、一条 compose 命令**的发行版。

- **workbuddy2api**（Go）— 把 CodeBuddy 账号池包装成 OpenAI 兼容接口的反代网关
- **workbuddy-manager**（FastAPI + Next.js）— 配套的 Web 管理控制台 + 对外多密钥网关

本仓库负责的是**容器化与集成**：镜像构建、compose 编排、容器入口、上游同步。
两个上游的代码以未修改的快照内置于 `vendor/`，其来源 commit 完整记录于
[`upstreams.json`](upstreams.json) 与 [`UPSTREAMS.md`](UPSTREAMS.md)。

> ⚠️ **合规须知**：这是**非官方**项目，以上游 CodeBuddy 账号作为服务上游，
> 仅限本人授权账号、本机 / 私有环境测试。使用涉及目标平台服务条款与账号风险，
> 请阅读两个上游的免责声明。

---

## 快速开始

### 前置条件

- Docker 与 Docker Compose v2
- 一个或多个已注册的 CodeBuddy 账号（用于 OAuth 登录）

### 部署

```bash
git clone https://github.com/bpzx/workbuddy2api-suite.git
cd workbuddy2api-suite

cp .env.example .env
# 可选：设置 WB_ADMIN_PASSWORD；留空则随机生成并打印到日志

docker compose pull
docker compose up -d
```

首次启动会自动生成 `/data/config.json` 并把 `api_key` 随机化。

**取管理员密码**（未在 `.env` 中设置时）：

```bash
docker compose logs manager | grep -iE 'password|密码|初始'
```

打开 `http://<主机>:7864`，用管理员账号登录。

### 添加账号

在面板「账号」页点「添加账号」扫码即可，无需命令行。

也可以用容器内的 `login.sh`（与面板写入同一份 `auths/`）：

```bash
docker compose exec wb2api ./login.sh
```

### 验证

```bash
# 管理端存活
curl -s http://localhost:7864/api/healthz

# 上游网关状态（内部端口，未发布到宿主机）
docker compose exec manager curl -s -H "Authorization: Bearer <api_key>" \
  http://wb2api:7863/healthz
```

---

## 架构

```
                    ┌───────────────────────────────┐
   客户端 / SDK ───▶│  manager  :7864（唯一对外入口）│
   (OpenAI 兼容)    │  多密钥鉴权 · 配额 · IP 管控   │
                    │  用量统计 · Web 管理界面       │
                    └───────────┬───────────────────┘
                                │ 内部网络
                                ▼
                    ┌───────────────────────────────┐
                    │  wb2api  :7863（不发布端口）   │
                    │  账号池 · 选号 · 熔断冷却      │
                    │  SSE 重建 · 定时任务           │
                    └───────────┬───────────────────┘
                                │
                                ▼
                        CodeBuddy 上游

                    ┌───────────────────────────────┐
                    │  dockerproxy（socket 代理）    │
                    │  仅放行 restart / logs         │
                    └───────────────────────────────┘
```

### 为什么 wb2api 不发布 7863

manager 的 `/v1` 才是对外的正式入口，它提供密钥鉴权、每密钥配额、IP 白名单与
用量统计。若同时暴露上游 7863，任何人都能用那个**共享的单个 api_key** 绕过
上面全部管控。默认配置下 7863 只在 compose 的内部网络中可达。

### 为什么需要 dockerproxy

wb2api 只在进程启动时读取 `config.json` 与扫描 `auths/`（无 SIGHUP），
因此新增账号、改配置后必须重启容器。manager 通过 `docker restart` /
`docker logs` 完成这件事。

直接把 `/var/run/docker.sock` 挂进 manager，等于给它**宿主机 root 等价权限**
（可 exec 进任意容器、挂载宿主目录）。所以改用 `docker-socket-proxy`，
只放行 `CONTAINERS`、`POST`、`ALLOW_RESTARTS` 三项，
并把 `EXEC` / `IMAGES` / `VOLUMES` / `BUILD` / `SECRETS` 等全部显式关闭。

---

## 数据与备份

全部数据放在**宿主目录**（默认 `./data`，由 `.env` 的 `WB_DATA_PATH` 控制），
宿主机上可以直接看到：

```
./data/
├── auths/         账号凭证（含明文 refresh token，**必须妥善保管**）
├── config.json    上游配置（含 api_key、设备 token）
├── state.json     账号池运行时状态（积分、冷却、熔断）
└── manager/       管理端 SQLite（密钥、日志、用量、用户）
```

备份就是拷贝这个目录：

```bash
tar czf suite-backup-$(date +%F).tar.gz -C ./data .
```

**权限说明**：容器以 uid 10001 运行，而 Docker 创建宿主目录时属主是 root。
入口脚本会以 root 启动、把数据目录 `chown` 给 10001，再降权运行服务，
所以正常情况无需手工干预。若日志出现 `permission denied`（常见于
NFS、只读挂载、或用了 rootless Docker），在宿主机执行：

```bash
sudo chown -R 10001:10001 ./data
```

> 想改回 Docker 命名卷（不占项目目录）？见 `docker-compose.yml` 末尾的说明。
> 注意两种方式的数据**不会自动迁移**——切换前先把旧数据拷过去。

---

## 出口代理（可选）

需要经代理才能访问腾讯时（受限网络、或用代理统一出口），在 `.env` 设置：

```bash
WB_HTTP_PROXY=socks5://192.168.1.10:1080
# 或 http://192.168.1.10:7890
```

留空 = 全部直连。

### 两个服务的代理行为不同（刻意如此）

| 服务 | 行为 |
|---|---|
| **manager**（Python/httpx） | 直连腾讯的请求走代理；**调自己上游 `wb2api:7863` 的请求不走代理** |
| **wb2api**（Go 网关） | 出站走 `HTTP_PROXY`/`HTTPS_PROXY`；**不认 `ALL_PROXY`** |

两点容易踩坑，都已在代码层处理，这里说明原因：

1. **manager 调内网上游必须绕过代理**。httpx 的 `proxy=` 参数作用于所有请求，
   配了 SOCKS 后连同一 compose 网络里的 `wb2api:7863` 也会被送去代理，管理端
   于是连不上自己的上游。上游代码没处理这点，本发行版用构建期补丁
   （`docker/patches/apply.py`）按 host 注入直连 transport 解决。

2. **Go 只认 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`**，不支持 `ALL_PROXY`
   （这是 Go 标准库 `httpproxy` 的行为，与 httpx 不同）。
   如果你的代理习惯只配 `ALL_PROXY`，网关不会走代理 —— 改成 `WB_HTTP_PROXY` 即可。
   上游的 Go transport 原本完全没有代理支持（零值 = 恒不使用代理），
   该能力同样由上述补丁注入。

验证代理是否生效：

```bash
# 网关出站（Go）
docker compose exec wb2api env | grep -i proxy
# 管理端出站（httpx）——上方会打印当前出口代理与提示
docker compose logs manager | grep -i 出口代理
```

---

## 常用操作

```bash
docker compose logs -f manager          # 看管理端日志
docker compose logs -f wb2api           # 看上游日志（manager 的「任务记录」即采自此）
docker compose ps                       # 状态
docker compose down                     # 停止（保留数据卷）
docker compose pull && docker compose up -d   # 更新到最新镜像
```

### 更新镜像

镜像构建在 GitHub Actions 上完成并推送到 ghcr.io。

**容器内不提供「一键更新」**（没有 git、systemd，镜像层只读）。
面板上「更新」按钮**不会改动任何代码**——点击后它只会在日志里打印下面的
命令，供你复制到宿主机执行：

```bash
docker compose pull && docker compose up -d
```

> 面板会提示 "管理端 vX → vY" 这类新版本信息，那个检测本身有效
> （它查 GitHub 上的版本号），只是**更新动作必须在宿主机做**。
> 界面上相关文案已改为容器部署的真实说明。
>
> 同样地，「固定上游版本」输入框在容器里不可用（没有上游 git 仓库，
> 无法检出指定版本）。要固定或回退上游版本，用
> `./scripts/sync-upstreams.sh --ref <commit>` 后重新构建镜像。

更新前建议先看新镜像捆绑的上游版本：

```bash
docker inspect ghcr.io/<owner>/workbuddy2api-suite:latest \
  --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' \
  | grep upstream
```

---

## 同步上游更新

`vendor/` 下的两个上游是**快照**，不会自动跟随。上游有更新时：

```bash
git pull
./scripts/sync-upstreams.sh --dry-run    # 只看差异：提交列表 + diff 统计
./scripts/sync-upstreams.sh              # 确认后同步
```

脚本会更新 `upstreams.json` 并打印后续的测试命令。
完整流程与同步后的检查清单见 [`UPSTREAMS.md`](UPSTREAMS.md)。

仓库还带一个每日漂移检测（`.github/workflows/upstream-check.yml`），
发现上游有新提交会自动开 issue。

---

## 直连上游网关（可选，默认关闭）

若确有需要绕过 manager 直接调用 wb2api（例如调试、或不需要多密钥与配额），
在 `docker-compose.yml` 的 `wb2api` 服务下按注释解开端口映射：

```yaml
  wb2api:
    ports:
      - "127.0.0.1:7863:7863"   # 只绑回环，不要写 0.0.0.0
```

**安全代价**：7863 只有一个共享 api_key，且没有配额与 IP 管控；
暴露它等于放弃 manager 提供的全部租户隔离。公网环境请勿这样做。

---

## 已知限制

| 限制 | 说明 |
|---|---|
| 上游更新需人工同步 | 快照不会自动跟随；用 `sync-upstreams.sh` + 漂移检测 issue 兜住 |
| 单镜像体积偏大 | 同时含 Go 二进制、Python 运行时与前端产物，换来版本一致与单产物发布 |
| 面板「上游本地版本」显示未知 | 容器内没有上游 git 仓库；以 `upstreams.json` 与镜像标签为准 |
| socket 代理仍有权限 | 它允许容器重启与读日志，等于把这两项能力交给 manager 容器；已通过禁用 exec/build/volumes 与私有网络收敛，但非零风险 |
| 容器入口以 root 启动 | 仅为 chown 数据目录后立刻 gosu 降权到 uid 10001；服务进程本身非 root。这是宿主目录挂载的必然代价（命名卷无此问题） |
| 代理能力靠构建期补丁注入 | 上游的 httpx 内网绕过、Go transport 代理支持均以补丁实现（`docker/patches/apply.py`）。上游若重构对应代码，**构建会失败**并指出要改哪里——这是刻意设计，避免静默失效 |
| 网关不认 `ALL_PROXY` | Go 标准库只读 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`；代理只配在 `ALL_PROXY` 时网关不走代理 |
| Python 测试在 Windows 有 2 个失败 | `test_first_token` 的临时目录清理在 Windows 上抛 `NotADirectoryError`（上游 `tearDown` 只捕获 `PermissionError`）；Linux / CI 通过 |

---

## 许可证

本项目为 MIT，见 [LICENSE](LICENSE)。
两个上游均为 MIT，其许可证随快照保留在
`vendor/wb2api/LICENSE` 与 `vendor/manager/LICENSE`。
