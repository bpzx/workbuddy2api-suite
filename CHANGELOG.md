# 更新日志

本文件记录 **workbuddy2api-suite（套件自身）** 的变更。
每个版本都会注明它捆绑的上游 commit —— 下游问题若要溯源到上游行为，
看这里就能定位到确切版本。

上游自身的更新日志见 `vendor/manager/CHANGELOG.md` 与
`vendor/wb2api/README.md`。

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

**已发布的版本按 git tag 分节**，每节开头注明该版本的提交范围与**发布时**的上游
锁定值 —— 这样"某个行为是从哪一版开始的""哪一版捆绑了哪个上游 commit"都能直接
查到。维护者流程（发版打 tag、同步上游、构建自检）见
[`MAINTAINING.md`](MAINTAINING.md)。

---

## [未发布]

### 变更：`.dockerignore` 裁掉根目录的非构建输入，并加机械守卫

根目录的说明文档与开发辅助目录都不是构建输入，现已排除：
`README.md` / `MAINTAINING.md` / `UPSTREAMS.md` / `CHANGELOG.md` / `LICENSE` /
`.gitignore` / `.dockerignore` / `.env.example` / `docker-compose.yml` /
`.github` / `scripts` / `tests`（Dockerfile 从根目录**只取 `upstreams.json`**）。

**收益要说准：体积上几乎可忽略**（这些合计约 276 KB，而 `vendor/` 就要 11 MB，
约 2%），所以这不是为了省空间。真正的理由是**把契约显式化** ——
将来有人写 `COPY README.md` 时构建会立刻失败，而不是把文档悄悄打进镜像。
此前没做这条是因为怕踩裸模式的坑（见下），而那个坑现在有了机械防护。

- 全部用**锚定模式**（`/README.md`、`/tests`）。裸模式会匹配任意层级：裸
  `LICENSE` 会连 `vendor/manager/LICENSE` 一起排除，而它是 Dockerfile 明确 COPY
  的 —— 报错只会说"文件不存在"，不会提到 `.dockerignore`。
  典型对照：`/.github` 只排除仓库根的 CI 配置，`vendor/manager/.github` 必须
  保留（上游测试要读自己的 `release.yml`），锚定前缀让这两者互不影响。
- 新增 `tests/test_dockerignore.py`（10 项）：解析 Dockerfile 里**来自构建上下文**
  的 COPY 源（跳过 `--from=`），逐个断言没有被排除，并点名
  `vendor/manager/LICENSE`、`vendor/wb2api/go.sum`、`internal/prompt/defaultprompt.md`
  （`go:embed` 必需）等关键文件；反向也守住 `data/`、`.env`、`node_modules`
  必须在排除之列。
- 该测试自带**正反例校验匹配器**（裸 `LICENSE` 必须被判为"会误伤"），否则匹配器
  写错时断言会在错误基础上通过。已做反证：注入裸 `LICENSE` 后两项断言立刻报出
  `vendor/manager/LICENSE`。

### 修复：设置页显示字面量 `undefined`（配置漂移）

上游设置页的「成本档位探索周期」输入框显示**字面量 `undefined`** 并带红框。

- **直接成因在上游代码的一处不一致**：`durationOk()` 用 `String(raw ?? '')`
  把 `undefined` 宽容成空串并通过判定（该字段配了 `offWhenZero`），
  但 `pickValues()` 随后把**原始** `raw` 字符串化 —— `String(undefined)`
  就是 `"undefined"`，于是它被当值填进了输入框。
- **触发条件是我们的问题**：`/data/config.json` 只在**首次启动**生成，之后
  永不更新，所以上游新增 `pool.cost_explore_interval` 后，老部署的配置里没有
  这个键，就走上了上面那条路径。上一轮我只修了**模板**（仅对新部署生效），
  没有处理**已存在的配置**。
- **修法**：入口脚本每次启动做一次**只补不改**的增量补齐
  （`docker/overlay/config_merge.py`）：只添加模板里有、`config.json` 里没有的
  键；已存在的值一律保留（哪怕用户改过或类型不同）；`api_key` 与我们的内部
  `_comment` 不参与；补了哪些键打进日志、写前备份为 `config.json.bak`；
  写盘失败（只读挂载）只告警、不让容器起不来。
- **测试**：新增 14 项（`tests/test_config_merge.py`），含**复现场景**
  （旧配置 + 新模板 → 缺失键被补上、用户值/密钥/历史旧键均不被改动）。

> 为什么不做成"每次启动重新生成配置"：那会覆盖用户在设置页里写下的配置，
> 比"缺一个键"危险得多。所以只补缺失的键，并且是可审计的。


### 文档：README 只留部署方需要的内容，维护者流程移入 MAINTAINING.md

删掉 README 里「怎么打 tag / 发版流程」整节，并把「同步上游更新」一节移出。

理由：**打 tag 与同步上游都是维护者的事，而 README 的读者是部署方** ——
把维护者的流程写给部署方看，只会让人误以为"我是不是该做点什么"。
这条判断已被验证过两次：面板里的「上游更新需在宿主机执行」指引、README 的
发版说明，都已移除。

- 新增 [`MAINTAINING.md`](MAINTAINING.md) 作为维护者手册：发版打 tag（含格式
  要求与撤销方法）、上游同步流程与同步后的核对清单、构建与自检命令、文档分工。
- 部署方需要的内容留在原位：镜像标签语义（`latest` / `main` / `sha-xxxxxxx`、
  如何钉住版本）在 `.env.example` 的 `SUITE_IMAGE` 说明里；**tag 格式约束**
  （必须 `v` 开头且分段纯数字）在 `build.yml` 推导版本号那一步与 MAINTAINING.md
  里各有一处、互相指明，不再散落。
- 「版本语义」一节改写为「正式版 vs 开发构建」，只讲**用户在面板上会看到什么**，
  不再讲 CI 的 tag 机制。
- **本 CHANGELOG 重组为按 tag 分节**（见下）：此前全文只有一个 `[未发布]`，
  已发布的 v0.2.1~v0.2.4 全混在其中，读者无法分辨哪些内容已发布。

---

## [0.2.4] - 2026-09-21

本版提交：`87cb164`（fix(panel)：修正上游 commit 比较假阳性；只留「检测更新」按钮）。

### 修复：上游网关「有新版本」的假阳性

面板同时显示「固定提交 `d1023f3` / 远端最新 `d1023f37`」并标「有新版本可更新」
—— 而那是**同一个提交**，只是缩写长度不同（7 位 vs 8 位）。

- **根因**：`upstreams.json` 的 `commit_short` 是 **7 位**（同步脚本写的），
  而查远端时把 GitHub 返回的 sha 截成了 **8 位**（硬编码 `[:8]`），
  随后用**全等**比较判"是否有更新" → 同一个提交被判成不同。
- **修法**：新增 `same_commit()` 改用**前缀比较**（同一提交的任意长度前缀
  必然互为前缀），并把两侧缩写统一到同一个长度常量 `SHORT_LEN = 7`。
  上游 `updater.py` 的判据本来就是前缀比较
  （`u_latest.startswith(local_head) or local_head.startswith(u_latest)`）——
  我们读过那段代码却没有照着做，这是本次 bug 的直接教训。
- **测试**：新增 4 项 `same_commit` 单测 + 1 项端到端复现
  （`固定 7 位 / 远端 8 位 / 同一提交` → 必须判为无更新）。
  已做**反证**：把实现退化回全等比较，这两个测试立刻变红 —— 确认它们不是空转。

### 界面调整：删掉「刷新」按钮，只留「检测更新」

  * **删掉「刷新」按钮，只留「检测更新」**。两个按钮外观几乎相同（同一个刷新
    图标），用户无法从界面上分辨差别，而作用重叠：
    「检测更新」走 `force=true` **绕过服务端 6 小时缓存**去查 GitHub 并弹提示
    给出结果 —— 这是不可替代的；「刷新」只是重新拉状态，而**版本号即便点了
    刷新也走缓存、数字不会变**，状态本身又由心跳自动刷新（空闲 20s、更新中 2s、
    切回标签页立即刷一次）。它唯一的作用是"不等那最多 20 秒"，不值得占一个
    和另一个按钮长得一样的位子。

---

## [0.2.3] - 2026-09-21

本版提交：`87c8384`（chore(upstream)：同步至 wb2api `d1023f3` / manager `3fb56bd`；修复配置模板漂移）。

### 同步的上游版本

| 上游 | 分支 | Commit | 时间 |
|---|---|---|---|
| Sliverkiss/workbuddy2api | `master` | `d1023f3` | 2026-09-21 09:52 |
| ithtelab/workbuddy-manager | `main` | `3fb56bd` | 2026-09-20 19:48 |

- wb2api：`b08f518 → d1023f3`（15 文件 / +764 −21）：图片 URL 拼接修复、
  积分口径、成长任务链、WAF 403 处理、pass-through 字段扩充。
- manager：`8bc9b0d → 3fb56bd`（55 文件 / +6528 −269），**v1.0.57 → v1.0.60**：
  `/v1/models` 也按模型白名单裁剪（白名单填错当场提示）、账号管理接口开关
  （让「临时停用」能走上游状态位）、Responses API 展开 namespace 子工具并
  保留 custom 工具调用、token 续期、时间显示、限流可见性等六个 issue 修复；
  i18n 补齐并新增一条机械检查。

**本次同步顺带修掉一处存在已久的配置模板漂移**：

- 上游已 **BREAKING 移除** `server.max_body_mb`（请求体改为无上限，见
  `cmd/server/main.go` 注释与 `TestMaxBodyLegacyKeyIgnored`），而我们的
  `docker/wb2api.config.template.json` 里还留着 `8` —— 生成的 `config.json`
  会**声称一个不存在的 8MB 限制**。上游为兼容旧配置容忍该键，所以它不报错，
  只是**静默地说错话**。
- 模板还缺上游的 `admin` 段与 `pool` 的 5 个新字段（`max_in_flight_global`、
  `degrade_threshold`、`degrade_cooldown`、`degrade_cooldown_max`、
  `cost_explore_interval`）。后者当时没造成行为问题，是因为上游对缺字段会套
  默认值（`cmd/server/config.go` 的 normalize）——**碰巧安全**，不是设计安全。

处理方式是**不再人工维护这份键列表**：模板改为从上游 `config.example.json`
生成，只覆盖 `api_key` 与两个数据卷路径；并新增
`tests/test_config_template.py`（5 项）按**递归键路径**比对模板与上游 example，
新增/删除/改名/挪层级都会让测试变红。

> 上一轮的锁定值（属更早的版本，不属本条）：wb2api `b08f518`、
> manager `8bc9b0d`（v1.0.57）。完整的锁定历史见
> `git log --oneline -- upstreams.json`，权威值始终是 `upstreams.json`。


---

## [0.2.2] - 2026-09-20

本版提交：`5d9043d`（fix(ui)：移除误导性顶部弹窗；界面文案去行话去重复）。

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
  * **删掉「上游更新需在宿主机执行：① … ② …」整行**，理由有两条：
    ① 读者不匹配 —— 本套件由维护者发布并锁定上游快照，部署方（使用者）
    无法也不应自行同步上游，把维护者的流程写在界面上只会让人以为
    "我是不是该做点什么"；② 这两块本来就没有按钮，不需要解释"为什么不提供"。
    同步流程保留在 README「同步上游更新」。
    连带**不再读取**上游的 `can_update_upstream` 能力标志（本面板没有"更新上游"
    按钮，要防的"点到做不到的操作"不存在），少一次按轮询周期发生的请求。
    该标志只以注释形式留在面板文件里并说明这一决定 —— 上游
    `test_docker_deploy::test_frontend_uses_capability_flag` 按字面量要求它存在；
    我们的测试同时守住"别把它 wire 回组件状态"。
  * 没有日志时**整块隐藏**更新日志卡片 —— 首次部署满屏的"暂无日志"只是噪音。
  * 文案键从 30 个精简到 27 个，删掉三个没人引用的死键。

- **新增构建期改动的预检测试**（`tests/test_patch_preflight.py`，10 项）：
  在**原始 `vendor/`** 上验证全部前提（锚点唯一存在、覆写目标仍在、新增目标未冲突、
  i18n 语系齐全），并断言补丁的**意图** —— 例如"替换后那条 toast 必须消失"，
  而不只是"锚点能匹配"（后者在替换内容写错时照样通过）。
  好处：本地毫秒级就能发现"上游重构导致锚点失效"，不必等构建或 CI。


---

## [0.2.1] - 2026-09-20

**首个正式 tag。** 包含项目从初始版本到本 tag 的全部内容（提交 `20c01fe` … `43ca937`）。
本 tag 时的上游锁定值为 wb2api `b08f518` / manager `8bc9b0d`（v1.0.57）。

> 下面「首个版本（项目初始集成）」一节记录的是**项目刚集成完成时**的状态，
> 其上游版本表是**那时**的锁定值（`0adc345` / `1f5ccef`），与发布时的锁定值不同 ——
> 两者都保留，便于看清演进。

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


### 修复（更早：CI 时区缺陷等）

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


### 新增（更早：出口代理支持等）

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

### 首个版本（项目初始集成）

首个版本：把 workbuddy2api 与 workbuddy-manager 整合为单一镜像、compose 部署。

#### 捆绑的上游版本

| 上游 | 分支 | Commit | 提交时间 |
|---|---|---|---|
| Sliverkiss/workbuddy2api | `master` | `0adc345` | 2026-09-15 23:37 |
| ithtelab/workbuddy-manager | `main` | `1f5ccef` | 2026-09-16 00:38 |

- wb2api：`feat(models): /v3/config 双域并集探测——v3 为主、企业端点补缺`
  （含模型清单**纯动态化**：删除 CN/global 静态 fallback 表）
- manager：仓库保护规则文档（面板显示版本 **v1.0.35**；
  含 **API 密钥限定版本**——国内版/国际版彻底分开、仪表盘按版本过滤）

#### 集成层核对（同步后）

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

#### 新增

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

#### 变更（相对直接使用上游的裸机部署）

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

#### 已知限制

- 上游更新需人工执行 `scripts/sync-upstreams.sh`（这是"可追溯同步"的必然代价）。
- 单镜像体积偏大（含 Go 二进制 + Python 运行时 + 前端产物）。
- UI 中「上游本地版本」显示为未知：容器内没有上游 git 仓库；
  套件版本与上游 commit 以 `upstreams.json` 与镜像标签为准。
- 在 Windows 上跑 Go 测试会有 2 个 `rate_limited_models` 用例失败：
  `CooldownSoftForModel` 内两次 `time.Now()` 的差值在 Windows 约 0.5ms 时钟
  粒度下相等，导致 `ResetAt` 被 `omitempty` 省略。Linux（含 CI）上通过。
