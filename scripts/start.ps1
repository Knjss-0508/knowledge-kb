# 答疑中台知识库 - 一键启动脚本
$ErrorActionPreference = "Stop"
$projectRoot = Split-Path -Parent $PSScriptRoot

Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  答疑中台知识库 - 环境启动" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan

# 1. 启动 Docker 容器
Write-Host "`n[1/3] 启动 Docker 容器..." -ForegroundColor Yellow
docker-compose -f "$projectRoot\docker-compose.yml" up -d

# 2. 等待 PostgreSQL 就绪
Write-Host "[2/3] 等待数据库就绪..." -ForegroundColor Yellow
$maxRetries = 30
$retries = 0
while ($retries -lt $maxRetries) {
    $result = docker exec kb-postgres pg_isready -U knowledge_admin 2>&1
    if ($LASTEXITCODE -eq 0) {
        Write-Host "  PostgreSQL 就绪!" -ForegroundColor Green
        break
    }
    $retries++
    Start-Sleep -Seconds 1
}
if ($retries -ge $maxRetries) {
    Write-Host "  PostgreSQL 启动超时" -ForegroundColor Red
    exit 1
}

# 3. 启动 FastAPI
Write-Host "[3/3] 启动后端服务..." -ForegroundColor Yellow
Set-Location "$projectRoot\backend"

Write-Host "`n========================================" -ForegroundColor Green
Write-Host "  启动 FastAPI 后端服务" -ForegroundColor Green
Write-Host "  API 文档: http://localhost:8001/docs" -ForegroundColor Green
Write-Host "  前端页面: http://localhost:8001/app" -ForegroundColor Green
Write-Host "  健康检查: http://localhost:8001/health" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""

uvicorn app.main:app --host 0.0.0.0 --port 8001 --reload