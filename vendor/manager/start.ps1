# WorkBuddy Manager —— 本机启动脚本（Windows / PowerShell）
#
#   powershell -ExecutionPolicy Bypass -File start.ps1
#
# 等价于 Linux 上的 systemd 常驻服务：前台运行，Ctrl+C 退出。
# 环境变量全部读根目录的 .env（路径、端口、初始密码都在那里改）。
$ErrorActionPreference = 'Stop'

$root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $root

$python = Join-Path $root '.venv\Scripts\python.exe'
if (-not (Test-Path $python)) {
    Write-Error "未找到虚拟环境 $python，请先执行：python -m venv .venv; .\.venv\Scripts\python -m pip install -r server\requirements.txt"
}

# 中文 Windows 的默认编码是 GBK，而 docker logs / 腾讯接口返回的都是 UTF-8：
# 不开 UTF-8 模式会让「读上游日志」的线程抛 UnicodeDecodeError（任务记录页取不到数据）。
$env:PYTHONUTF8 = '1'
$env:PYTHONIOENCODING = 'utf-8'

# 监听地址与端口从 .env 读，改一处即可（需要局域网访问就把 WB_MANAGER_HOST 改成 0.0.0.0）
$bindHost = '127.0.0.1'
$bindPort = '7864'
$envPath = Join-Path $root '.env'
if (Test-Path $envPath) {
    foreach ($line in Get-Content $envPath) {
        if ($line -match '^\s*WB_MANAGER_HOST\s*=\s*(\S+)') { $bindHost = $Matches[1] }
        elseif ($line -match '^\s*WB_MANAGER_PORT\s*=\s*(\S+)') { $bindPort = $Matches[1] }
    }
}

if (-not (Test-Path (Join-Path $root 'web\out\index.html'))) {
    Write-Warning 'web\out 不存在：前端尚未构建，页面会 404。请先在 web 目录执行 npm ci; $env:NEXT_OUTPUT_EXPORT=1; npm run build'
}

# uvicorn 的 --env-file 会读取 .env（依赖 python-dotenv，已在 requirements 里）
Write-Host "WorkBuddy Manager 启动中：http://${bindHost}:${bindPort}（Ctrl+C 退出）"
& $python -m uvicorn server.main:app --env-file .env --host $bindHost --port $bindPort
