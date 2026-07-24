param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RetryFailed
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $ProjectRoot

$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            if (-not [Environment]::GetEnvironmentVariable($parts[0], "Process")) {
                [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
            }
        }
    }
}

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}

$queueDir = if ($env:ANSWER_HUB_AUTOMATION_QUEUE) {
    $env:ANSWER_HUB_AUTOMATION_QUEUE
} else {
    Join-Path $ProjectRoot "data\automation-queue"
}
$outputDir = if ($env:ANSWER_HUB_AUTOMATION_OUTPUT) {
    $env:ANSWER_HUB_AUTOMATION_OUTPUT
} else {
    Join-Path $ProjectRoot "outputs\automation-runs"
}
$maxFiles = if ($env:ANSWER_HUB_AUTOMATION_MAX_FILES) {
    $env:ANSWER_HUB_AUTOMATION_MAX_FILES
} else {
    "10"
}
$staleAfterSeconds = if ($env:ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS) {
    $env:ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS
} else {
    "7200"
}
$useMimo = $env:ANSWER_HUB_AUTOMATION_USE_MIMO -match "^(1|true|yes|on)$"
$syncToCzReviewValue = if ($env:ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW) {
    $env:ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW
} else {
    $env:ANSWER_HUB_AUTOMATION_SUBMIT_TO_CZ
}
$submitToCz = $syncToCzReviewValue -match "^(1|true|yes|on)$"
$clusteringMode = if ($env:ANSWER_HUB_AUTOMATION_CLUSTERING_MODE) {
    $env:ANSWER_HUB_AUTOMATION_CLUSTERING_MODE
} elseif ($useMimo) {
    "direct_mimo"
} else {
    "rule"
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$arguments = @(
    "-m",
    "answer_hub.cli",
    "automation-queue",
    "--queue-dir",
    $queueDir,
    "--output-dir",
    $outputDir,
    "--clustering-mode",
    $clusteringMode,
    "--max-files",
    $maxFiles,
    "--stale-after-seconds",
    $staleAfterSeconds
)

if ($env:ANSWER_HUB_AUTOMATION_STANDARDS) {
    $arguments += @("--standards", $env:ANSWER_HUB_AUTOMATION_STANDARDS)
}
if ($env:ANSWER_HUB_AUTOMATION_PRODUCT_TYPE) {
    $arguments += @("--product-type", $env:ANSWER_HUB_AUTOMATION_PRODUCT_TYPE)
}
if (-not $useMimo) {
    $arguments += "--rule-only"
}
if ($RetryFailed) {
    $arguments += "--retry-failed"
}
if ($submitToCz) {
    $arguments += "--sync-to-cz-review"
}

$logDir = Join-Path $ProjectRoot "outputs\automation-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir ("queue-" + (Get-Date -Format "yyyyMMdd") + ".log")

& $python @arguments *>> $logPath
exit $LASTEXITCODE
