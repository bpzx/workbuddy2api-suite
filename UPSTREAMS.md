# 上游来源与同步

本项目是 **workbuddy2api** 与 **workbuddy-manager** 的容器化整合发行版。
两个上游的代码以快照形式内置于 `vendor/`，**原样保留、不作修改**；
它们的来源 commit 记录在 [`upstreams.json`](upstreams.json)。

> 为什么用快照而不是 submodule：本项目只做容器化与集成，
> 需要的是「上游某个确定版本」而非「跟随上游变动」。
> 快照让构建自包含、可审计，也让同步变成一条可复核的命令。

## 当前锁定版本

| 上游 | 仓库 | 分支 | Commit | 提交时间 | 快照位置 |
|---|---|---|---|---|---|
| wb2api | [Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api) | `master` | [`39f3c3f`](https://github.com/Sliverkiss/workbuddy2api/commit/39f3c3fbd485c9a5f15b05daf81c3c67141632d4) | 2026-09-15 14:17 | `vendor/wb2api` |
| manager | [ithtelab/workbuddy-manager](https://github.com/ithtelab/workbuddy-manager) | `main` | [`3ed6c48`](https://github.com/ithtelab/workbuddy-manager/commit/3ed6c4894fceca017a1d7e3631c94c0e4453dc22) | 2026-09-15 13:47 | `vendor/manager` |

- wb2api：`fix(scheduler): 去串行化 + sleep 换 select-ctx 可取消`
  （本次含 20+ 提交：熔断/模型冷却持久化、429 冷却对齐上游重置时间、
  tool_call 配对清理与截断检测、推理档位透出、跨平台路径修复）
- manager：`improve(update): deploy/ 差异提示说清「要不要紧」`
  （面板显示版本 **v1.0.31**）

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

上游代码保持原样入库，但容器化集成需要三处上游没有的行为，因此在**构建期**
打补丁（`docker/patches/apply.py`，Dockerfile 的 `vpatch` 阶段）。

| # | 目标 | 上游位置 | 补丁内容 | 为什么需要 |
|---|---|---|---|---|
| 1 | manager | `server/config.py` · `http_client()` | 给内部服务（`WB2API_BASE`、`dockerproxy`）挂直连 transport（`mounts`） | httpx 的 `proxy=` 作用于所有请求；配了 SOCKS 后连内网上游 `wb2api:7863` 也会走代理，管理端连不上自己的上游。`no_proxy` 环境变量在显式 `proxy=` 下**不生效**（已实测） |
| 2 | wb2api | `internal/upstream/client.go` · `New()` | 给 `http.Transport` 设 `Proxy: http.ProxyFromEnvironment` | 上游未设该字段，Go 零值 = **恒不使用代理**且不读环境变量，网关出站无法走代理 |
| 3 | manager 前端 | `web/components/common/settings/UpdatePanel.tsx` | 「更新」面板文案改为容器部署的真实操作 | 上游文案描述的是**裸机部署**（git + systemd + Release 包）。本项目在构建期已把 `deploy/update.py` 换成替身（不执行实际更新），文案若不改，用户会以为点按钮就能更新——实际什么都发生不了 |

补丁 3 覆盖的文案：顶部提示行、"一键更新"面板标题与说明、三个按钮的 hint、
确认弹窗措辞、"固定上游版本"整块（容器内无 git，该功能不可用）。

### 补丁是 fail-fast 的

补丁脚本先断言「锚点文本存在且只出现一次」，不满足就**让构建失败**，
并打印要改哪里。这比静默跳过更安全——后者会让镜像"能构建、能启动"，
但补丁意图悄悄失效，极难排查。

因此上游若重构了这几处，**构建会红**，这是预期行为。按提示更新锚点文本即可。

### 同步上游时的注意事项

- 同步脚本不会改动 `docker/patches/apply.py`，补丁不随上游更新。
- `vendor/` 快照始终是上游原样，补丁只在构建期施加到镜像内。
  因此 `git diff upstreams.json 锁定的 commit` 永远是**零差异**。
- 若上游自行实现了其中某项能力，补丁会报「似乎已打过补丁」——
  确认后从 `apply.py` 删掉对应条目即可。

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
