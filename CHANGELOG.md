# 更新日志

本文件记录 **workbuddy2api-suite（套件自身）** 的变更。
每个版本都会注明它捆绑的上游 commit —— 下游问题若要溯源到上游行为，
看这里就能定位到确切版本。

上游自身的更新日志见 `vendor/manager/CHANGELOG.md` 与
`vendor/wb2api/README.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

---

## [未发布]

### 修复

- **CI 编译失败：`pattern defaultprompt.md: no matching files found`**。
  根因是 vendoring 的经典陷阱——上游 `vendor/wb2api/.gitignore` 里的
  **bare 模式 `*.md`** 是递归的，快照放进 `vendor/` 后把
  `internal/prompt/defaultprompt.md`（`//go:embed` 的编译期必需文件）
  一并排除了。

  这个故障的迷惑性在于：**本地 `go build` 完全正常**（文件就在磁盘上），
  只有 CI 从 git 全新 clone 时才暴露。同类误伤还有 `config.json` 规则命中
  `.github/actions/ai-governance/config.json`。

  修法：
  1. 移除 vendor 下全部上游 `.gitignore`（4 个）——它们是忽略规则，不是源文件；
  2. 保护规则改由根 `.gitignore` 用**锚定路径**显式表达，不再使用 bare 模式
     （bare 模式会递归进 vendor/ 深层）；
  3. 前端产物类排除项（`node_modules`、`out`、`.next` 等）同步收敛到锚定路径。

- **同步脚本新增两道防线**，防止此类疏漏复发：
  - 导出快照时**自动移除**上游 `.gitignore`；
  - 同步结束时做**纳出完整性校验**：vendor/ 磁盘上每个文件都必须出现在
    git 索引里，否则报错退出。这是唯一能自动发现「文件被静默忽略」的手段——
    `git add -A` 不报错，测试也可能全绿，只有逐文件对账能发现。

  > 该校验在开发中当场抓到了真实问题（中文文件名被 git 转义导致的假阳性），
  > 已改用 NUL 分隔比较（`-z`）修正。

### 修复

- **面板「更新」按钮的误导性文案**（用户实测反馈）。上游文案描述的是
  **裸机部署**："点下方按钮即可更新，账号与配置会自动保留"、"从 GitHub
  拉取最新代码并自动重建/重启"、按钮 hint "重建容器 / 替换代码与前端"。
  但本项目在构建期已把 `deploy/update.py` 换成替身脚本（不执行实际更新），
  文案却不一致 —— 用户按提示点按钮，**什么都不发生**。

  这是本项目的设计缺陷：替换了更新器的**行为**，却没改**界面文案**。

  修法（补丁 3）：顶部提示行、面板标题与说明、三个按钮 hint、确认弹窗措辞
  全部改为容器部署的真实说明；「固定上游版本」整块标注为容器内不可用
  （无上游 git 仓库），并给出 `sync-upstreams.sh --ref` 的替代做法。
  CI 增加断言：补丁必须落地，且旧的误导文案必须消失。

### 新增

- **出口代理支持**（`.env` 的 `WB_HTTP_PROXY`），两个服务分别处理：

  - **manager**：修复配 SOCKS 代理时的
    `ImportError: Using SOCKS proxy, but the 'socksio' package is not installed`
    —— 镜像补装 `httpx[socks]`（上游 requirements 只写了 `httpx`，不含 socks 支持）。
    同时修复一个更隐蔽的问题：httpx 的 `proxy=` 作用于**所有**请求，
    配了 SOCKS 后连同一 compose 网络内的上游 `wb2api:7863` 也会被送去代理，
    管理端连不上自己的上游。现已按 host 注入直连 transport。
    > 实测确认：显式传 `proxy=` 时 `no_proxy` 环境变量**完全失效**，
    > 只有 `mounts` 能精确指定直连。

  - **wb2api（Go 网关）**：上游的 `http.Transport` 未设 `Proxy` 字段，
    Go 零值 = **恒不使用代理**且不读环境变量，网关出站无法走代理。
    现已支持 `HTTP_PROXY`/`HTTPS_PROXY`/`NO_PROXY`。
    > 注意 Go 标准库**不认 `ALL_PROXY`**（与 httpx 不同）。

- **构建期补丁机制** `docker/patches/apply.py`：上述两处能力上游没有，
  但本项目约定「vendor 代码一字不改」，因此改为构建期打补丁。
  补丁带 **fail-fast 断言**：锚点找不到就**让构建失败**并指出要改哪里，
  而不是静默跳过（静默会让镜像能启动但代理悄悄失效）。
  CI 已加一步「补丁必须真的落地」的校验。

- **数据默认放宿主目录**（`./data`，由 `WB_DATA_PATH` 控制）：
  宿主机可直接查看与备份，不再需要透过 `docker run` 进命名卷。
  - 配套解决权限问题：宿主目录属主是 root，而服务以 uid 10001 运行，
    会 permission denied。入口脚本改为 root 启动 → `chown` 数据目录 →
    `gosu` 降权到 10001，服务进程本身仍非 root。
  - 想改回命名卷：`docker-compose.yml` 末尾有说明（两种方式数据不自动迁移）。

### 说明

- 根 `.gitignore` 顶部写明了「所有模式一律锚定」的强制约定与原因，
  后续新增规则需遵守，否则会重演本事故。
- `UPSTREAMS.md` 新增「构建期补丁登记」一节：记录每处补丁的上游位置与理由，
  便于上游重构时定位。

---

首个版本：把 workbuddy2api 与 workbuddy-manager 整合为单一镜像、compose 部署。

### 捆绑的上游版本

| 上游 | 分支 | Commit | 提交时间 |
|---|---|---|---|
| Sliverkiss/workbuddy2api | `master` | `39f3c3f` | 2026-09-15 14:17 |
| ithtelab/workbuddy-manager | `main` | `3ed6c48` | 2026-09-15 13:47 |

- wb2api：`fix(scheduler): 去串行化 + sleep 换 select-ctx 可取消`
  （含 20+ 提交：熔断与模型冷却持久化、429 冷却对齐上游重置时间、
  tool_call 配对清理与截断检测、推理档位透出、跨平台路径修复、
  `prompt_cache_key` 费用优化）
- manager：`improve(update): deploy/ 差异提示说清「要不要紧」`
  （面板显示版本 **v1.0.31**）

### 集成层核对（同步后）

- **三处补丁全部正常应用**（共 10 个替换点）。wb2api 的 `client.go` 本次
  改动很大，但 `http.Transport` 构造处未变；manager 的 `http_client()` 与
  `UpdatePanel.tsx` 均未变。
- **SSRF 守卫不冲突**：manager 本次新增 SSRF 防护（`_reject_internal_host`），
  但它只作用于用户可传 URL 的接口（Upstash 连通性测试），
  **不在 manager 调自己上游的路径上**，与补丁 1 的内网直连不冲突。
- **替身脚本契约未变**：新版 `deploy/update.py` 的 `--target` 与状态文件
  约定与替身一致。
- **配置模板无需改**：上游 `config.example.json` 零差异。
- **env 变量集合未变**（21 个）：compose 无需调整。
- **Dockerfile COPY 清单未变**：无需跟改。

> **Go 测试已全绿**：先前那 2 个在 Windows 上必现失败（`rate_limited_models`
  依赖亚毫秒时钟精度）的用例，被上游本次改动顺带修掉了。现在
  `go test ./...` 在 Windows 上也是 16/16 包全过。

### 新增

- **单镜像双服务**：一个镜像内同时装 Go 网关与 Python/静态前端管理端，
  compose 用不同 command 拉起两个容器。版本一致性由单镜像从根上保证。
- **三容器 compose**：`wb2api`（内部 7863，不发布）+ `manager`（对外 7864）
  + `dockerproxy`（socket 代理）。
- **socket 代理隔离**：manager 需要 `docker restart/logs` 上游容器，
  但直接挂 `/var/run/docker.sock` 等于给它宿主机 root 等价权限。改用
  `tecnativa/docker-socket-proxy` 只放行 `CONTAINERS`/`POST`/`ALLOW_RESTARTS`，
  并显式关闭 `EXEC`/`IMAGES`/`VOLUMES`/`BUILD`/`SECRETS` 等。
- **上游来源锁定**：`upstreams.json`（机器可读）+ `UPSTREAMS.md`（人读）+
  OCI 镜像标签 + 镜像内 `/opt/suite/upstreams.json`，四处留痕可追溯。
- **一键同步脚本** `scripts/sync-upstreams.sh`：`--dry-run` 预演、
  `--only` 单选、`--ref` 回退到指定 commit、`--reexport` 重新导出。
  导出先落临时目录并校验，通过后才替换 vendor/，失败不留半个目录。
- **上游漂移检测** `.github/workflows/upstream-check.yml`：每日比对锁定
  commit 与上游最新，漂移则开/更新 issue。
- **CI 三关门禁**：镜像构建前先跑 Go build/vet/test、Python unittest、
  前端静态导出，任一失败即不出镜像。
- **首次启动自举**：entrypoint 从模板生成 `/data/config.json` 并随机化
  `api_key`；对已存在的配置只做只读校验并告警，不悄悄改写。

### 变更（相对直接使用上游的裸机部署）

- **`WB2A_*` 环境变量一律不设**：上游的 `applyEnv()` 会在读完 config.json
  后用环境变量覆盖，一旦设置，manager 设置页写入的对应项会被静默盖掉。
  保持 config.json 为唯一事实来源。
- **容器内「一键更新」改为引导 compose**：`docker/stub/update.py` 在构建期
  覆盖 `/opt/manager/deploy/update.py`（vendor 源码不动）。裸机版会 git 检出
  并 `systemctl restart`，在容器里跑会把运行中的代码改坏。
- **不发布 7863**：让 manager 的密钥鉴权、配额与 IP 管控无法被绕过。
- **保留 vendor 下的 `.github`**：GitHub 只搜索仓库根目录的 workflows，
  嵌套的不会被触发；而 manager 的测试会读自己的 `.github/workflows/release.yml`，
  排除掉会造成假失败。

### 已知限制

- 上游更新需人工执行 `scripts/sync-upstreams.sh`（这是"可追溯同步"的必然代价）。
- 单镜像体积偏大（含 Go 二进制 + Python 运行时 + 前端产物）。
- UI 中「上游本地版本」显示为未知：容器内没有上游 git 仓库；
  套件版本与上游 commit 以 `upstreams.json` 与镜像标签为准。
- 在 Windows 上跑 Go 测试会有 2 个 `rate_limited_models` 用例失败：
  `CooldownSoftForModel` 内两次 `time.Now()` 的差值在 Windows 约 0.5ms 时钟
  粒度下相等，导致 `ResetAt` 被 `omitempty` 省略。Linux（含 CI）上通过。
