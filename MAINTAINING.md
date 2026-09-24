# 维护者手册

**本文件的读者是本项目的维护者。** 部署与使用请看 [`README.md`](README.md) ——
那里只写部署方需要的内容。把维护者流程写在 README 上，会让部署方误以为
"我是不是该做点什么"，这一点已经吃过两次教训（面板里的上游更新指引、README 的
发版说明，均已移除）。

| 想知道什么 | 看哪 |
|---|---|
| 怎么部署、怎么用、有哪些限制 | [`README.md`](README.md) |
| 上游**血统**：锁定了哪些 commit、打了哪些补丁 / 覆写了哪些文件、同步时的坑 | [`UPSTREAMS.md`](UPSTREAMS.md) |
| 每个版本**捆绑了哪些上游 commit**、改了什么 | [`CHANGELOG.md`](CHANGELOG.md) |
| 维护者流程（发版、同步、构建、自检） | 本文件 |

---

## 发版：打 tag

```bash
# 1) 先把要发版的提交推到 main，并确认 CI 通过
git push origin main

# 2) 打带注释的 tag（-a 会记录打的人与时间，便于追溯）
git tag -a v0.2.5 -m "v0.2.5"

# 3) 推送 tag —— **这一步才触发发版构建**
git push origin v0.2.5
```

推送后 CI 构建并推送四个标签：`latest`、`v0.2.5`、`0.2.5`、`sha-xxxxxxx`，
并把 `WB_SUITE_VERSION=v0.2.5` 写进镜像（OCI 标签 + 环境变量 + `/opt/suite/version`）
—— 面板「系统更新」第一段「本套件」显示的当前版本就是它。再打 `v0.2.6` 时
`latest` 会跟着移动，已部署的实例打开面板就会看到「v0.2.5 → v0.2.6，有更新」
并可一键升级。

### tag 格式要求：以 `v` 开头、分段全是数字

| tag | 结果 |
|---|---|
| `v1.2.3` | ✅ 正式版，触发 CI，参与面板的版本比较 |
| `v1.2` | ✅ 同上（按缺位补 0 比较，等价于 `v1.2.0`） |
| `v1.2.3-rc1` | ❌ 会触发 CI，但面板当成**开发构建**（`parse_semver` 只认 `^v?\d+(\.\d+)*$`） |
| `release-1.2.3` | ❌ **不触发 CI**（触发条件是 `v*`） |
| `1.2.3` | ❌ 不触发 CI（少了 `v`） |

> 这条例外只在两处实现，别在别处再写第三遍：CI 的触发条件在
> `.github/workflows/build.yml` 顶部，解析规则在
> `docker/overlay/server/services/suite.py` 的 `parse_semver()`。

### 打错了要撤

比如 tag 打在了错误的提交上。删掉远端与本地 tag 即可，但**注意 `latest`
已经被指过去了** —— 撤销后需要重新推一个正确的 tag 让它回正：

```bash
git push --delete origin v0.2.5 && git tag -d v0.2.5
```

### 为什么 `latest` 只在打 tag 时移动

「一键更新」拉的是 `SUITE_IMAGE`（默认 `:latest`），而面板拿**镜像内的版本号**
与仓库里最新的 `vX.Y.Z` git tag 比较。两者必须指同一个东西才能收敛：

- 若 `latest` 每次 main 推送都移动 → 跑 main 构建（`sha-xxx`）时面板会说
  "最新正式版 v1.2.3，有更新"，点更新却只拿到又一个 main 构建，**永远到不了
  它承诺的那个版本**；
- 现在非发版构建只更新 `main` 与 `sha-*`，于是 tag → `latest` → 面板提示 →
  点更新 → 版本号对齐 → 面板显示"已是最新"，闭环成立。

想跟随 main 的部署方改用 `:main` 或某个 `:sha-xxxxxxx`（见 `.env.example`）。

---

## 同步上游

`vendor/` 下的两个上游是**固定快照**，不会自动跟随 —— 这是"可追溯"与"自动跟随"
之间的必然取舍。

> ⚠️ **wb2api 的上游已删除**（`Sliverkiss/workbuddy2api` 现为 404，账号仍在）。
> 它**没有可同步的目标了**：`--only wb2api` 会 fetch 失败，每日漂移检测对它只记
> 一行"查询失败"（fail-soft，不开 issue 也不失败）。**该部分自此由本项目自行维护**
> —— 改它走 `docker/patches/apply.py`（见 [UPSTREAMS.md](UPSTREAMS.md) 的
> 「构建期补丁登记」，补丁 5~7 就是上游没有、我们自己加的风控加固）。
> 本地镜像缓存 `.upstream-cache/wb2api.git` 保留到 `f2ccc7b` 的历史可备查阅。

**manager 仍有上游可同步**，流程如下：

```bash
git pull
./scripts/sync-upstreams.sh --dry-run    # 只看差异：提交列表 + diff 统计
./scripts/sync-upstreams.sh              # 确认后同步（--only <名字> 只同步一个）
```

脚本会：fetch 上游 → 打印新旧 commit 之间的提交与 diff → 导出快照到
`vendor/<name>/`（自动移除上游 `.gitignore`）→ 回写 `upstreams.json` → 校验快照
是否已全部纳入 git。

### 同步后的核对清单

**清单与逐条理由在 [`UPSTREAMS.md`](UPSTREAMS.md) 的「同步后的检查清单」** ——
那里是集成改动（补丁 / 覆写 / 配置模板 / stub 契约）的登记处，清单条目与它们
一一对应，放在一起才不会漏。这里只列最容易忘的三点：

1. **跑构建期改动预检**：`python -m unittest discover -s tests -t tests` 里的
   `test_patch_preflight.py` 在**原始 vendor** 上验证全部补丁锚点、覆写目标与
   i18n 前提。上游一旦重构，这里立刻变红 —— 比等 CI 或 Docker 构建快得多。
2. **看一眼被覆写过的文件**：`UpdatePanel.tsx` 是整文件替换，上游对它的改进
   **不会自动流进来**。用 `git log -p <旧>..<新> -- <路径>` 判断有无值得搬的改动。
3. **对比配置模板**：`tests/test_config_template.py` 按递归键路径比对模板与上游
   `config.example.json`，上游增删配置项时它变红 —— 那时更新模板。

漂移检测：`.github/workflows/upstream-check.yml` 每日比对 `upstreams.json` 与
上游 HEAD，发现新提交会开/更新 issue（不需要就直接删掉那个 workflow）。

---

## 账号风控维护（定期）

网关对外**声称自己是官方桌面客户端**，因此以下几项会随时间和上游客户端版本
漂移，需要定期复核。背景与操作细节见 README「账号风控相关」与
[`UPSTREAMS.md`](UPSTREAMS.md) 的补丁 5~7。

- [ ] **新账号加入后确认设备风控凭据**：`docker compose logs wb2api | grep device_token`
  —— 启动时会提示「N/总数 个账号缺少 device_token」。内置登录流程**不写**该字段
  （只有插件 OAuth 的产物带），缺了会静默降级风控形态。
- [ ] **客户端版本号是否过期**：`upstream.client_version` / `cli_version`（模板里已
  显式写出契约版本，设置页「出站标识」可改）。官方客户端升级后，旧版本号本身
  可能成为"老客户端"特征。
- [ ] **设备标识是否需要轮换**：`WB2A_DEVICE_ID_SALT`。**只在确有必要时**用 ——
  换盐会让所有账号的设备标识**同时**变化，本身就可能成为异常信号。
- [ ] **盯公开派生仓库有没有值得借鉴的修复**：wb2api 原仓库已删除，但搜索可见
  若干同构的公开派生（例如 `linguo2625469/workbuddy2api-panel`）。它们是**被改造
  过的分支**，可作"参考实现"借鉴修复；**不要**把 `upstreams.json` 指过去 ——
  那会让本项目记录的血统失真。

### 未决项（已识别，尚未处理）

| 项 | 现状 | 为什么没动 |
|---|---|---|
| 合成任务**载荷**同构 | 所有账号发同样的 5 条 `chat_request_send`（同模型 / 同长度 / 同 agent）。本套件只给**间隔**加了抖动（补丁 7） | 让载荷按账号变化等于**编造不同的事件数据**；收益（降低聚类特征）与风险（与平台预期不符）需要判断，不宜盲改 |
| `cmd/login` 的 UA 与网关不一致 | 登录工具发 `CLI/2.63.2 CodeBuddy/2.63.2`，而网关运行时是 `WorkBuddy/5.5.4 … CLI/2.137.1` | 改动它需要先确认**登录端点仍接受**新 UA —— 盲改可能直接**弄坏加号流程**，属需单独验证的项 |
| `max_in_flight_global` 注释与实现不符 | 三处注释写「0 = 回落 max_in_flight」，而 `normalize()` 实际把 `0`/负数归一为 `2` | 行为是**有意**的（国际版 WAF 更紧，恒分档）。改源码注释属于改上游文案；已在 README 说明「以 2 为准」，不再动它 |

## 构建与自检

镜像由 GitHub Actions 构建并推送到 ghcr.io（`.github/workflows/build.yml`），
本地开发机**不需要** Go / Node 工具链来部署。

CI 的 `verify` 作业在构建前跑三道测试 + 补丁落位断言：

```bash
# Go（注意固定 TZ：上游 cmd/stats 的测试把期望值硬编码成了 UTC+8）
cd vendor/wb2api && TZ=Asia/Shanghai go build ./... && TZ=Asia/Shanghai go vet ./... && TZ=Asia/Shanghai go test ./...

# Python（上游全套；data/ 是某个测试预设的"已安装"布局，需先建好）
cd vendor/manager && mkdir -p data && python -m unittest discover -s server/tests -t .

# 前端静态导出
cd vendor/manager/web && npm ci && npm run build:export
```

**本项目自己的测试**（版本语义、更新服务鉴权、状态契约、构建期改动预检、
配置模板与增量补齐等）：

```bash
python -m unittest discover -s tests -t tests
```

> 其中 `test_suite_service.py` 与 `test_suite_updater.py` 里的 `server.*`、
> `docker/overlay/*` 导入的是**打过补丁的树**，因此完整跑法要先把补丁应用到一份
> 副本再设 `PYTHONPATH`：
>
> ```bash
> cp -r vendor/wb2api vendor/manager /tmp/patched/
> python docker/patches/apply.py /tmp/patched
> PYTHONPATH=/tmp/patched/manager python -m unittest discover -s tests -t tests
> ```
>
> 只读原始 vendor 的那部分（`test_patch_preflight.py`、`test_config_template.py`）
> 不受影响，可直接在仓库根跑。

CI 里 `test_docker_deploy::test_missing_compose_raises_actionable_error` 需要
先 `mkdir -p data`，原因是它预设了"已安装"的目录布局而上游仓库并不跟踪 `data/`
（已核实）—— 干净检出上该测试必然失败，上游自己的 CI 能过只是开发机工作区里
恰好有它。

---

## 本地构建镜像（可选）

部署用 ghcr 预构建镜像即可；真要在本地验证 Dockerfile 时：

```bash
docker build -f docker/Dockerfile -t workbuddy2api-suite:local .
```

构建期会装 docker compose 插件（固定 v2 版本，v5 的 `up --build` 需要外部
buildx 且无回退分支）、写入套件版本、执行 `apply.py` 落位全部集成改动。
`apply.py` 带 fail-fast：锚点找不到、上游占用了我们的 i18n 键、上游删掉了被覆写
的文件，**构建都会失败并指出原因** —— 这是刻意的，静默失效比构建失败难查得多。
