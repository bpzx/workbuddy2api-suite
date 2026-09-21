# 上游来源与同步

本项目是**基于** **workbuddy2api** 与 **workbuddy-manager** 构建的**发行版**，
两个上游的代码以快照形式内置于 `vendor/`，**原样保留、不作修改**；
它们的来源 commit 记录在 [`upstreams.json`](upstreams.json)。

> 注意措辞：本项目**不是**"把两个上游合并成一个镜像"。两个上游各自都自带
> `Dockerfile` 与 `docker-compose.yml`，所以"能容器化"本身不是本项目的价值。
> 本项目提供的是它们没有的那些（出口代理支持、套件一键更新、socket 隔离、
> 版本锁定与同步、四容器编排），详见 README 的「与直接用上游的区别」。

> 为什么用快照而不是 submodule：本项目只做容器化与集成，
> 需要的是「上游某个确定版本」而非「跟随上游变动」。
> 快照让构建自包含、可审计，也让同步变成一条可复核的命令。

## 当前锁定版本

| 上游 | 仓库 | 分支 | Commit | 提交时间 | 快照位置 |
|---|---|---|---|---|---|
| wb2api | [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | `master` | [`d1023f3`](https://github.com/Sliverkiss/workbuddy2api/commit/d1023f37) | 2026-09-21 09:52 | `vendor/wb2api` |
| manager | [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | `main` | [`3fb56bd`](https://github.com/ithtelab/workbuddy-manager/commit/3fb56bd0) | 2026-09-20 19:48 | `vendor/manager` |

- wb2api：`Merge pull request #184 from .../fix/image-url-`（+764/−21，15 个文件）
  含图片 URL 修复、积分口径、成长任务链、WAF 403 处理等
- manager：`docs: issue 回复的写法——与更新日志同一条规矩，配自检脚本与守卫`
  （+6528/−269，55 个文件）**面板显示版本 v1.0.60**；期间跨 v1.0.58 / v1.0.59 /
  v1.0.60 三个发版，含 `/v1/models` 按模型白名单裁剪、账号管理接口开关、
  Responses API custom 工具桥接、token 续期、i18n 补齐（新增一条机械检查
  `test_setting_field_concats_have_translations`）等

> 本次同步顺带修掉一处**配置模板漂移**（此前一直存在，非本次引入）：
> 上游已 BREAKING 移除 `server.max_body_mb`（请求体改为无上限），而我们的
> `docker/wb2api.config.template.json` 仍留着它 —— 生成的 `config.json` 会
> **声称一个不存在的 8MB 限制**（上游容忍旧键，所以不报错、只是说错话）。
> 现已改为**从上游 example 重新生成**模板，并新增
> `tests/test_config_template.py` 按**递归键路径**比对，防止再次漂移。
> 同时补上了模板里缺的 `admin` 段与 `pool` 的 5 个新字段
> （后者因上游对缺字段套默认值而未造成行为问题，但漏着是隐患）。

> 记录的是**分支 + commit**，不是 Release tag：上游的 CHANGELOG 常滞后于
> 代码（manager 打完 v1.0.25 后仍有未发版提交），按 tag 记录会失真。
> 权威值始终以 `upstreams.json` 为准，本表是同步脚本执行后的手工摘录。

### 快照保留范围

- **排除**：`.git`（体积）
- **移除**：上游的 `.gitignore`（**关键，见下方「嵌套 .gitignore 陷阱」**）
- **保留**：`.github`、`LICENSE`、全部测试
  - 保留 `.github` 有两个原因：GitHub **只搜索仓库根目录**的
    `.github/workflows`，嵌套在 `vendor/` 下的工作流不会被触发（无害）；
    而 manager 的 `test_release_signature.py` 会读自己的
    `.github/workflows/release.yml`，排除掉会让上游测试报 FileNotFoundError，
    污染"测试全绿"这个信号。
  - 保留全部测试：上游那 2.3 万行测试是逆向所得协议知识的护栏，
    也是判断"这个上游版本能不能用"的主要依据。

### 嵌套 .gitignore 陷阱（真实事故，务必理解）

上游的 `.gitignore` 用的是 **bare 模式**（`*.md`、`config.json`、`out/`），
这类模式是**递归**的。它原本在「上游仓库根目录」下工作正常，但一旦快照变成
`vendor/wb2api/`，同一个规则就会匹配到**它自己仓库里更深层**的文件：

```
vendor/wb2api/.gitignore 的规则 `*.md`（本意：只留 README）
  └─ 误伤 vendor/wb2api/internal/prompt/defaultprompt.md
       ↑ 这是 prompt.go 里 `//go:embed defaultprompt.md` 的**编译期必需文件**
```

后果很有迷惑性：**本地 `go build` 完全正常**（文件就在磁盘上），
但 CI 从 git 全新 clone 时该文件不存在，直接编译失败：

```
internal/prompt/prompt.go:17:12: pattern defaultprompt.md: no matching files found
```

同类误伤还有 `config.json` 规则命中 `ai-governance/config.json`。

**为什么根 `.gitignore` 的否定规则救不了**：Git 的合并规则是「深层 .gitignore
优先」，浅层写 `!vendor/wb2api/internal/prompt/defaultprompt.md` 无法撤销
深层 `*.md` 的排除。

**因此本项目采取的方案**：

1. `sync-upstreams.sh` 在导出快照时**自动移除** vendor 下所有 `.gitignore`；
2. 保护规则改由**根 `.gitignore` 用锚定路径**（`/auths/`、`/config.json` …）
   显式表达，不用 bare 模式；
3. 同步脚本结尾做**纳出完整性校验**：vendor/ 磁盘上每个文件都必须出现在
   git 索引里，否则报错退出 —— 这是唯一能自动发现此类疏漏的检查。

> 教训：`git add -A` 不报错 ≠ 文件都进了版本库。
> 只有「磁盘文件集 == git 索引文件集」这个对账才能发现被静默忽略的文件。

## 构建期补丁登记

上游代码保持原样入库，但容器化集成需要**四组**上游没有的行为，因此在**构建期**
打补丁（`docker/patches/apply.py`，Dockerfile 的 `vpatch` 阶段）。
（四组共 6 处文本替换：补丁 1 与 3 各含 2 处。）

| # | 目标 | 上游位置 | 补丁内容 | 为什么需要 |
|---|---|---|---|---|
| 1 | manager | `server/config.py` · `http_client()` | 给内部服务（`WB2API_BASE`、`dockerproxy`）挂直连 transport（`mounts`） | httpx 的 `proxy=` 作用于所有请求；配了 SOCKS 后连内网上游 `wb2api:7863` 也会走代理，管理端连不上自己的上游。`no_proxy` 环境变量在显式 `proxy=` 下**不生效**（已实测） |
| 2 | wb2api | `internal/upstream/transport.go` · `newTransport()` | 给 `http.Transport` 设 `Proxy: http.ProxyFromEnvironment` | 上游未设该字段，Go 零值 = **恒不使用代理**且不读环境变量，网关出站无法走代理 |
| 3 | manager | `server/main.py` | 加两行：导入并 `include_router(suite_router)` | 注册本项目新增的 `/api/system/suite-*`（套件自更新）路由。**上游路由实现一行未改** —— 见下面的覆写登记 |
| 4 | manager | `web/components/common/layout/ManagementBar.tsx` | 移除"发现新版本"会话弹窗（整块 `useEffect`） | 那条提示在本发行版**两处都不成立**：① 它显示的版本号是**上游 manager** 的，不是本套件的；② 文案指向「设置 → 系统更新 一键升级」，而该页并不提供升级上游的按钮。留着只会让人误以为点一下就能升级 |

补丁 1、2 都与**出口代理**有关 —— 那是本项目相对上游的实质增量之一；
补丁 3 只是"挂载我们自己的路由"的接线；补丁 4 是"去掉不该出现的行为"。

> **注意区分「改措辞」与「去行为」**：本项目**不做**前者（见下面的教训），
> 但做后者。补丁 4 移除的是一个**实际会弹出来的提示**，不是把某句话换个说法 ——
> 判据是"它是否在误导用户去做一个做不到的操作"。

### 覆写登记（比补丁更重的手段）

`docker/overlay/` 下还有一类改动：**整文件覆写**上游文件。它与补丁的区别很重要：

| | 补丁 | 覆写 |
|---|---|---|
| 做法 | 锚定上游代码，原位替换一小段 | 用我们的文件**整个替换**上游文件 |
| 上游后续改进 | 自动保留（除被替换的那段） | **全部丢弃**，同步时要人工复核 |
| 失效方式 | 上游重构 → 锚点找不到 → 构建失败 | 上游改名/删除文件 → 构建失败 |
| 适用 | 缺一小段功能 | 要改的是**整个结构**，打补丁等于把整段代码换掉 |

**能不覆写就不要覆写。** 当前只有一处：

| 目标 | 上游位置 | 原因 | 代价 |
|---|---|---|---|
| 更新面板 UI | `web/components/common/settings/UpdatePanel.tsx` | 面板从上游的「3 个更新目标（both/upstream/manager）」重构为「本套件 + 2 个上游」三段式，改的是整个渲染结构；打补丁等于把大部分渲染体换掉，且锚点会随上游每次改动失效 | **放弃该文件的上游后续改进**，每次同步上游都要人工看一眼 |

同时 `docker/overlay/` 还含两处**新增**文件（上游没有，不冲突）：
`server/services/suite.py`（版本比对与更新触发）与 `server/routers/suite.py`（三个端点）。
新增与覆写的落位同样由 `apply.py` 负责，且都带 `SUITE-OVERLAY` 标记 ——
CI 会用**正向断言**检查它们确实落位（`build.yml`）。

> 覆写 `UpdatePanel.tsx` 时必须保留 `can_update_upstream` 字样：上游
> `test_docker_deploy.py::test_frontend_uses_capability_flag` 断言本文件要透出
> 该能力标志。新版面板确实在用它（上游区块据此显示"请在宿主机更新"）。

### 曾经打过、现已删除的补丁（重要教训）

早期版本还打了三处补丁去修正「更新」面板的文案与按钮（改文案、删按钮、
改后端提示语），理由是"本发行版是容器部署、更新在宿主机执行"。
**这些补丁已全部删除**，因为：

1. **上游自己做得更好**。v1.0.32+ 引入 **`can_update_upstream` 能力驱动**判定：
   用 `docker info` 能否跑通来判断"能不能操作 docker"，比按"是否在容器里"判断
   更准确。本发行版因 socket 代理关闭 `/info` 端点而表现为"不可用"，
   于是界面会**自动禁用**更新按钮并给出替代做法 —— 正是我们想要的。
2. **维护成本远高于收益**。上游随后做了 i18n 重构，文案移入
   `web/lib/i18n/locales/*.json`。继续锚定硬编码文案意味着
   **上游每次改字都要跟着改补丁**，而收益仅仅是措辞更贴切。
   实测过一次：上游一次改动就让 8 个锚点同时失效，构建直接红。

> **后续变化（勿与上面的历史混淆）**：本发行版后来**整文件覆写**了更新面板，
> 换成「本套件 + 两个上游」三段式，并新增了套件一键更新。因此上面"界面会自动
> 禁用更新按钮"描述的是**当时**的行为 —— 现在上游那套「3 个更新目标」的按钮
> 已经不在我们的面板里了，但 `can_update_upstream` 判定仍被读来显示能力边界
> （上游测试也要求该标志存在）。见上面的「覆写登记」。

> **教训：不要为"文案更准确"维护构建期补丁。**
> 上游一旦重构就会失效，而构建失败比文案不准更烦人。
> 真正值得打补丁的是**功能缺失**（如本项目的代理支持），不是**措辞差异**。

### 补丁 2 的一个技术细节：Proxy 与自定义 DialContext 可共存

上游的 `newTransport()` 设了自定义 `DialContext`（连接超时加固）。直觉上
`Transport.Proxy` 可能被 dialer 绕过，但**实测证明可以共存**：

用假代理收包验证——设了 `Proxy` 后，连接代理本身仍走 `DialContext`，
请求确实经代理发出。因此补丁只需加一个 `Proxy` 字段，无需包装 dialer。

> 验证方法（可复现）：起一个 TCP listener 冒充代理，分别用
> 「只设 Proxy」与「Proxy + DialContext」两个 Transport 发请求，
> 看 listener 是否收到。两者都收到 → 可共存。
### 已知的上游测试时区缺陷（CI 固定 TZ 规避）

上游 `cmd/stats` 的标题渲染用 `t.Local()`（`cmd/stats/main.go`），
而它的测试把期望值**硬编码**成了 UTC+8 的渲染结果：

```go
// cmd/stats/main_test.go
Since: "2026-09-14T20:43:52+08:00"   // 测试数据
if !strings.Contains(frame[0], "自 09-14 20:43")   // 期望：UTC+8 渲染
```

`t.Local()` 取决于**运行机器时区**，因此：

| 环境 | 渲染结果 | 结果 |
|---|---|---|
| GitHub runner（UTC） | `自 09-14 12:43` | **失败** |
| 本发行版容器 / 国内机器（UTC+8） | `自 09-14 20:43` | 通过 |

**应对**：CI 的 Go 测试步骤固定 `TZ=Asia/Shanghai`
（并确保 runner 装了 tzdata —— Go 在 Linux 上读 `$TZ` 并到
`/usr/share/zoneinfo/<TZ>` 找时区文件，见 `zoneinfo_unix.go`）。

选这个方案而非跳过该包或改测试，理由：

* 固定时区与**本发行版容器内的时区一致**（Dockerfile 已设 `TZ=Asia/Shanghai`），
  CI 验证的环境更贴近实际运行环境；
* 不改上游代码（跳过测试包会掩盖真实回归，改测试则违反 vendor 不改约定）。

> 影响面已核实：全仓库只有 `cmd/stats` 这一处把 `Local()` 输出写死进断言；
> 其他时区相关测试（如 `scheduler_test.go`）都用 `time.Local` 做**相对**断言
> （自己构造、自己比较），与机器时区无关。
>
> 另注：`cmd/stats` **未被本发行版的镜像编译**（Dockerfile 只构建 6 个二进制，
> 不含 stats），它是上游给用户本地用的终端统计工具，因此这个测试缺陷不影响
> 镜像功能 —— 但因为它住在 `go test ./...` 里，会挡住 CI 构建。

### 补丁是 fail-fast 的

`apply.py` 是**四类改动共用的一套 fail-fast 校验**：文本补丁、i18n 键合并、
文件新增、文件覆写。全部改动**先整体校验、再统一落盘**，不会出现"改了一半"
的中间状态。任一前提不成立就**让构建失败**并打印要改哪里：

| 改动类型 | 校验的前提 | 失败意味着 |
|---|---|---|
| 文本补丁 | 锚点存在、只出现一次、尚未打过 | 上游重构了那段代码 |
| 文件覆写 | 目标**已存在**（上游没改名/删除）、尚未被我们覆写 | 上游移动或删除了该文件 |
| 文件新增 | 目标**不存在** | 上游自己实现了同一件事，应改用上游的 |
| i18n 键合并 | 语系集合与上游一致、我们各语系键集/占位符一致、**上游未占用同名键** | 上游新增语言，或上游自行加了同名文案 |

最后一条值得强调：如果上游自己定义了 `suiteUpdate.*` 的某个键，构建会失败而
**不是静默覆盖上游文案** —— 那会让人误以为译文是自己的，实际是上游的语义。

这比静默跳过安全得多：后者会让镜像"能构建、能启动"，但改动意图悄悄失效，
极难排查。因此上游若重构了这些位置，**构建会红**，这是预期行为，
按提示更新锚点或复核后删除对应条目即可。

### 同步上游时的注意事项

- 同步脚本不会改动 `docker/patches/apply.py` 与 `docker/overlay/`，
  补丁与覆写都不随上游更新。
- `vendor/` 快照始终是上游原样，补丁只在构建期施加到镜像内。
  因此 `git diff upstreams.json 锁定的 commit` 永远是**零差异**。
- 若上游自行实现了其中某项能力，补丁会报「似乎已打过补丁」——
  确认后从 `apply.py` 删掉对应条目即可。
- **覆写过的文件要人工复核**：`UpdatePanel.tsx` 我们已经整文件替换，
  上游对这个文件的改进**不会自动流进来**。同步上游后建议看一眼
  `git log -p <old>..<new> -- web/components/common/settings/UpdatePanel.tsx`，
  判断有没有值得搬过来的改动。

---

## 来源信息的四种落地形式

任何场景都能追溯到下游代码的确切来源：

1. **`upstreams.json`** — 机器可读的权威记录，`sync-upstreams.sh` 读写它
2. **`UPSTREAMS.md`（本文件）** — 人读表格与同步步骤
3. **镜像标签** — 镜像自带血统，`docker inspect` 即可查：
   ```
   io.workbuddy2api-suite.upstream.wb2api.repo
   io.workbuddy2api-suite.upstream.wb2api.commit
   io.workbuddy2api-suite.upstream.manager.repo
   io.workbuddy2api-suite.upstream.manager.commit
   ```
4. **镜像内 `/opt/suite/upstreams.json`** — 运行中的容器也能自证来源

## 上游更新后如何同步

```bash
# 1) 先看差异，不落盘
./scripts/sync-upstreams.sh --dry-run

# 2) 确认无误后同步（可只同步其中一个）
./scripts/sync-upstreams.sh                     # 两个都同步
./scripts/sync-upstreams.sh --only wb2api
./scripts/sync-upstreams.sh --only manager

# 3) 重跑测试（脚本会提示）
cd vendor/wb2api && go build ./... && go vet ./... && go test ./...
cd vendor/manager && python3 -m unittest discover -s server/tests -t .
```

脚本会：`git fetch` 目标分支 → 打印新旧 commit 之间的**提交列表与 diff 统计**
→ `rsync` 覆盖 `vendor/<name>/`（排除 `.git`、`.github`）→ 回写
`upstreams.json` 的新 commit 与时间 → 提示重跑测试与提交。

### 同步后的检查清单

- [ ] 同步脚本末尾的**纳出完整性校验**通过（它会自动检查，不必手动）
- [ ] 三关测试全绿（Go / Python / 前端构建）
- [ ] **在纯净副本上再编译一次**（本地测试通过 ≠ CI 能过）。这一步专门防
      「文件被 .gitignore 静默排除」——本地文件在磁盘上所以不报错，CI 才暴露：
      ```bash
      rm -rf /tmp/citest && mkdir -p /tmp/citest
      git checkout-index -a -f --prefix=/tmp/citest/
      (cd /tmp/citest/vendor/wb2api && go build ./... && go vet ./...)
      ```
- [ ] 若上游改了 Dockerfile 依赖（新增需要拷贝的文件、新增系统包），
      同步 `docker/Dockerfile` 的对应 `COPY`/`RUN`
- [ ] 若上游改了 `config.example.json` 的字段，同步
      `docker/wb2api.config.template.json`
- [ ] 若上游改了 manager 的 `/api/system/*` 或 `deploy/update.py` 的调用约定，
      复核 `docker/stub/update.py` 是否仍兼容
- [ ] 更新套件 `CHANGELOG.md`，注明本次捆绑的上游 commit

## 漂移检测

`.github/workflows/upstream-check.yml` 每天比对 `upstreams.json` 与上游仓库的
最新 commit，发现漂移时自动开 issue（同一天重复检测只更新不重复开）。

## 许可

两个上游均为 MIT，`vendor/*/LICENSE` 已随快照保留，本项目`LICENSE` 一并声明。
