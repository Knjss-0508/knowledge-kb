$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$appPath = Join-Path $projectRoot "streamlit_app.py"

if (-not (Test-Path -LiteralPath $pythonPath)) {
    throw "Project Python was not found: $pythonPath. Follow START_HERE.md to install the environment."
}

if (-not (Test-Path -LiteralPath $appPath)) {
    throw "Streamlit entry point was not found: $appPath."
}

Set-Location -LiteralPath $projectRoot
Write-Host "Starting Answer Hub automation dashboard..." -ForegroundColor Cyan
Write-Host "Open: http://localhost:8501" -ForegroundColor Green
Write-Host "Keep this window open. Press Ctrl+C to stop." -ForegroundColor DarkGray

& $pythonPath -m streamlit run $appPath `
    --server.port 8501 `
    --server.address 127.0.0.1 `
    --server.headless false `
    --browser.gatherUsageStats false
