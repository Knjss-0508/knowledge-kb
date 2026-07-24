param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$TaskName = "AnswerHubAutomationQueue",
    [ValidateRange(1, 1440)]
    [int]$IntervalMinutes = 5,
    [switch]$Uninstall
)

$ErrorActionPreference = "Stop"
$ProjectRoot = [System.IO.Path]::GetFullPath(
    $ProjectRoot.Trim().Trim([char]34)
)

if ($Uninstall) {
    & schtasks.exe /Query /TN $TaskName *> $null
    if ($LASTEXITCODE -eq 0) {
        & schtasks.exe /Delete /TN $TaskName /F | Out-Null
        if ($LASTEXITCODE -ne 0) {
            throw "Failed to remove scheduled task: $TaskName"
        }
        Write-Host "Removed scheduled task: $TaskName" -ForegroundColor Yellow
    } else {
        Write-Host "Scheduled task does not exist: $TaskName" -ForegroundColor DarkGray
    }
    exit 0
}

$runner = Join-Path $ProjectRoot "scripts\run_automation_queue_hidden.vbs"
if (-not (Test-Path -LiteralPath $runner)) {
    throw "Hidden automation runner was not found: $runner"
}

$taskCommand = "wscript.exe `"$runner`""
& schtasks.exe `
    /Create `
    /TN $TaskName `
    /TR $taskCommand `
    /SC MINUTE `
    /MO $IntervalMinutes `
    /RL LIMITED `
    /F | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw (
        "Failed to create scheduled task. Open PowerShell as the current " +
        "Windows user and run this script again."
    )
}

Write-Host "Installed scheduled task: $TaskName" -ForegroundColor Green
Write-Host "Interval: every $IntervalMinutes minute(s)" -ForegroundColor Cyan
Write-Host "Inbox: $(Join-Path $ProjectRoot 'data\automation-queue\pending')" -ForegroundColor Cyan
