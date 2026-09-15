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

## 为什么签名不能自动化

私钥不进 CI：恶意 PR 合并后可以改 workflow 的任意步骤，密钥放那里等于直接
交给攻击者。签名必须由维护者在本机执行。详见 [release-signing.md](release-signing.md)。

## 如果网络不稳

下载产物时若**体积小于 GitHub 报告的大小**，是下载被截断（本机代理不稳），
不是产物被动过——用 `curl -C -` 续传后重新比对 sha256 即可。
验签会正确拒绝截断的文件，这是设计如此。
