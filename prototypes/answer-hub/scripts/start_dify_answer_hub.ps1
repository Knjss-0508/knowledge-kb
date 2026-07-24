param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DifyVersion = "1.15.0",
    [int]$DifyPort = 8080,
    [int]$ApiPort = 8780,
    [int]$QueueIntervalMinutes = 1
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $ProjectRoot

$installDify = Join-Path $ProjectRoot "scripts\install_dify_local.ps1"
$startApi = Join-Path $ProjectRoot "scripts\start_automation_api.ps1"
$installTask = Join-Path $ProjectRoot "scripts\install_automation_task.ps1"

& powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $installDify `
    -ProjectRoot $ProjectRoot `
    -DifyVersion $DifyVersion `
    -HttpPort $DifyPort
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$apiListening = Get-NetTCPConnection `
    -State Listen `
    -LocalPort $ApiPort `
    -ErrorAction SilentlyContinue
if (-not $apiListening) {
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$startApi`" " +
        "-ProjectRoot `"$ProjectRoot`""
    )
    Start-Process `
        -FilePath "powershell.exe" `
        -ArgumentList $arguments `
        -WorkingDirectory $ProjectRoot `
        -WindowStyle Hidden
    Start-Sleep -Seconds 3
}

& powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $installTask `
    -ProjectRoot $ProjectRoot `
    -IntervalMinutes $QueueIntervalMinutes
if ($LASTEXITCODE -ne 0) {
    exit $LASTEXITCODE
}

$healthUrl = "http://127.0.0.1:$ApiPort/health"
try {
    $health = Invoke-RestMethod -Uri $healthUrl -TimeoutSec 10
    if ($health.status -ne "ok") {
        throw "Unexpected API health status."
    }
} catch {
    throw "Answer Hub API did not become healthy at $healthUrl. $($_.Exception.Message)"
}

Write-Host ""
Write-Host "Dify + Answer Hub is ready." -ForegroundColor Green
Write-Host "Dify console: http://localhost:$DifyPort" -ForegroundColor Cyan
Write-Host "Answer Hub API: $healthUrl" -ForegroundColor Cyan
Write-Host "Import tool: config\dify-answer-hub-openapi.json" -ForegroundColor Cyan
