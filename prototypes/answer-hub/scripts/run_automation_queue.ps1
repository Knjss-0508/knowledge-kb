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
    $systemPython = Get-Command python.exe -ErrorAction SilentlyContinue
    if (-not $systemPython) {
        throw "Project Python was not found: $python; system python.exe is also unavailable."
    }
    $python = $systemPython.Source
    Write-Output "Project virtualenv not found; using system Python: $python"
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
$secondPartPullProfile = $env:SECOND_PART_PULL_PROFILE
$secondPartPullState = if ($env:SECOND_PART_PULL_STATE) {
    $env:SECOND_PART_PULL_STATE
} else {
    Join-Path $ProjectRoot "data\second-part-pull\state.json"
}
$secondPartPullMaxPages = if ($env:SECOND_PART_PULL_MAX_PAGES) {
    $env:SECOND_PART_PULL_MAX_PAGES
} else {
    "10"
}
$automationPlanPath = if ($env:ANSWER_HUB_AUTOMATION_PLAN_PATH) {
    $env:ANSWER_HUB_AUTOMATION_PLAN_PATH
} else {
    Join-Path $ProjectRoot "data\automation-plan.json"
}
$automationPlan = $null
if (Test-Path -LiteralPath $automationPlanPath) {
    try {
        $automationPlan = Get-Content -Raw -Encoding UTF8 $automationPlanPath | ConvertFrom-Json
    } catch {
        throw "无法读取执行计划文件：$automationPlanPath"
    }
}
$secondPartQueryFromDate = $env:SECOND_PART_QUERY_FROM_DATE
$secondPartQueryToDate = $env:SECOND_PART_QUERY_TO_DATE
$planFromDate = ""
$planToDate = ""
if ($automationPlan) {
    $planFromDate = [string]$automationPlan.knowledge_settle_from_date
    $planToDate = [string]$automationPlan.knowledge_settle_to_date
    if (-not $planFromDate) {
        $planFromDate = [string]$automationPlan.second_part_query_from_date
    }
    if (-not $planToDate) {
        $planToDate = [string]$automationPlan.second_part_query_to_date
    }
}
if ($planFromDate -or $planToDate) {
    $secondPartQueryFromDate = $planFromDate
    $secondPartQueryToDate = $planToDate
}
$legacySecondPartQueryDate = $env:SECOND_PART_QUERY_DATE
if ($secondPartQueryFromDate -or $secondPartQueryToDate) {
    if (-not $secondPartQueryFromDate -or -not $secondPartQueryToDate) {
        throw (
            "SECOND_PART_QUERY_FROM_DATE and " +
            "SECOND_PART_QUERY_TO_DATE must be set together."
        )
    }
} elseif ($legacySecondPartQueryDate) {
    $secondPartQueryFromDate = $legacySecondPartQueryDate
    $secondPartQueryToDate = $legacySecondPartQueryDate
} else {
    $secondPartQueryWindowDays = 1
    if ($env:SECOND_PART_QUERY_WINDOW_DAYS) {
        try {
            $secondPartQueryWindowDays = [int]$env:SECOND_PART_QUERY_WINDOW_DAYS
        } catch {
            throw "SECOND_PART_QUERY_WINDOW_DAYS must be a positive integer."
        }
    }
    if ($secondPartQueryWindowDays -lt 1) {
        throw "SECOND_PART_QUERY_WINDOW_DAYS must be at least 1."
    }
    $lastCompleteDate = (Get-Date).Date.AddDays(-1)
    $secondPartQueryFromDate = $lastCompleteDate.AddDays(
        1 - $secondPartQueryWindowDays
    ).ToString("yyyy-MM-dd")
    $secondPartQueryToDate = $lastCompleteDate.ToString("yyyy-MM-dd")
}
$env:SECOND_PART_QUERY_FROM_DATE = $secondPartQueryFromDate
$env:SECOND_PART_QUERY_TO_DATE = $secondPartQueryToDate
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

$pullExitCode = 0
if ($secondPartPullProfile) {
    $pullArguments = @(
        "-m",
        "answer_hub.cli",
        "second-part-pull",
        "--profile",
        $secondPartPullProfile,
        "--queue-dir",
        $queueDir,
        "--output-dir",
        $outputDir,
        "--state-file",
        $secondPartPullState,
        "--max-pages",
        $secondPartPullMaxPages
    )
    & $python @pullArguments *>> $logPath
    $pullExitCode = $LASTEXITCODE
}

& $python @arguments *>> $logPath
$queueExitCode = $LASTEXITCODE
if ($queueExitCode -ne 0) {
    exit $queueExitCode
}
exit $pullExitCode
