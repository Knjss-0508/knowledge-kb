# 知识库中台启动脚本（沙箱权限内运行）
Write-Host "=== 答疑中台知识库 - 启动服务 ==="

# 1. 启动 Docker 容器（需要 Docker Desktop 已运行）
Write-Host "[1/3] 检查 Docker 容器..."
docker ps --format "{{.Names}}" | Out-Null

# 2. 杀掉旧的 Python 进程
Write-Host "[2/3] 清理旧进程..."
Get-Process python -ErrorAction SilentlyContinue | Stop-Process -Force -ErrorAction SilentlyContinue
Start-Sleep 2

# 3. 启动 FastAPI
Write-Host "[3/3] 启动 FastAPI 服务..."
$backendDir = "C:\Users\a1873\Documents\答疑中台知识库项目\backend"
Start-Process -FilePath "python" -ArgumentList "-m","uvicorn","app.main:app","--host","0.0.0.0","--port","8001" -WorkingDirectory $backendDir -WindowStyle Hidden

Start-Sleep 4

# 验证
try {
    $health = Invoke-RestMethod -Uri "http://localhost:8001/health" -TimeoutSec 3
    Write-Host "✓ 服务启动成功"
    Write-Host "  API 文档: http://localhost:8001/docs"
    Write-Host "  前端页面: http://localhost:8001/app"
} catch {
    Write-Host "✗ 服务启动失败: $($_.Exception.Message)"
}
