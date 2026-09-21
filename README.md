# workbuddy2api-suite

基于 [workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) 与
[workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)
构建的**发行版**：把两者锁定到确定的版本、打包成一个预构建镜像，
并补上它们在容器化部署下缺的几块能力。

- **workbuddy2api**（Go）— 把 CodeBuddy 账号池包装成 OpenAI 兼容接口的反代网关
- **workbuddy-manager**（FastAPI + Next.js）— 配套的 Web 管理控制台 + 对外多密钥网关

## 部署机要求与建议配置

### 必须满足

| 项 | 要求 | 说明 |
|---|---|---|
| Docker | Engine + **Compose v2**（即 `docker compose`，不是旧的 `docker-compose`） | compose 文件用了顶层 `name:`（需 Compose v2.3+） |
| CPU 架构 | `linux/amd64` 或 `linux/arm64` | CI 只构建这两个平台，其余架构需自行构建 |
| 出网 | 能访问 ghcr.io（拉镜像）、GitHub API（版本检测）、腾讯（网关上游） | 全程不通外网时需配 `WB_HTTP_PROXY`（支持 http/socks5） |
| 数据目录 | **本地文件系统**，且可 `chown` | 入口脚本以 root 修正属主后降权到 uid 10001。**NFS / 只读挂载会失败**（`.env.example` 里有手工修复说明） |
| 时钟 | 宿主时间准确 | 镜像内固定 `TZ=Asia/Shanghai`；签到、夜猫等定时任务按本地时区判定 |

### 建议配置（经验值，按账号数与并发调）

| 资源 | 最低 | 建议 | 受什么影响 |
|---|---|---|---|
| CPU | 1 核 | 2 核 | 大部分时间在等 I/O（转发 SSE、等上游）；并发上去才吃 CPU |
| 内存 | 512 MB | 1–2 GB | manager（uvicorn + SQLite）、wb2api（Go，账号池随账号数增长）、前端静态托管 |
| 磁盘 | 2 GB 可用 | 5 GB 以上 | 镜像本身预计数百 MB 量级（内含 Python 运行时、6 个静态 Go 二进制、docker CLI 与 compose 插件、前端产物）；再加数据目录里日志与用量记录的持续增长 |

> 上面的磁盘数字是**预期值，未在本仓库实测**（本项目没有实跑过镜像构建）。
> 部署后自己量一下更准：
>
> ```bash
> # 镜像大小（解压后）
> docker image inspect ghcr.io/bpzx/workbuddy2api-suite:latest --format '{{.Size}}' \
>   | numfmt --to=iec
> # 各容器实时 CPU / 内存
> docker stats --no-stream
> ```

### 不建议这样部署

- **不要把 7864 直接暴露到公网**。它是唯一对外入口，但应经 Cloudflare Tunnel 或
  反向代理，并把 `MANAGER_BIND` 设为 `127.0.0.1` —— 否则 IP 白名单、登录失败锁定
  这些管控会失去意义。配置写法见 [`.env.example`](.env.example) 的
  「用 Cloudflare Tunnel 上线」一节。
- **不要把数据目录放在 NFS / 网络盘上**：属主修正会失败，而账号凭证与 SQLite
  对文件锁与延迟也敏感。

### 平台说明

**Linux 是目标环境**（CI 构建、测试与本文档均以 Linux 为准）。
macOS / Windows 的 Docker Desktop 可以跑，但有两处请注意，其中一键更新在
Windows 上**未实测**：

- 一键更新的 helper 会把套件目录按**同一个绝对路径**挂进容器（compose 靠这个
  才能解析相对路径）。Docker Desktop 在 Windows 上要做 WSL 路径转换，这条路径
  转换没有实测过。
- 数据目录的属主由 entrypoint 修正；Docker Desktop 的绑定挂载语义与 Linux 不同，
  可能在日志里看到属主告警（功能不受影响）。

## 与直接用上游的区别

两个上游**各自都自带 Dockerfile 与 compose**，所以"能容器化"不是本项目的价值。
本项目提供的是它们没有的那些：

| 本项目的做法 | 上游的做法 | 为什么要这样 |
|---|---|---|
| **出口代理支持**（HTTP/SOCKS5，含鉴权） | ❌ 都没有 | 上游的 Go transport 不读代理环境变量；manager 配了代理后连自己的上游也会绕代理。两处都靠构建期补丁修（见 [`UPSTREAMS.md`](UPSTREAMS.md)） |
| **套件一键更新**（面板里拉镜像 + 重建容器） | 裸机更新脚本（`git pull` + `systemctl restart`） | 本发行版用预构建镜像，容器内改代码会被下次 `pull` 覆盖。更新交给一个**只持有 socket 的最小侧车**，manager 本身仍不碰 socket（见 [一键更新与安全边界](#一键更新与安全边界)） |
| **单镜像、ghcr 预构建** | 各自 `build: .` 本地构建 | 一次构建、一处版本号；部署机不需要 Go/Node 工具链，`docker compose pull` 即可 |
| **socket 代理隔离** | 直接挂 `/var/run/docker.sock` | 上游的 manager 需要它来重启/读日志上游容器。裸挂等于把宿主 root 交给该容器；本项目用 `docker-socket-proxy` 只放行所需 API |
| **版本锁定 + 同步机制** | 跟随各自分支 | `upstreams.json` 记录确切 commit，`sync-upstreams.sh` 一条命令同步，每日漂移检测开 issue。好处是"上游某次更新坏了"时你能明确回退到可用版本 |
| **四容器一键起** | 各自 compose，需手工组网 | 本项目 compose 把网关、管理端、socket 代理、更新侧车一次编排好 |

> **设计取舍**：上游更新很快（manager 曾从 v1.0.35 到 v1.0.57 跨 50+ 提交），
> 因此本项目**刻意不去改上游的界面与文案** —— 那类补丁上游一重构就失效，
> 收益又仅是措辞更贴切。目前只保留 3 处补丁（2 处在代理代码、1 处是注册我们
> 自己的路由）。
>
> 唯一的例外是**更新面板**：它被**整文件覆写**（不是打补丁），因为要改的是整个
> 渲染结构。代价是放弃该文件的上游后续改进，每次同步上游要人工看一眼 ——
> 这一条如实登记在 [`UPSTREAMS.md`](UPSTREAMS.md) 的「覆写登记」。
> 其余「曾经打过、现已删除的补丁」的教训见同一文件。

两个上游的代码以**未修改的快照**内置于 `vendor/`，来源 commit 完整记录于
[`upstreams.json`](upstreams.json) 与 [`UPSTREAMS.md`](UPSTREAMS.md)。

> ⚠️ **合规须知**：这是**非官方**项目，以上游 CodeBuddy 账号作为服务上游，
> 仅限本人授权账号、本机 / 私有环境测试。使用涉及目标平台服务条款与账号风险，
> 请阅读两个上游的免责声明。

---

## 快速开始

### 前置条件

- 一台满足 [部署机要求与建议配置](#部署机要求与建议配置) 的机器
- 一个或多个已注册的 CodeBuddy 账号（用于 OAuth 登录）

### 部署

```bash
git clone https://github.com/bpzx/workbuddy2api-suite.git
cd workbuddy2api-suite

cp .env.example .env
# 可选：设置 WB_ADMIN_PASSWORD；留空则随机生成并打印到日志
# 可选：设置 SUITE_UPDATER_TOKEN 以启用面板「一键更新」（见下）

docker compose pull
docker compose up -d
```

首次启动会自动生成 `/data/config.json` 并把 `api_key` 随机化。

> `SUITE_UPDATER_TOKEN` 留空也能正常部署 —— 只是面板的「一键更新」按钮不可用，
> 会显示原因与宿主机命令。想用一键更新就填一个随机值
> （`openssl rand -hex 24`）再 `docker compose up -d`。
> 它会放宽权限，**请先读 [一键更新与安全边界](#一键更新与安全边界)**。

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

# 更新侧车存活（不发布端口，从容器内查）
docker compose exec updater curl -fsS http://127.0.0.1:7865/healthz

# 上游网关状态（内部端口，未发布到宿主机）
docker compose exec manager curl -s -H "Authorization: Bearer <api_key>" \
  http://wb2api:7863/healthz
```

`docker compose ps` 应看到四个服务：`wb2api`、`manager`、`dockerproxy`、`updater`。

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

                    ┌───────────────────────────────┐
                    │  updater  :7865（不发布端口）  │
                    │  一键更新：拉镜像 + 重建容器   │
                    │  ★ 唯一持有 docker socket 的   │
                    │    组件，见「一键更新与安全边界」│
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

**能力边界（刻意如此）**：这套白名单足以让 manager 重启上游与读日志
（这正是它需要的），但 **`docker info` 会失败** —— 因为 `/info`
端点在 `INFO` 段（已关闭）。上游 manager 用 `docker info` 判断"能否操作
docker"，于是在本发行版里该判定恒为「否」。

**上游**更新走 `./scripts/sync-upstreams.sh` + 重建镜像
（见 [`MAINTAINING.md`](MAINTAINING.md)）—— 那是维护者的流程，**不写在界面上**：
本套件的部署方（使用者）无法自行同步上游，把维护者的步骤摆在界面上只会
让人以为"我是不是该做点什么"。界面上只呈现"上游有没有更新"这个事实。

若你确实想放开这块，需要打开 `INFO` 段并放宽容器操作权限 ——
但那会显著扩大该容器的权限，**不建议**。

> 注意区分：**上游**不能由面板更新；**本套件自己**可以（一键更新），
> 走的是一个独立的、只持有 socket 的最小侧车 —— 见下节。这样 manager 本身
> 仍然完全不碰 socket。

---

## 一键更新与安全边界

「本套件」的一键更新由新增的 `updater` 服务执行。它是典型的 **sidecar（侧车）
模式** —— 与主服务同处一个 compose 网络、随主服务一起编排、只负责一件事的
辅助容器（这里那件事就是"拉镜像 + 重建容器"）。下文沿用「侧车」这个叫法。

**注意：这个词只出现在文档与 compose 注释里，界面上不会出现** ——
面板给用户看的是"一键更新不可用"加上具体原因与可复制的命令。

这一节如实说明它的权限取舍，因为**它确实放宽了权限**，你应当知情。

### 它是怎么工作的

```
面板点「一键更新」
      │  POST /api/system/suite-update（管理员 + token）
      ▼
manager  ──HTTP(token)──▶  updater 侧车（唯一持有 docker socket）
                                 │  派生一次性 helper 容器
                                 │  （不属 compose 项目，避免重建到自己）
                                 ▼
                            helper：docker compose pull && up -d
                                 │  重建 wb2api / manager / updater
                                 ▼
                            进度写入共享状态文件 → 面板实时显示
```

为什么要绕 helper 这一层：`docker compose up -d` 会重建**包括 updater 在内**的
所有服务。若侧车自己执行，它会在命令跑到一半时被重建掉，既拿不到结果也写不完
状态。放在 compose 项目**之外**的一次性容器里执行就没有这个自杀竞争。

### 权限取舍（重要）

| 组件 | docker socket | 能做到什么 |
|---|---|---|
| `manager`（**公网暴露面**） | ❌ 不碰 | 经 `dockerproxy` 只能 restart / 读日志；向侧车发一个**无参数**的固定触发请求 |
| `updater` 侧车（不发布端口） | ✅ 直连 | 拉镜像、重建容器 —— 等价于宿主机 root |

**为什么必须放宽**：拉取镜像并重建容器，本质上就需要 socket 的 IMAGES /
创建容器等能力，无法规避。把这些 API 全加进 `dockerproxy` 的白名单等于让代理
失去意义，所以改为**把 socket 收敛到一个最小侧车**。

**换来了什么**：经 Cloudflare Tunnel 对外、攻击面最大的 manager **依然拿不到
任意 docker 能力** —— 即使它被攻破，攻击者能触发的也只是侧车那条写死的
`compose pull && up -d`，而不是"在宿主机上跑任意容器"。

**风险仍然存在，如实说明**：侧车本身（约 350 行、不发布端口、只接受带 token 的
固定请求）若被攻破，等同于宿主机 root。这是这条能力链路的固有代价。

### 不想要它

`SUITE_UPDATER_TOKEN` 留空时侧车会**拒绝一切更新请求**，面板显示原因并给出
宿主机命令 —— 功能降级但一切照常工作。要彻底移除，把 `docker-compose.yml` 里
的 `updater` 服务整段注释掉。

---

## 数据与备份

全部数据放在**宿主目录**（默认 `./data`，由 `.env` 的 `WB_DATA_PATH` 控制），
宿主机上可以直接看到：

```
./data/
├── auths/            账号凭证（含明文 refresh token，**必须妥善保管**）
├── config.json       上游配置（含 api_key、设备 token）
├── config.json.bak   启动时增量补齐前的备份（仅在确实补过键时出现）
├── state.json        账号池运行时状态（积分、冷却、熔断）
└── manager/          管理端 SQLite（密钥、日志、用量、用户）
```

### config.json 会被「增量补齐」

`config.json` 只在**首次启动**时从模板生成，之后不再重新生成（重新生成会覆盖你在
设置页里改过的配置）。但上游版本更新时会**新增**配置项，老部署里没有那些键，
表现为设置页出现看不懂的显示（例如输入框里显示字面量 `undefined`）。

因此入口脚本每次启动会做一次**只补不改**的补齐：

- 只添加模板里有、`config.json` 里没有的键；**已存在的值一律保留**（哪怕你改过、
  或类型与模板不同）；
- 补了哪些键会打进 `docker compose logs wb2api`，并先备份成 `config.json.bak`；
- 上游**移除**的旧键不会被删（不改动你的文件）——例如 `server.max_body_mb`
  仍在你的配置里，但它对当前上游版本已无作用（见「已知限制」）。

因此看到启动日志里出现 `+ pool.cost_explore_interval` 这类行是**正常的**，
它表示这次补齐了上游新增的配置项。

备份就是拷贝这个目录（`config.json.bak` 可以不备份）：

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

### 更新镜像（一键更新）

镜像构建在 GitHub Actions 上完成并推送到 ghcr.io。
**面板「设置 → 系统更新」提供一键更新**：拉取新镜像 → 重建容器，进度实时显示在
面板的日志区。容器重建时页面会短暂断开，恢复后自动显示结果。

启用前需要在 `.env` 里设置一个 token：

```bash
SUITE_UPDATER_TOKEN=<随机值>      # 生成：openssl rand -hex 24
```

`docker compose up -d` 后生效。**留空 = 拒绝所有更新请求**（安全默认），
此时面板显示「一键更新不可用」并给出可在宿主机直接执行的命令 —— 可用的降级，
不是故障。权限取舍见 [一键更新与安全边界](#一键更新与安全边界)。

> 更新过程中容器会被重建，包括 manager 自己 —— 所以浏览器里的页面会断开一次，
> 这是预期行为。

#### 面板显示什么

| 区块 | 内容 | 能否更新 |
|---|---|---|
| **本套件** | 当前版本 → 最新正式版 | ✅ 一键更新 |
| 上游网关 workbuddy2api | 固定 commit → 远端 HEAD | ❌ 只报告 |
| 上游管理端 workbuddy-manager | 快照版本 → 上游最新 Release | ❌ 只报告 |

> ⚠️ 第三段显示的是**上游 workbuddy-manager 自己的版本号**（如 `v1.0.60`），
> **不是本项目的版本**。本项目的版本看第一段（形如 `v0.2.3`）。
> 这个数字曾被误读过，所以现在明确归在「上游依赖」区块里。

上游两块不能更新的原因：容器里既没有上游 git 仓库，也不该由运行中的容器去改
自己的代码。上游更新走维护者流程，见 [`MAINTAINING.md`](MAINTAINING.md)。

#### 版本语义：正式版 vs 开发构建

面板只把**正式版**当作"最新版本"：

- 当前是正式版（如 `v0.2.3`）→ 与最新正式版做语义化版本比较，只在**严格更新**
  时提示（不会把降级当成更新）
- 当前是开发构建（由 main 分支产出，没有版本号）→ 如实显示为**「开发构建」**，
  并列出可切换到的最新正式版；不参与版本比较，避免每次提交都变成"有新版本"

镜像标签怎么选（`latest` / `main` / `sha-xxxxxxx`）见 `.env.example` 的
`SUITE_IMAGE` 说明 —— 想钉住某个版本或跟随开发构建都在那里配置。

#### 等价的手动做法

不经过面板，直接在宿主机执行：

```bash
docker compose pull && docker compose up -d
```

#### 更新前先看新镜像捆绑的上游版本

```bash
docker inspect ghcr.io/<owner>/workbuddy2api-suite:latest \
  --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' \
  | grep upstream
```

---

## 上游快照与同步（维护者）

`vendor/` 下的两个上游是**固定快照**，不会自动跟随。它们捆绑的确切 commit 记录在
[`upstreams.json`](upstreams.json) 与 [`UPSTREAMS.md`](UPSTREAMS.md)；镜像的 OCI
标签里也带一份，可用：

```bash
docker inspect ghcr.io/bpzx/workbuddy2api-suite:latest \
  --format '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' \
  | grep upstream
```

**同步流程是维护者的事，写在 [`MAINTAINING.md`](MAINTAINING.md)** ——
部署方无法也不应自行同步上游：那需要重建镜像，而不是改本机配置。
这里只放这个指向，避免把维护者的流程摆在部署方的文档里。

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
| 设置页有「旧版上游」区块 | 上游设置页的「请求上限（旧版上游）／请求体上限（旧版上游）」对**本套件捆绑的版本已无作用**（上游 `9d1a21b` 起移除了 `server.max_body_mb`，该 commit 早于我们锁定的 `d1023f3`）。上游刻意保留该字段以兼容仍跑旧版上游的部署，并在界面上如实标注了「旧版上游」。本项目**不为它打补丁** —— 那属于纯外观改动，而要改的 `settings/page.tsx` 是上游改动最频繁的文件之一（本次同步它就变了 41 行），补丁锚点会持续失效。你的配置里若仍有 `server.max_body_mb`，那是历史遗留键，留着无作用也不影响启动 |
| 单镜像体积偏大 | 同时含 Go 二进制、Python 运行时与前端产物，换来版本一致与单产物发布 |
| **更新面板是覆写的** | `UpdatePanel.tsx` 被整文件替换（改的是整个渲染结构），因此**上游对该文件的后续改进不会自动流入**，每次同步上游要人工看一眼。已登记在 [`UPSTREAMS.md`](UPSTREAMS.md) |
| **updater 侧车有高权限** | 它直连 `/var/run/docker.sock`（拉镜像 + 重建容器的固有要求），等价于宿主机 root。已通过"只给最小侧车、manager 不碰 socket、不发布端口、token 校验、只接受无参数固定请求"收敛，但**非零风险**。详见 [一键更新与安全边界](#一键更新与安全边界) |
| socket 代理仍有权限 | 它允许容器重启与读日志，等于把这两项能力交给 manager 容器；已通过禁用 exec/build/volumes 与私有网络收敛，但非零风险 |
| 容器入口以 root 启动 | 仅为 chown 数据目录后立刻 gosu 降权到 uid 10001；服务进程本身非 root。这是宿主目录挂载的必然代价（命名卷无此问题） |
| 集成改动靠构建期施加 | 本项目与上游的差异**全部在构建期**施加（`vendor/` 始终保持上游原样），所以镜像内容与上游快照并不完全一致。任一改动的前提不成立时（上游重构了锚点 / 占用了同名 i18n 键 / 删掉了被覆写的文件）**构建会失败并指出原因**——刻意设计，静默失效比构建失败难查得多。完整的补丁与覆写登记见 [`UPSTREAMS.md`](UPSTREAMS.md)，构建细节见 [`MAINTAINING.md`](MAINTAINING.md) |
| 上游更新按钮已移除 | 面板不再提供上游更新入口（容器里没有上游 git 仓库，也不该由运行中的容器改自己的代码）。上游两块只显示"是否有更新"；同步与重建镜像由维护者执行，见 [`MAINTAINING.md`](MAINTAINING.md) |
| 网关不认 `ALL_PROXY` | Go 标准库只读 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`；代理只配在 `ALL_PROXY` 时网关不走代理 |
| Python 测试在 Windows 有 2 个失败 | `test_first_token` 的临时目录清理在 Windows 上抛 `NotADirectoryError`（上游 `tearDown` 只捕获 `PermissionError`）；Linux / CI 通过。已核实与本地改动无关（未打补丁的原始快照同样失败） |

---

## 许可证

本项目为 MIT，见 [LICENSE](LICENSE)。
两个上游均为 MIT，其许可证随快照保留在
`vendor/wb2api/LICENSE` 与 `vendor/manager/LICENSE`。
