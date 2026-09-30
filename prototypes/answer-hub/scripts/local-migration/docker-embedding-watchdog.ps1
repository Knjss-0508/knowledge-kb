# Docker Desktop 守护：不在运行就启动，并等待容器就绪
$ErrorActionPreference = "Continue"
$log = "E:\answer-hub-runtime\docker-watchdog.log"
function W($m) { Add-Content -Path $log -Value ("[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m) -Encoding utf8 }

$ddCandidates = @("E:\DockerDesktop\Docker Desktop.exe", "C:\Program Files\Docker\Docker\Docker Desktop.exe", "D:\Docker\Docker\Docker Desktop.exe")
$dd = $ddCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

$proc = Get-Process "Docker Desktop" -ErrorAction SilentlyContinue
if (-not $proc) {
    if (-not $dd) { W "❌ 找不到 Docker Desktop.exe，无法启动"; exit 1 }
    W "Docker Desktop 未运行，启动: $dd"
    Start-Process $dd -ErrorAction SilentlyContinue
    Start-Sleep -Seconds 45
} else {
    # 进程在，但引擎可能没就绪
    $null = & docker info 2>&1
    if ($LASTEXITCODE -ne 0) { W "进程在但引擎未就绪，等待..." ; Start-Sleep -Seconds 30 }
}

# 检查生产关键容器
$need = @("kb-embedding-qwen", "kb-embedding-tunnel")
$running = @(& docker ps --format "{{.Names}}" 2>$null)
foreach ($c in $need) {
    if ($running -contains $c) { continue }
    W "容器 $c 未运行，尝试启动"
    & docker start $c 2>&1 | Out-Null
}
$now = @(& docker ps --format "{{.Names}}" 2>$null)
$missing = @($need | Where-Object { $now -notcontains $_ })
if ($missing.Count -eq 0) { W "✅ 两个生产容器均在运行" }
else { W ("❌ 仍缺失: {0}" -f ($missing -join ', ')) }