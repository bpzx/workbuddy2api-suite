# workbuddy2api-suite

基于 [workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) 与
[workbuddy-manager](https://github.com/ithtelab/workbuddy-manager)
构建的**发行版**

两个上游的代码以**未修改的快照**内置于 `vendor/`，来源 commit 完整记录于
[`upstreams.json`](upstreams.json) 与 [`UPSTREAMS.md`](UPSTREAMS.md)。

> ⚠️ **合规须知**：这是**非官方**项目，以上游 CodeBuddy 账号作为服务上游，
> 仅限本人授权账号、本机 / 私有环境测试。使用涉及目标平台服务条款与账号风险，
> 请阅读两个上游的免责声明。

## 建议配置（经验值，按账号数与并发调）

| 资源 | 最低 | 建议 | 受什么影响 |
|---|---|---|---|
| CPU | 1 核 | 2 核 | 大部分时间在等 I/O（转发 SSE、等上游）；并发上去才吃 CPU |
| 内存 | 512 MB | 1–2 GB | manager（uvicorn + SQLite）、wb2api（Go，账号池随账号数增长）、前端静态托管 |
| 磁盘 | 2 GB 可用 | 5 GB 以上 | 镜像本身预计数百 MB 量级（内含 Python 运行时、6 个静态 Go 二进制、docker CLI 与 compose 插件、前端产物）；再加数据目录里日志与用量记录的持续增长 |

> 上面的磁盘数字是**预期值**, 部署后自己量一下更准：
> 
>```bash
> # 镜像大小（解压后）
> docker image inspect ghcr.io/bpzx/workbuddy2api-suite:latest --format '{{.Size}}' \
> | numfmt --to=iec
>   # 各容器实时 CPU / 内存
> docker stats --no-stream
> ```

## 部署

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

## 账号风控相关

网关对外**声称自己是官方桌面客户端**（`X-Product: WorkBuddy`、`X-IDE-*`、
UA `WorkBuddy/5.5.4 … CLI/2.137.1`）。以下几项直接关系到账号会不会被平台风控
标记，建议逐条确认。

### 设备风控凭据 `device_token`

**这是最该先查的一项。** `X-Device-Token` 只在账号带 `device_token` 时才发送
（三级回退：账号级 → 全局 `upstream.device_token` → `device_token_file`；
都取不到就**整条头不发送**）。而**内置登录流程不写这个字段**，所以用本项目
流程加的账号默认都缺它。

上游代码对此的判据很明确（manager 侧注释）：`device_token` 是设备风控凭据，
**丢了会静默降级风控形态**；缺失会被**按设备指纹异常关联风控**。正因为如此，
manager 在换 token 重登时会**专门保留**该字段，避免被冲掉。

**查看哪些账号缺**（只列账号名，不打印凭据本身）：

```bash
# 启动时会直接提示「N/总数 个账号缺少 device_token」
docker compose logs wb2api | grep device_token
# 或者逐个账号看
docker compose exec wb2api sh -c 'for f in /data/auths/*.json; do \
  n=$(basename "$f" .json); \
  grep -q "\"device_token\"" "$f" && echo "$n 有" || echo "$n 缺失"; done'
```

**怎么补**：把令牌写进该账号 auth 文件的**顶层** `device_token` 键（auth 文件的
扁平形与「插件 OAuth 嵌套形」都是放在顶层），或写全局 `upstream.device_token`
（设置页 → 上游配置 → 高级模式）。取值只能来自官方桌面端/插件链路。

### 设备标识与轮换

`X-Machine-ID` / `X-Session-ID` 由 `sha256("<盐>" + 用途 + ":" + uid)[:36]` 稳定
派生 —— 这是**有意设计**（对标官方客户端"每账号一台固定虚拟设备"），
缺了反而会被按设备指纹缺失关联风控。

代价是：某账号的设备标识一旦被上游打标，**重新登录也换不掉**（uid 不变则标识
不变）。所以本发行版给盐留了一个后门（上游原本没有）：

```bash
# .env —— 不设 = 沿用历史默认盐 "wb2a:"（行为与以前完全一致）
WB2A_DEVICE_ID_SALT=<随机值>
```

> ⚠️ 换盐会让**所有账号**的设备标识**同时**变化 —— 在平台看来是"全体换了设备"，
> 本身就可能成为异常信号。**只在确有必要时**（例如确认某账号设备标识已被打标）
> 才用，并且一次换到位。这是本套件设置的**唯一** `WB2A_*` 环境变量，
> 理由：它是一次性轮换密钥，不适合放进设置页。

### 三个容易踩的配置

- **`pool.max_in_flight: 0` = 单账号在途不限**。该值只能写在 `config.json` 里，
  误设不会有任何报错 —— 所以本发行版在你真设成 0 时会在启动日志里告警。
  **保持 3 左右**。
- **`pool.max_in_flight_global` 的 `0` 不等于"不分档"**：配置层会把 `0`/负数归一
  为 `2`（国际版 WAF 更紧，恒分档）。字段注释里"0 = 回落 max_in_flight"指的是
  **池 API 层**的语义，配置层到不了那里 —— **以 2 为准**。
- **合成任务（签到 / 旅行 / 活跃上报）的载荷在所有账号间是同一套**。本发行版
  只给上报**间隔**加了抖动（见 [UPSTREAMS.md](UPSTREAMS.md) 补丁 7）；载荷本身
  仍是固定的（同模型、同长度）。要不要让它按账号变化属**未决项** —— 那等于
  编造不同的事件数据，收益与风险需要你自己判断。

### 客户端版本号会过期

`upstream.client_version` / `cli_version` 决定 UA 与 `X-IDE-Version`（模板里显式
写着契约版本）。官方客户端会升级，一年后这个版本号本身就可能是"老客户端"特征。
可在设置页「出站标识」里改。另有一处**已知不一致**（未改）：
`cmd/login` 登录工具发的是另一套版本号（`CLI/2.63.2 CodeBuddy/2.63.2`），
与网关运行时不一致；改动它需要确认登录端点仍接受，属需要单独验证的项。

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

> ⚠️ **workbuddy2api 的上游仓库已被删除**（`Sliverkiss/workbuddy2api` 现为 404）。
> 该部分**由本项目自行维护**，不再有可同步的目标 —— 详见
> [`UPSTREAMS.md`](UPSTREAMS.md)。`manager` 上游正常。

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

## 许可证

本项目为 MIT，见 [LICENSE](LICENSE)。
两个上游均为 MIT，其许可证随快照保留在
`vendor/wb2api/LICENSE` 与 `vendor/manager/LICENSE`。
