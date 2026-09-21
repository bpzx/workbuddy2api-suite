# syntax=docker/dockerfile:1
#
# workbuddy2api-suite 单镜像（双服务）
# =====================================
# 一个镜像里同时装 wb2api（Go 网关）与 manager（FastAPI + 静态前端），
# 由 docker-compose 用不同 command 拉起两个容器。
#
# 为什么单镜像：两个服务必须版本一致（manager 依赖 wb2api 的接口与
# config.json 字段口径），单镜像从根上排除"两个镜像版本错配"这类故障，
# 也让发布只有一个产物、一处版本号。
#
# 上游代码（vendor/）在本文件里**只被拷贝，从不被修改**；
# 唯一例外是 deploy/update.py 的替换（见 runtime 阶段），且用的是本项目
# 新写的替身文件，vendor 源码保持原样。详见 UPSTREAMS.md。
#
# 目录角色：
#   /opt/wb2api  Go 二进制 + 自带脚本
#   /opt/manager Python 后端 + 前端静态产物
#   /opt/suite   容器入口与套件元信息
#   /data        唯一数据卷（auths / config.json / state.json / manager 数据）

# ── 阶段 0：给上游源码打集成补丁 ──────────────────────────
# 本项目遵守「vendor/ 下上游代码一字不改」的约束（见 UPSTREAMS.md），
# 但容器化集成需要两处上游没有的行为（内网直连、Go 走代理）。
# 折中：源码原样入库，构建期在此打补丁，产物供后续阶段使用。
#
# 补丁脚本带 fail-fast 断言：上游若重构了锚点代码，**构建会失败**并指出
# 要改哪里。这是刻意的——静默跳过会让镜像能构建能启动、但代理行为悄悄失效。
FROM python:3.11-slim-bookworm AS vpatch
WORKDIR /src
COPY vendor/ /src/vendor/
COPY docker/patches/apply.py /src/patches/apply.py
# overlay/ 放「本发行版自己写、或整文件覆写」的文件，由 apply.py 在构建期落进
# vendor 树（新增：套件更新后端；覆写：更新面板 UI）。
# 必须放在与 patches/ 同级的位置：apply.py 用 __file__ 的上级目录定位它。
COPY docker/overlay/ /src/overlay/
RUN python /src/patches/apply.py /src/vendor

# ── 阶段 1：构建前端静态导出 ──────────────────────────────
FROM node:20-bookworm-slim AS web
WORKDIR /src/web
# 先只拷依赖清单，让 npm ci 这一层可被缓存（改业务代码不必重装依赖）
COPY --from=vpatch /src/vendor/manager/web/package.json /src/vendor/manager/web/package-lock.json ./
RUN npm ci
# 再拷源码构建。build:export = NEXT_OUTPUT_EXPORT=1 next build → out/
# ⚠️ 必须取自 vpatch：更新面板（UpdatePanel.tsx）与 i18n 文案都由 apply.py
# 在 vpatch 阶段落位，从构建上下文拷会丢掉它们 —— 界面会退回上游的
# 「3 个更新目标」旧面板，且缺少本套件区块的文案。
COPY --from=vpatch /src/vendor/manager/web/ ./
RUN npm run build:export \
 && test -f out/index.html

# ── 阶段 2：编译 wb2api 全部二进制 ────────────────────────
FROM golang:1.23-alpine AS gobuild
WORKDIR /src
COPY --from=vpatch /src/vendor/wb2api/go.mod /src/vendor/wb2api/go.sum ./
RUN go mod download
COPY --from=vpatch /src/vendor/wb2api/ ./
# 与上游 Dockerfile 同款：一次编译全部二进制（工具进镜像，容器内可直接跑脚本），
# 全部 -trimpath -s -w，CGO 关闭（纯静态）。
RUN CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/wb2api        ./cmd/server \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/signin_bin   ./cmd/signin \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/login        ./cmd/login \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/credit       ./cmd/credit \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/trial_bin    ./cmd/trial \
 && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/activity_bin ./cmd/activity

# ── 阶段 3：docker CLI ────────────────────────────────────
# manager 需要 `docker restart` / `docker logs` 操作上游容器。
# 只取 CLI 静态二进制，不装 docker 引擎（绝不能在这个镜像里跑 dockerd）。
FROM docker:29-cli AS dockercli

# ── 阶段 4：运行时 ────────────────────────────────────────
# 选 Debian slim 而非 alpine：pydantic-core / uvicorn 的 C 扩展在 glibc 下
# 有完整 wheel，省掉在 musl 上现场编译的脆弱环节。
FROM python:3.11-slim-bookworm

# tzdata 必须有：上游签到/旅行的时点判定（04:00 重置、夜猫 23:00–08:00）
# 全部依赖本地时区，容器默认 UTC 会让所有定时任务错点。
# curl/jq：wb2api 自带的 shell 工具脚本（checkin.sh 等）依赖它们；
# bash：这些脚本的 shebang；tini：位于本文件末尾作为 PID 1；
# gosu：入口脚本以 root 修正数据目录属主后降权到 app 运行（见下）。
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      ca-certificates tzdata curl jq bash tini gosu \
 && rm -rf /var/lib/apt/lists/* \
 && ln -snf /usr/share/zoneinfo/Asia/Shanghai /etc/localtime \
 && echo "Asia/Shanghai" > /etc/timezone

# 非 root 用户。两个服务共用同一个 uid/gid，以便共享 /data
# （wb2api 写 auths/state.json，manager 读写同一批文件并新增账号）。
# uid 10001 与上游 wb2api 镜像保持一致，便于从旧部署迁移过来的卷继续可用。
#
# 为什么宿主目录需要下面这套 root 入口逻辑：
#   named volume 的属主由 Docker 从镜像继承（自动就是 app）；
#   而 bind mount 的目录由宿主机创建，属主是 root，权限通常 755 ——
#   uid 10001 在里面**连建 auths/ 都做不到**，服务启动即失败。
#   这类报错（permission denied）对不熟悉 uid 的用户很难定位，因此由
#   入口脚本在启动时以 root 修正属主，再降权运行服务。
RUN groupadd -g 10001 app \
 && useradd -u 10001 -g 10001 -M -s /usr/sbin/nologin app

# ── docker CLI ────────────────────────────────────────────
# manager 的 `docker restart` / `docker logs` 靠它。只拷静态链接的 CLI 二进制，
# 不装 docker 引擎（本镜像绝不跑 dockerd）。
# 运行时通过 compose 的 DOCKER_HOST=tcp://dockerproxy:2375 连 socket 代理，
# 而不是直接挂 /var/run/docker.sock。
COPY --from=dockercli /usr/local/bin/docker /usr/local/bin/docker

# ── docker compose 插件 ───────────────────────────────────
# 为什么必须自己装：docker 官方那个静态包**不含 compose 插件**
# （实测 `tar -tzf docker-27.3.1.tgz` 只有 docker/dockerd/ctr/containerd*），
# 上游 manager 也因此吃过 issue #28（容器内 `up --build` 报 exit 127）。
# 我们把 CLI 从 docker:29-cli 取出来，同样不带 compose，所以这里补装。
#
# 谁需要它：updater 侧车派生的一次性 helper 容器，靠 `docker compose pull`
# + `up -d` 在宿主机侧重拉镜像并重建全部服务。没有它「一键更新」直接不可用。
#
# 装在官方约定的插件目录，文件名必须是连字符 `docker-compose`，
# `docker compose` 子命令才认得它。
#
# **必须停在 compose v2，不要升到 v5**（上游实测记录，见其 ComposeCommandTest）：
# v5 移除了内置 builder，`up --build` 改为调用外部 buildx 插件且**没有回退
# 分支**，而本镜像只装 CLI + compose、没有 buildx。v2 有内置 builder 作回退。
ARG COMPOSE_VERSION=v2.40.3
RUN set -eux; \
    case "${TARGETARCH:-$(uname -m)}" in \
        amd64 | x86_64)  COMPOSE_ARCH=x86_64 ;; \
        arm64 | aarch64) COMPOSE_ARCH=aarch64 ;; \
        arm | armv7l)    COMPOSE_ARCH=armv7 ;; \
        *) echo "compose 插件不支持的架构：${TARGETARCH:-$(uname -m)}" >&2; exit 1 ;; \
    esac; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL --retry 5 --retry-delay 2 --retry-all-errors \
        "https://github.com/docker/compose/releases/download/${COMPOSE_VERSION}/docker-compose-linux-${COMPOSE_ARCH}" \
        -o /usr/local/lib/docker/cli-plugins/docker-compose; \
    chmod +x /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker compose version

# ── wb2api ────────────────────────────────────────────────
COPY --from=gobuild /out/ /opt/wb2api/
# 自带脚本与它们 import 的 Python 模块（登录、签到、积分、开学季、夜猫）
COPY vendor/wb2api/login.sh vendor/wb2api/signin.sh vendor/wb2api/credit.sh \
     vendor/wb2api/trial.sh vendor/wb2api/checkin.sh /opt/wb2api/
COPY vendor/wb2api/scripts/ /opt/wb2api/scripts/
# 去 CRLF（Windows 检出可能）：容器内脚本以 \r 结尾会导致 shebang 失效
RUN sed -i 's/\r$//' /opt/wb2api/*.sh && chmod 755 /opt/wb2api/*.sh \
 && sed -i 's/\r$//' /opt/wb2api/scripts/*.py /opt/wb2api/scripts/*.sh \
 && chmod 755 /opt/wb2api/scripts/*.py /opt/wb2api/scripts/*.sh \
 && rm -f /opt/wb2api/config.example.json

# 注意：指向 /data 的软链在文件末尾统一创建（见「收尾」段）。
# 放在这里的话，末尾的 `chown -R /opt/wb2api` 会遍历到它们，而 config.json
# 此时还没有目标（悬空链），chown 的行为依实现而异，不如放到 chown 之后确定。

# ── manager ───────────────────────────────────────────────
# ⚠️ 必须取自 vpatch 阶段：manager/server/config.py 带集成补丁
# （内网请求直连，不走 WB_HTTP_PROXY）。从构建上下文拷会丢掉补丁。
COPY --from=vpatch /src/vendor/manager/server/ /opt/manager/server/
COPY vendor/manager/CHANGELOG.md /opt/manager/CHANGELOG.md
# server/ 内也放一份：changelog 的候选路径包含 server/CHANGELOG.md
# （历史更新器只整体替换 server/，旧部署升上来时根目录可能没有该文件）
COPY vendor/manager/CHANGELOG.md /opt/manager/server/CHANGELOG.md
COPY vendor/manager/LICENSE /opt/manager/LICENSE
# 前端静态导出（uvicorn 直接托管，无需 Node 运行时）
COPY --from=web /src/web/out/ /opt/manager/web/out/

# 容器化下的「一键更新」替身。构建期覆盖，vendor 源码不动。
# 裸机版的 deploy/update.py 会 git 检出 + systemctl + 覆盖自身代码，
# 在容器里跑会把运行中的代码改坏（见 docker/stub/update.py 注释）。
COPY docker/stub/update.py /opt/manager/deploy/update.py

# ── 套件元信息 ────────────────────────────────────────────
# 镜像自带上游血统：docker inspect 即可查证捆的是哪两个 commit
COPY upstreams.json /opt/suite/upstreams.json
# 构建参数：由 CI 注入，让镜像自带可查证的血统。
# GITHUB_REPOSITORY 必须显式声明为 ARG —— Dockerfile 里不会自动看到
# CI 的环境变量，不声明的话 ${GITHUB_REPOSITORY} 会展开成空串，
# 镜像的 source 标签就变成 "https://github.com/"（无用的半截 URL）。
ARG GITHUB_REPOSITORY=""
ARG WB2API_COMMIT=unknown
ARG MANAGER_COMMIT=unknown
ARG SUITE_VERSION=dev
LABEL org.opencontainers.image.title="workbuddy2api-suite" \
      org.opencontainers.image.description="基于 workbuddy2api + workbuddy-manager 构建的容器化发行版" \
      org.opencontainers.image.source="https://github.com/${GITHUB_REPOSITORY}" \
      org.opencontainers.image.version="${SUITE_VERSION}" \
      io.workbuddy2api-suite.upstream.wb2api.repo="Sliverkiss/workbuddy2api" \
      io.workbuddy2api-suite.upstream.wb2api.commit="${WB2API_COMMIT}" \
      io.workbuddy2api-suite.upstream.manager.repo="ithtelab/workbuddy-manager" \
      io.workbuddy2api-suite.upstream.manager.commit="${MANAGER_COMMIT}"

# ── 套件版本（构建期从 git tag 写入）───────────────────────
# SUITE_VERSION 由 CI 计算：打了 v* tag 是 v1.2.3，日常 push main 是 sha-xxxxxxx。
# 它此前**只**进 OCI 标签，运行中的容器无从知道自己是什么版本 —— 而「一键更新」
# 必须先能比较"当前版本 vs 最新版本"。所以同时写一份文件 + 一个环境变量：
# 后端 server/services/suite.py 优先读环境变量，回退读文件。
# 放在 /opt/suite，与 upstreams.json 一起构成镜像自带的血统信息。
#
# WB_SUITE_REPO 复用 GITHUB_REPOSITORY（CI 已注入），后端用它查「最新正式版」
# tag。这样**不需要用户额外配置**套件仓库地址。
RUN printf '%s' "${SUITE_VERSION}" > /opt/suite/version
ENV WB_SUITE_VERSION=${SUITE_VERSION} \
    WB_SUITE_REPO=${GITHUB_REPOSITORY}

COPY docker/entrypoint-wb2api.sh docker/entrypoint-manager.sh \
     docker/entrypoint-updater.sh /opt/suite/
# updater 侧车的两个 Python 文件。update_runner.py 会被侧车派生的 helper
# 容器当 entrypoint 直接执行，所以必须有可执行位（见下面的 chmod）。
# config_merge.py 由 entrypoint-wb2api.sh 在启动时调用，把上游新增的配置项
# 增量补齐进已有的 config.json（只补缺失键，不改已有值）。
COPY docker/overlay/suite_updater.py docker/overlay/update_runner.py \
     docker/overlay/config_merge.py /opt/suite/
COPY docker/wb2api.config.template.json /opt/suite/
# 去 CRLF（Windows 检出可能）：容器内脚本以 \r 结尾会导致 shebang 失效
RUN sed -i 's/\r$//' /opt/suite/*.sh /opt/suite/*.py \
 && chmod 755 /opt/suite/*.sh /opt/suite/*.py

# ── Python 依赖 ───────────────────────────────────────────
# manager 的 requirements 是依赖来源；装完即删清单，不留在镜像里。
#
# 额外装 httpx[socks]：上游 requirements 只写了 httpx>=0.27.0，不含 SOCKS 支持。
# 一旦 WB_HTTP_PROXY 指向 socks5:// 代理，httpx 会抛
#   ImportError: Using SOCKS proxy, but the 'socksio' package is not installed.
# 这里补上 socksio，使 SOCKS 代理可用。
# 注：这只影响「通过 httpx 发起的请求」（manager 直连腾讯的那批）；httpx 之外的
# 出站（如 wb2api 的 Go 流量）走各自的机制，见 docker/patches/apply.py。
COPY vendor/manager/server/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
 && pip install --no-cache-dir 'httpx[socks]' \
 && rm /tmp/requirements.txt

# ── 收尾：目录与属主 ──────────────────────────────────────
# ⚠️ chown 必须在**所有 COPY 之后**：Docker 的 COPY 以 root 落盘，
# 早于 COPY 的 chown 会被后续拷贝覆盖回 root，导致以 app 身份运行时
# 写不进自己的目录（表现：manager 的版本自愈写 .version 静默失败）。
# 这与上游 wb2api 镜像的做法一致（先 COPY 再 chown -R app:app /app）。
#
# 有意不预置 manager 的 .version：那是**上游 manager 自己的**版本痕迹文件，
# 上游 current_version() 会按代码里的 app.version 自愈它（第一次打开面板就会
# 写入 /opt/manager/.version）。套件版本是另一条线，由上面的
# WB_SUITE_VERSION 环境变量 + /opt/suite/version 表达，两者不要混淆。
RUN mkdir -p /data/auths \
 && chown -R app:app /opt/wb2api /opt/manager /opt/suite /data

# 自带脚本按 cwd=/opt/wb2api 读写 ./auths 与 ./config.json（见 login.sh 的
# AUTH_DIR、checkin.sh 的 WB2A_CONFIG）。不软链的话，用
# `docker compose exec wb2api ./login.sh` 加的账号会写进镜像层，容器重建即丢失。
# 软链到数据卷，让命令行与面板操作走同一份数据。
# config.json 的目标在首次启动时才由 entrypoint 生成，悬空链在启动前无害
# （脚本读到不存在会用 fallback 值并降级提示）。
# 放在 chown 之后：避免 chown -R 遍历软链目标。
RUN ln -s /data/auths /opt/wb2api/auths \
 && ln -s /data/config.json /opt/wb2api/config.json \
 && chown -h app:app /opt/wb2api/auths /opt/wb2api/config.json

# ── 运行时约定 ────────────────────────────────────────────
#
# 版本信息现在有**三条独立的线**，别混淆（面板上分三段显示）：
#
#   1. 本套件（WB_SUITE_VERSION，上面写入）：本镜像的版本，来自 CI 的 git tag。
#      「一键更新」比的就是它 —— 拉新镜像、重建容器。
#   2. 上游网关 workbuddy2api：固定 commit（/opt/suite/upstreams.json）
#      对比远端默认分支 HEAD。只报告，不提供更新动作。
#   3. 上游管理端 workbuddy-manager：快照自带的上游 app.version
#      （由 manager 的 current_version() 读出）对比上游最新 Release。
#      也只报告。**注意这个数字是上游的版本号，不是本套件的版本号** ——
#      历史上它就被误读过（"管理端 v1.0.57 是本项目吗？"），所以新版面板
#      把它明确放在「上游依赖」区块里。
#
# 上游两块之所以只报告不更新：容器里既没有上游 git 仓库，也不该由运行中的
# 容器去改自己的代码。上游更新一律走宿主机
# `./scripts/sync-upstreams.sh` → 重建镜像 → `docker compose pull && up -d`。
# 详见 UPSTREAMS.md。
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    TZ=Asia/Shanghai \
    WB_SUITE_DATA_DIR=/data
WORKDIR /opt/manager
# 7863 wb2api（不在 compose 里发布到宿主）／7864 管理端／7865 updater 侧车
EXPOSE 7863 7864 7865

# ⚠️ 这里**故意不写 USER app**，容器以 root 启动，由入口脚本在 chown 数据目录
# 之后立刻 gosu 降权到 uid 10001，服务进程本身仍是非 root。
#
# 为什么必须这样：数据默认放宿主目录（bind mount），该目录由 Docker/宿主创建时
# 属主是 root，而服务以 uid 10001 运行 —— 不修正属主就连建 auths/ 都会
# permission denied，服务起不来。修正属主需要 root 权限。
# 这是官方镜像的常规做法（postgres / redis 同样以 root 入口再降权）；
# 降权后服务进程不具备 root 能力，实际攻击面没有扩大。
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/opt/suite/entrypoint-manager.sh"]
