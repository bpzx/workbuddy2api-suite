# 部署指南

本项目管理端**依赖上游 [`workbuddy2api`](https://github.com/Sliverkiss/workbuddy2api)**
（提供账号池调度与 OpenAI 兼容接口）。单独 clone 本仓库是跑不起来的 ——
为此我们提供了一键脚本，会在干净机器上自动安装好两者。

> **重要**：请勿把本项目管理端的数据目录与上游账号目录提交或公开分享，
> 其中含账号授权凭据。

---

## 一、最简单的方式：Release 包 + 一键脚本

```bash
# 1) 下载 Release 包（内含已构建的前端，无需 Node.js）
wget https://github.com/ithtelab/workbuddy-manager/releases/latest/download/workbuddy-manager-<版本>.tar.gz
tar xzf workbuddy-manager-*.tar.gz
cd workbuddy-manager-*

# 2) 一键部署（会自动检测并安装上游 workbuddy2api）
sudo bash deploy/install.sh
```

脚本会自动完成：

1. 环境预检（Python ≥3.9、Docker、端口占用检查）
2. **安装上游 workbuddy2api** —— 克隆、生成随机 `api_key`、设置目录属主、
   构建并启动容器、等待就绪
3. 安装管理端 —— 部署代码、装依赖、注册 systemd 服务
4. 验证两条链路并打印访问地址与初始密码

**全程无需手工编辑任何配置文件。**

---

## 二、通过 git clone 部署

```bash
git clone https://github.com/ithtelab/workbuddy-manager.git
cd workbuddy-manager

# 需先在 web/ 构建前端（git 仓库不含构建产物）
cd web && npm ci && npm run build:export && cd ..

sudo bash deploy/install.sh
```

> 若机器上无 Node.js，请改用 Release 包，或先安装 Node.js ≥18。

---

## 三、已有 workbuddy2api 的情况

若已自行部署上游，用参数跳过安装，脚本不会改动已有配置与账号：

```bash
sudo bash deploy/install.sh --skip-upstream
```

也可用环境变量指定自定义路径与端口：

```bash
sudo APP_DIR=/opt/wbm \
     UPSTREAM_DIR=/srv/workbuddy2api \
     MANAGER_PORT=7864 UPSTREAM_PORT=7863 \
     bash deploy/install.sh
```

| 变量 | 默认值 | 说明 |
|---|---|---|
| `APP_DIR` | `/opt/workbuddy-manager` | 管理端目录 |
| `UPSTREAM_DIR` | `/opt/workbuddy2api` | 上游目录 |
| `UPSTREAM_REPO` | 上游 GitHub 地址 | 上游仓库地址 |
| `MANAGER_PORT` | `7864` | 管理端端口 |
| `UPSTREAM_PORT` | `7863` | 上游端口 |
| `PY` | `/usr/bin/python3` | Python 解释器路径 |

---

## 四、部署后

### 1. 获取登录密码

脚本会在末尾打印；也可随时查看：

```bash
journalctl -u workbuddy-web | grep -A3 '初始管理员'
```

默认用户 `admin`。**请登录后立即在「设置 → 管理用户」中修改密码。**

### 2. 添加账号

浏览器打开管理端 → 「账号」页 → 「添加账号」→ 用微信 / QQ 扫码。

授权成功后会自动签到、落盘并重载上游，无需手工操作。
（上游的扫码是交互式的，因此无法在脚本里自动化，必须由你扫码一次。）

### 3. 配置 HTTPS 反向代理

公网访问**务必**走 HTTPS，否则会话 Cookie 与密码可被窃听。

**1Panel**：网站 → 创建反向代理 → 目标 `http://127.0.0.1:7864`
→ 申请 Let's Encrypt 证书 → 开启强制 HTTPS。

**Nginx**（手工）：

```nginx
server {
    listen 443 ssl http2;
    server_name wb.example.com;

    ssl_certificate     /path/fullchain.pem;
    ssl_certificate_key /path/privkey.pem;

    client_max_body_size 16m;   # 需大于管理端 8 MiB 的网关请求体上限

    location / {
        proxy_pass http://127.0.0.1:7864;
        proxy_set_header Host              $host;
        proxy_set_header X-Real-IP         $remote_addr;   # 必须：真实 IP 来源
        proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
        proxy_http_version 1.1;
        proxy_set_header Upgrade    $http_upgrade;
        proxy_set_header Connection "upgrade";
        proxy_read_timeout 300s;    # 流式对话需较长超时
    }
}
```

> `X-Real-IP` 是管理端判定真实来源 IP 的**首选依据**（`X-Forwarded-For`
> 首段可被客户端伪造）。若前面还叠了 CDN，请把服务端
> `WB_TRUSTED_PROXY_HOPS` 设为 CDN + 反代的层数。

### 4. 加固建议

- 用防火墙或安全组只放行 `22 / 80 / 443`；`7863`、`7864` 保持仅本机
- 可在 1Panel 为站点配置 IP 白名单，或叠加 Cloudflare Access
- 定期轮换管理端密码与网关密钥

---

## 四·五、后续更新

部署后有两种更新方式，**都不需要重新执行安装脚本**。

### 方式一：网页一键更新（推荐）

登录后进入 **设置 → 系统更新**，选择：

| 模式 | 作用 | 适用场景 |
|---|---|---|
| **全部更新** | 上游 + 管理端 | 常规升级 |
| **仅上游** | 只更新 workbuddy2api | 上游有修复 / 新模型 |
| **仅管理端** | 只更新本控制台 | 界面或管理功能升级 |

更新在后台执行，页面实时显示进度与日志；期间服务可能短暂重启
（页面会自动重连）。**账号授权、上游配置、密钥与日志数据都会保留。**

> 上游更新会自动把端口绑定重新收敛为 `127.0.0.1`，避免上游仓库里的
> `7863:7863` 覆盖本项目的安全基线。

### 方式二：命令行

```bash
cd /opt/workbuddy-manager
sudo /opt/workbuddy-manager/venv/bin/python deploy/update.py --target both
```

`--target` 取值为 `manager` / `upstream` / `both`。

---

## 五、目录与端口

```
管理端   /opt/workbuddy-manager         :7864   仅本机（经反代对外）
  ├─ server/        后端代码
  ├─ web/out/       前端静态产物
  ├─ data/          SQLite、users.json（含会话密钥）
  └─ venv/          Python 虚拟环境

上游     /opt/workbuddy2api             :7863   仅本机
  ├─ auths/         账号授权文件（每个账号一个 JSON）
  ├─ data/state.json 账号池状态
  └─ config.json    上游配置（含 api_key）
```

---

## 六、常用运维命令

```bash
# 日志
journalctl -u workbuddy-web -f                    # 管理端
cd /opt/workbuddy2api && docker compose logs -f   # 上游

# 重启
systemctl restart workbuddy-web
cd /opt/workbuddy2api && docker compose restart

# 状态
systemctl status workbuddy-web
curl -s http://127.0.0.1:7864/healthz             # 含上游连通性

# 升级
cd /opt/workbuddy-manager && git pull              # 管理端代码
cd /opt/workbuddy2api && git pull && docker compose up -d --build  # 上游
```

---

## 七、卸载

```bash
systemctl disable --now workbuddy-web
rm -f /etc/systemd/system/workbuddy-web.service && systemctl daemon-reload
rm -rf /opt/workbuddy-manager

cd /opt/workbuddy2api && docker compose down
rm -rf /opt/workbuddy2api        # 注意：会删除账号授权文件
```

---

## 八、常见问题

**Q：脚本提示「未检测到 Docker」**
上游只提供 Docker 部署方式。请先安装 Docker 与 compose 插件，或用
`--skip-upstream` 跳过并自行部署上游。

**Q：管理端显示「反代上游不可用」**
上游容器未运行或端口不通。检查：

```bash
cd /opt/workbuddy2api && docker compose ps
curl -s http://127.0.0.1:7863/healthz
```

**Q：account 目录权限报错（Permission denied）**
容器内以 uid 10001 运行，宿主机目录需归属该 uid：

```bash
chown -R 10001:10001 /opt/workbuddy2api/auths /opt/workbuddy2api/data
```

**Q：首次构建上游很慢**
上游是 Go 项目，首次 `docker compose up --build` 需拉取基础镜像并编译，
视网络情况可能需要几分钟，属正常现象。

**Q：扫码后账号没出现**
检查上游日志与账号文件：

```bash
docker compose -f /opt/workbuddy2api/docker-compose.yml logs --tail 50
ls -l /opt/workbuddy2api/auths/
```

**Q：公网访问点「添加账号」二维码加载不出来**
管理端需要访问腾讯接口生成授权链接，请确认服务器可访问外网。
