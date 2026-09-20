# WorkBuddy Manager —— 停止本机服务（Windows / PowerShell）
& (Join-Path (Split-Path -Parent $MyInvocation.MyCommand.Path) 'service-tools.ps1') stop
