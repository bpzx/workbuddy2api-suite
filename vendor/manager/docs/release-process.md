# 发布流程（维护者手册）

上游 `workbuddy2api` 改动频繁，且**多数改动不会让管理端报错**——只会让某处
行为悄悄失配（参数位置、请求头形态、默认值翻转、错误码语义）。所以发版前
必须先看上游，而不是直接发。

## 发版前：先查上游

```bash
cd /tmp && rm -rf upcheck
git clone --quiet --filter=blob:none --no-checkout \
  https://github.com/Sliverkiss/workbuddy2api.git upcheck
cd upcheck
git log --oneline -5                       # 当前最新是什么
git log --oneline <上次适配到的提交>..HEAD   # 有没有新提交
```

- **没有新提交** → 直接发版
- **有新提交** → 逐个读，判断是否影响管理端；需要适配就先适配、补测试，
  再发版（适配与发版可以是两个提交，但不要带着未处理的适配发版）

> 上次适配到的提交：写在最新一条 `adapt(upstream): ...` 提交信息里，例如
> `adapt(upstream): 出站指纹默认对齐桌面端；新增快过期积分窗口` 对应上游
> `bc77429`。查一下最近的 adapt 提交即可。

### 判断「是否需要适配」的准绳

管理端有两条路径会与上游产生耦合：

1. **转发路径**（网关 `/v1/*` → 上游）：上游改内部实现通常与我们无关，
   但如果它改了**入站协议/响应形状**，我们就得跟。
2. **直连路径**（管理端**绕过上游直连腾讯**）：扫码登录、签到、查积分、
   地区注册、trial、探测。**上游的改动不会自动惠及这条路**，需要人工同步
   （请求头、请求体形状、错误码语义都要对齐上游的参照实现）。

第 2 类最容易漏，因为上游改了不会通知我们，接口也照样返回 200。

## 更新日志的写法：写给用户，不是写给同事

`CHANGELOG.md` 的每一条都会**原样出现在用户看得见的地方**：GitHub Release 页面
的正文（CI 从 CHANGELOG 抽取）、应用内「设置 → 更新日志」页。读者是**使用者**，
不是维护者。

这个坑反复踩过：修完一个自己引入的缺陷后，很容易顺手把排障过程写进去 ——
函数名、模块路径、「我错在哪」「已做反向验证」「用例变红」，以及大段的设计权衡
推演。这些对用户毫无价值，还会把「升级后我要做什么」这个真正有用的信息淹掉。
1.0.42 ~ 1.0.49 全线存在这个问题，所以现在有测试守着（见下）。

**写**：

- 用户会遇到什么现象、升级后有什么变化
- 引用报错时**照抄界面上那句原文**（用户就是拿它来搜的）
- 用户要动手的东西：环境变量、配置项、命令
- 「为什么这么设计」里**影响用户决策**的那部分（如「上限不再跟随上游配置，
  改用环境变量 X」）

**不写**：

- 函数名、模块路径、变量名、测试名、内部术语（事件循环、线程池……）
- 排障过程与自我检讨（「根因是我……」「已做反向验证」）
- 代码评审式的取舍推演（内部权衡放代码注释，提交信息里可以写）

**判据**：一个只用产品、不读代码的人，能否看懂这条对他有什么影响。

**唯一的例外**：用户界面上的**报错原文**要照抄（含内部术语也没关系）——
守卫测试按「该词是否出现在用户可见的文案里」自动放行。

守卫在 `server/tests/test_changelog.py::ChangelogIsUserFacingTest`：扫全部版本段落，
检测内部标识符、排障叙事、以及未被用户文案引用的框架术语。写错了跑测试就会报出来，
**发版前那次 `unittest discover` 是最后一道闸**。

## 发版步骤

```bash
# 1) 改版本号与更新日志（「未发布」→「[x.y.z] - 日期」）
#    server/main.py 的 version、CHANGELOG.md
python -m unittest discover -s server/tests -t .   # 全绿才继续

# 2) 提交并推送
git add -A && git commit -m "chore(release): vX.Y.Z"
git push origin main

# 3) 打 tag（CI 会构建并创建 Release，**CI 不签名**）
git tag vX.Y.Z && git push origin vX.Y.Z

# 4) 等 CI 完成后，在本机对**最终产物**签名并上传
gh release download vX.Y.Z --repo ithtelab/workbuddy-manager --pattern '*.tar.gz'
ssh-keygen -Y sign -f ~/.ssh/workbuddy-release -n file workbuddy-manager-vX.Y.Z.tar.gz
gh release upload vX.Y.Z workbuddy-manager-vX.Y.Z.tar.gz.sig \
  --repo ithtelab/workbuddy-manager

# 5) 重新下载验证（不要用刚签名的那份，要重新拉）
#    比对 sha256 → 用 deploy/update.py 的 check_signature 验签 → 解开核对版本号
```

**没有 `.sig` 的 Release 会被所有用户的一键更新拒绝**——这一步不是可选的。

## 多人协作时的分工

仓库有 write 权限的协作者**可以直接推 tag**，推了就触发 CI 建 Release——但
他们**签不了名**（私钥只在维护者本机，这是设计如此）。所以协作发版的分工是
「**协作者准备，维护者签字**」：

**协作者可以独立完成的部分**

```bash
bash deploy/check-upstream.sh <上次适配到的提交>   # 先查上游
# 改 .version 与 server/main.py 的 version、CHANGELOG.md 加段落
python -m unittest discover -s server/tests -t .   # 全绿
# 走 PR 合入 main，再推 tag
git tag vX.Y.Z && git push origin vX.Y.Z
```

**维护者收尾（只有这三步）**

```bash
gh release download vX.Y.Z --repo ithtelab/workbuddy-manager --pattern '*.tar.gz'
ssh-keygen -Y sign -f ~/.ssh/workbuddy-release -n file workbuddy-manager-vX.Y.Z.tar.gz
gh release upload vX.Y.Z workbuddy-manager-vX.Y.Z.tar.gz.sig \
  --repo ithtelab/workbuddy-manager
```

签名前核对密钥没拿错：`ssh-keygen -lf ~/.ssh/workbuddy-release.pub` 应输出
`SHA256:xmHLJDKH/vYtAp59XwXPVE4A/CwXAOpxTlYh7KC677Y`。

**协作者发完版、维护者没签之前，用户的一键更新会全部被拒绝**——这不是故障，
是防线在工作。发现 Release 缺 `.sig` 时，按上面三步补签即可，不必重跑 CI。

> tag 与 main 分支都有保护规则（见下节）：协作者不能创建/删除 `v*` tag，
> 也不能直接往 main 推。这不是不信任，而是让「能改代码」与「能发布可信产物」
> 保持分离——这正是签名机制要解决的问题。

## 仓库保护规则

用 **ruleset**（不是旧的 branch protection）配置，因为 tag 保护只有 ruleset 支持。

| 规则 | 作用 |
|---|---|
| `protect-tags` (id 23464783) | `refs/tags/**` 的创建/删除/移动仅 admin 可做 |
| `protect-main` (id 23465105) | main 禁止删除与强推，且**必须走 PR**（至少 1 个批准） |

```bash
# 查看
gh api repos/ithtelab/workbuddy-manager/rulesets \
  --jq '.[] | "\(.id) \(.name) \(.target)"'
```

### bypass_actors 的 actor_id 是未公开的映射，别猜

`bypass_actors` 里的 `actor_id` 对 `RepositoryRole` 而言**没有被 GitHub 官方文档
记录**，而且**不是顺序编号**——按直觉填会出事：

| 角色 | actor_id |
|---|---|
| maintain | 2 |
| write | 4 |
| **admin** | **5** |

（有人按「read=1, triage=2, write=3, maintain=4, admin=5」推断后填了 4，
结果**任何 write 权限的人都能绕过**规则。）

这里用的是 `5`（admin）。这个值可以用 GraphQL 直接把角色名读出来核对——
REST 接口只回显整数，GraphQL 会给出 `repositoryRoleName`：

```bash
gh api graphql -f query='{ repository(owner:"ithtelab", name:"workbuddy-manager") {
  rulesets(first:10){ nodes { name target bypassActors(first:20){
    nodes { repositoryRoleDatabaseId repositoryRoleName bypassMode } } } } } }'
```

应输出 `admin(id=5)/ALWAYS`。**改动这条规则后请重新核对一次**：写错的后果是
保护静默失效，而界面上看起来一切正常。

> 用角色而不是具体用户，是为了以后增减管理员时不用改规则。

## 为什么签名不能自动化

私钥不进 CI：恶意 PR 合并后可以改 workflow 的任意步骤，密钥放那里等于直接
交给攻击者。签名必须由维护者在本机执行。详见 [release-signing.md](release-signing.md)。

## 如果网络不稳

下载产物时若**体积小于 GitHub 报告的大小**，是下载被截断（本机代理不稳），
不是产物被动过——用 `curl -C -` 续传后重新比对 sha256 即可。
验签会正确拒绝截断的文件，这是设计如此。
