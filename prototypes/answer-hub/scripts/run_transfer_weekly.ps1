param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [int]$SampleSize = 350
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

foreach ($name in @(
    "TRANSFER_MANHATTAN_PROFILE",
    "TRANSFER_BAIXIAOSHENG_PROFILE",
    "TRANSFER_KB_CATALOG_PATH"
)) {
    if (-not [Environment]::GetEnvironmentVariable($name, "Process")) {
        throw "$name is not configured."
    }
}

$today = (Get-Date).Date
$currentMonday = $today.AddDays(-(([int]$today.DayOfWeek + 6) % 7))
$weekStart = $currentMonday.AddDays(-7).ToString("yyyy-MM-dd")
$outputDir = Join-Path $ProjectRoot "outputs\transfer-analysis\$weekStart"
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$dbPath = if ($env:TRANSFER_ANALYSIS_DB_PATH) {
    $env:TRANSFER_ANALYSIS_DB_PATH
} else {
    "data/transfer_analysis.db"
}

& $python -m answer_hub.cli transfer-analyze `
    --week-start $weekStart `
    --standards $env:TRANSFER_KB_CATALOG_PATH `
    --output-dir $outputDir `
    --sample-size $SampleSize `
    --manhattan-profile $env:TRANSFER_MANHATTAN_PROFILE `
    --baixiaosheng-profile $env:TRANSFER_BAIXIAOSHENG_PROFILE `
    --db $dbPath

exit $LASTEXITCODE
