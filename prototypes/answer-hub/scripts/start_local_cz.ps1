param(
    [ValidateSet("cpu", "gpu")]
    [string]$Embedding = "cpu",
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$czRoot = Join-Path $workspace "cz-knowledge-kb\knowledge-kb-master"
$envPath = Join-Path $czRoot ".env"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "未检测到 Docker。请先安装并启动 Docker Desktop。"
}
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "缺少 $envPath。请先复制 .env.example 为 .env，并配置 INTEGRATION_API_KEY 与模型参数。"
}

$composeArgs = @("compose", "-f", "docker-compose.yml")
if ($Embedding -eq "gpu") {
    $composeArgs += @("-f", "docker-compose.embedding-gpu.yml")
}
$composeArgs += @("up", "-d")
if (-not $NoBuild) {
    $composeArgs += "--build"
}

Push-Location $czRoot
try {
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "CZ 本地服务启动失败，退出码：$LASTEXITCODE"
    }
    & docker compose ps
    Write-Host ""
    Write-Host "CZ 已启动：http://127.0.0.1:8000"
    Write-Host "Qwen3 Embedding 已作为批量导入查重拦截器启动。"
} finally {
    Pop-Location
}
