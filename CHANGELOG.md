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

### 界面调整（按实际使用反馈）

- **移除顶部的"发现新版本"弹窗**。上游每个浏览器会话检测一次新版本就弹 toast，
  本发行版必须去掉它 —— 那条提示**两处都不成立**：
  ① 它显示的版本号是**上游 workbuddy-manager** 的版本，不是本套件的版本
  （「管理端 v1.0.60」很容易被读成"本项目是 v1.0.60"，这个误读真实发生过）；
  ② 它的文案指向「设置 → 系统更新 一键升级」，而该页**并不提供升级上游的按钮**
  （上游更新必须走宿主机同步 + 重建镜像），等于提示用户去做一个做不到的操作。

  版本信息统一看「系统更新」页（本套件 + 两个上游分三段，各自标明能否更新）。
  实现为**文本补丁 4**（`ManagementBar.tsx`）：整块 `useEffect` 替换为说明注释。
  注意这属于"去掉不该出现的行为"，不是"改措辞"——后者是本项目明确不做的事。

- **界面文案去行话、去重复**：
  * 「侧车」这类只在开发文档里有意义的词**不再出现在界面上**（改说"更新服务"）；
    该词仍在 README 与 compose 注释里使用，并在首次出现处给出解释。
  * 「一键更新不可用」的原因与"也可以直接在宿主机执行"**拆成两行** ——
    此前拼成一句，读起来是"请在宿主机执行：未设置 SUITE_UPDATER_TOKEN"，语义不通；
    后端给的原因文案也改写为自带修复方法（"在 .env 里设置 SUITE_UPDATER_TOKEN 即可启用"）。
  * 「本套件不更新上游」与「容器无法操作 docker」原来写成两句，实际在说同一件事，
    已**合成一句**（能力标志仍被真实使用：无法操作 docker 时追加一句原因）。
  * 没有日志时**整块隐藏**更新日志卡片 —— 首次部署满屏的"暂无日志"只是噪音。
  * 文案键从 30 个精简到 27 个，删掉三个没人引用的死键。

- **新增构建期改动的预检测试**（`tests/test_patch_preflight.py`，10 项）：
  在**原始 `vendor/`** 上验证全部前提（锚点唯一存在、覆写目标仍在、新增目标未冲突、
  i18n 语系齐全），并断言补丁的**意图** —— 例如"替换后那条 toast 必须消失"，
  而不只是"锚点能匹配"（后者在替换内容写错时照样通过）。
  好处：本地毫秒级就能发现"上游重构导致锚点失效"，不必等构建或 CI。

### 新增：本套件一键更新（「系统更新」面板重构）

「系统更新」页从上游的「3 个更新目标（both / upstream / manager）」重构为三段式，
并新增**更新本套件自身镜像**的能力：

| 区块 | 显示 | 按钮 |
|---|---|---|
| **本套件** | 当前版本（构建期从 git tag 读入）→ 最新正式版 | **一键更新** |
| 上游网关 workbuddy2api | 固定 commit → 远端 HEAD，是否有更新 | 无 |
| 上游管理端 workbuddy-manager | 快照版本 → 上游最新 Release，是否有更新 | 无 |

上游两块**只报告、不更新** —— 上游更新必须走宿主机
`./scripts/sync-upstreams.sh` → 重建镜像 → `compose pull && up -d`。
删除了原先的 3 个目标磁贴、上游版本固定卡片与签名徽章（它们在容器里本就不工作）。

**版本从 tag 读入**。`SUITE_VERSION` 此前**只**进 OCI 标签，运行中的容器无从知道
自己是什么版本 —— 而"一键更新"必须先能比较当前与最新。现在同时写入
`/opt/suite/version` 并作为 `WB_SUITE_VERSION` 环境变量注入。

判定**只认正式 tag**（`vX.Y.Z`）：main 分支产出的 `sha-*` 构建如实显示为
「开发构建」而不参与版本比较，避免每次提交都变成"有新版本"。

**updater 侧车**（新增服务，本套件**唯一**持有 docker socket 的组件）：
收到触发 → 派生一个一次性 helper 容器（**不属 compose 项目**，否则
`compose up -d` 会重建到它自己）→ helper 在宿主机侧执行
`docker compose pull && up -d`，输出流式写入状态文件，面板实时显示日志。

为此需要的两项改动：
- 镜像内**安装 docker compose 插件**（钉在 v2.40.3）。docker 官方静态包不含它
  （上游也因此吃过 issue #28），且**必须停在 v2** —— v5 的 `up --build` 改为调用
  外部 buildx 且无回退分支。
- compose 新增 `updater` 服务（不发布端口，只在内部网络暴露 7865）。

**安全边界（如实记录）**：updater 直连 `/var/run/docker.sock`，等价于宿主机 root
—— 这是"拉镜像 + 重建容器"的固有要求，无法规避。**但公网暴露面（manager）依然
完全不碰 socket**：它只能向侧车发一个**无参数**的固定触发请求（带 token 校验）。
这是相比"把 socket 直接给 manager"的实质收益。`SUITE_UPDATER_TOKEN` 留空时侧车
拒绝一切更新请求，面板显示原因并给出可在宿主机直接执行的命令（可用的降级）。

**顺带修掉一个真实缺陷**：容器里没有上游 git 仓库，上游
`updater._local_upstream_head()` 恒返回空串，导致 `upstream.has_update`
**永远为 False**（表现为"上游明明发了新版，面板却说不更新"）。
现在改为读镜像内烤好的 `/opt/suite/upstreams.json` 拿固定 commit。

**构建期机制扩展**：`apply.py` 从"只支持文本补丁"扩展为**四类改动** ——
文本补丁 / i18n 键合并 / 文件新增 / 文件覆写 —— 共用同一套"先整体校验、
再统一落盘"的 fail-fast 语义。其中 i18n 键合并在**上游自行占用了同名键时会让
构建失败**，而不是静默覆盖上游文案。更新面板 UI 属**整文件覆写**（不是打补丁），
代价与登记见 `UPSTREAMS.md` 的「覆写登记」。

**测试**：新增 63 项本项目自己的测试（版本语义、tag 挑选、更新服务鉴权与并发保护、
状态文件契约、helper 挂载参数与属主交还、覆写标记、构建期改动预检），
CI 新增一步运行它们。

### 修复（CI 与构建）

- **`latest` 改为只在打 `v*` tag 时移动**（此前每次 main 推送都移动）。
  这是让「一键更新」**能收敛**的必要一半：更新拉的是 `SUITE_IMAGE`（默认
  `:latest`），而面板拿镜像内版本号与仓库最新 git tag 比较 —— 两者必须指同一个
  东西。此前跑 main 构建（`sha-*`）时面板会一直说"最新正式版 v1.2.3，有更新"，
  点更新却只拿到又一个 main 构建，**永远到不了它承诺的版本**。
  现在非发版构建只更新 `main` 与 `sha-*`；想跟随开发构建就把 `SUITE_IMAGE`
  改成 `:main` 或某个 `:sha-xxxxxxx`。
- **修正 build.yml 里一处已经错误的注释**：它说套件版本"不参与面板的版本检测"，
  而新版面板第一段显示的就是套件版本（上游版本是另一条线，在第二、三段）。
- **前端构建改为在打过补丁的树上进行**。原先 CI 构建的是 `vendor/manager/web`
  （**未打补丁**），意味着我们对更新面板的覆写与新 i18n 文案**根本没被编译验证过**
  —— TSX 类型错误会一路漏到 Docker 构建才炸，而那时的报错离原因更远。
- **补上 CI 缺失的测试前提**。`ComposeCommandTest::test_missing_compose_raises_actionable_error`
  预设了"已安装"的目录布局（`data/`），而上游仓库并不跟踪该目录（已核实），
  因此**干净检出上这条测试必然 FileNotFoundError** —— 上游自己的 CI 能过，只是
  因为开发者工作区里恰好有它。CI 现在先 `mkdir -p data`。
- CI 的补丁断言从"反向断言没有补丁标记"改为**正向断言 overlay 已落位**：
  对覆写文件来说，"没有补丁标记"这件事毫无验证价值。

### 变更（大幅瘦身：补丁从 5 项减到 2 项）

同步上游后发现本项目**大部分补丁已被上游官方能力覆盖**，因此做了大幅精简。

**删除的补丁**（原先用于修正「更新」面板的文案与按钮）：

| 原补丁 | 上游现在的官方方案 |
|---|---|
| 改「更新」面板文案 | i18n 重构：文案移入 `web/lib/i18n/locales/*.json` |
| 移除更新按钮 | `can_update_upstream` 能力驱动：不可用时**自动禁用**并提示 |
| 改后端提示语 | `dockerBlocked` / `dockerNote` 等 i18n 键，按能力分派 |
| 关掉上游 commit 更新行 | 无需处理 |

**删除理由**：上游一次 i18n 重构就让 8 个锚点同时失效、构建直接红。
继续锚定硬编码文案意味着**上游每次改字都要跟着改补丁**，
而收益仅仅是措辞更贴切 —— 维护成本远高于收益。

> **教训：不要为"文案更准确"维护构建期补丁。**
> 值得打补丁的是**功能缺失**（如代理支持），不是**措辞差异**。

**保留的补丁**（上游确实没有、且经实测确认）：

| # | 目标 | 内容 |
|---|---|---|
| 1 | manager `config.py` | 内网请求绕过 `WB_HTTP_PROXY`（否则配 SOCKS 后连不上自己的上游） |
| 2 | wb2api `transport.go` | Transport 支持代理环境变量（上游完全不读） |

补丁脚本从 **532 行减到 207 行**（-61%），CI 断言同步精简，
并新增**反向断言**：若有人再给上游前端/更新器打补丁，CI 直接失败。

### 文档

- **修正 README / LICENSE 的项目定位**。此前写作"把两个上游整合为一个镜像"，
  但上游 manager 早已自带 `Dockerfile` 与 `docker-compose.yml` ——
  "能容器化"不再是本项目的价值，该表述既过时也夸大了定位。

  现改为如实描述：本项目是**基于**两个上游构建的**发行版**，
  并在 README 新增「与直接用上游的区别」对照表，列明本项目的实质增量：

  | 本项目 | 上游 |
  |---|---|
  | 出口代理支持（HTTP/SOCKS5 含鉴权） | ❌ 都没有 |
  | 单镜像 + ghcr 预构建（部署机无需 Go/Node） | 各自 `build: .` 本地构建 |
  | socket 代理隔离（只放行所需 API） | 直接挂 `/var/run/docker.sock` |
  | 版本锁定 + 同步机制 + 漂移检测 | 跟随各自分支 |
  | 三容器一键编排 | 各自 compose，需手工组网 |

  同时补上了 **socket 白名单的能力边界**说明：`INFO` 段关闭导致
  `docker info` 失败，因此面板「更新上游」按钮会显示不可用 ——
  这是预期行为（更新走宿主机 `compose pull`），此前 README 未提及。

  > CHANGELOG 里 0.1.0 的历史描述**保持原样**：那是当时的真实认知，
  > 不应该篡改历史记录。

### 同步的上游版本

| 上游 | 分支 | Commit | 时间 |
|---|---|---|---|
| Sliverkiss/workbuddy2api | `master` | `b08f518` | 2026-09-19 16:44 |
| ithtelab/workbuddy-manager | `main` | `8bc9b0d` | 2026-09-19 11:41 |

- wb2api：默认提示词换 GLM5.3 适配版；期间含 WAF 403 软冷却 + 轮转退避、
  global 域并发分档、成长任务链补全（11+ 提交 / +3387 行）
- manager：**v1.0.35 → v1.0.57**（跨 20+ 版本 / 50+ 提交：i18n 重构、
  `can_update_upstream` 能力驱动、Windows 原生部署、DeepSeek 多轮修复等）

### 修复

- **CI 在 `cmd/stats` 测试失败：上游的时区缺陷挡住了构建**。

  失败信息：`main_test.go:551: 标题应含窗口起始时刻，得到 "🪟 网关请求统计 ·
  窗口 1h29m（自 09-14 12:43）"` —— 断言期望 `20:43`。

  根因：上游 `cmd/stats/main.go` 用 `t.Local()` 渲染起始时刻，
  而测试把期望值硬编码成了 **UTC+8 的渲染结果**（测试数据是
  `2026-09-14T20:43:52+08:00`）。于是该测试只在 UTC+8 机器上通过：

  | 环境 | 渲染 | 结果 |
  |---|---|---|
  | GitHub runner（UTC） | `自 09-14 12:43` | 失败 |
  | 国内机器 / 本容器（UTC+8） | `自 09-14 20:43` | 通过 |

  这解释了为什么**本地全绿、CI 却红**。

  修法：CI 的 Go 测试步骤固定 `TZ=Asia/Shanghai`，并确保 runner 有 tzdata
  （Go 在 Linux 上读 `$TZ` 并到 `/usr/share/zoneinfo/<TZ>` 找时区文件）。

  选这个方案而不是跳过该包或改上游测试：
  - 固定时区**与本发行版容器内的时区一致**（Dockerfile 已设 `TZ`），
    CI 验证的环境更贴近实际运行；
  - 跳过该包会掩盖真实回归，改测试则违反 vendor 不改的约定。

  > 影响面已核实：全仓库只有 `cmd/stats` 这一处把 `Local()` 输出写死进断言。
  > 另注：该包**未被镜像编译**（Dockerfile 只构建 6 个二进制，不含 stats），
  > 所以不影响镜像功能，但它住在 `go test ./...` 里，会挡住 CI。

- **补丁 2 锚点随上游重构而失效，fail-fast 正确拦截**。
  上游把 Transport 构造从 `client.go` 的内联字面量抽成了
  `transport.go` 的 `newTransport()`，旧锚点找不到，构建按设计失败并指出
  要改哪里（而非静默失去代理能力）。已更新锚点。

- **验证了一个曾担心的冲突**：上游 `newTransport()` 设了自定义 `DialContext`
  （连接超时加固），而 Go 的 `Transport.Proxy` 直觉上可能被 dialer 绕过。
  用假代理收包**实测证明可共存** —— 设 `Proxy` 后连接代理本身仍走
  `DialContext`，请求确实经代理发出。故补丁只需加一个字段，无需包装 dialer。

#### 历史修复（早前同步时发现）

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
| Sliverkiss/workbuddy2api | `master` | `0adc345` | 2026-09-15 23:37 |
| ithtelab/workbuddy-manager | `main` | `1f5ccef` | 2026-09-16 00:38 |

- wb2api：`feat(models): /v3/config 双域并集探测——v3 为主、企业端点补缺`
  （含模型清单**纯动态化**：删除 CN/global 静态 fallback 表）
- manager：仓库保护规则文档（面板显示版本 **v1.0.35**；
  含 **API 密钥限定版本**——国内版/国际版彻底分开、仪表盘按版本过滤）

### 集成层核对（同步后）

- **补丁 3 的锚点被上游改动拦下（fail-fast 生效）**：上游把更新面板从
  "容器/裸机"文案改为**能力驱动**（`can_update_upstream`，用 `docker info`
  能否跑通判断）。补丁 3 的旧锚点因此失效，**构建按设计失败并指出要改哪里**，
  而不是静默跳过留下误导文案。已按上游新结构更新锚点，并把新增的
  后端提示语纳入补丁（现共 12 个替换点）。
- **能力判定与本发行版实际相符**：manager 经 `docker-socket-proxy` 访问
  docker 且关闭了 `INFO` 段（安全收紧）→ `docker info` 失败 →
  `can_update_upstream=False` → 界面自动禁用「更新上游」并提示宿主机操作。
  方向正确，但上游提示语提到"未挂载 `/var/run/docker.sock`"与实际情况不符
  （我们挂了代理，只是关了 `/info`），已修正为说明真实原因。
- **配置模板无需改**：上游 `config.example.json` 零差异。
- **env 变量集合未变**（21 个）：compose 无需调整。
- **Dockerfile COPY 清单未变**：无需跟改。
- **Go 测试保持全绿**（16/16 包）：上游先前已修掉那 2 个 Windows 时钟问题。

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
