param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot)
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

if (-not $env:TRANSFER_MANHATTAN_PROFILE) {
    throw "TRANSFER_MANHATTAN_PROFILE is not configured."
}

$start = (Get-Date).Date.AddDays(-1).ToString("yyyy-MM-dd")
$end = (Get-Date).Date.ToString("yyyy-MM-dd")
$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$dbPath = if ($env:TRANSFER_ANALYSIS_DB_PATH) {
    $env:TRANSFER_ANALYSIS_DB_PATH
} else {
    "data/transfer_analysis.db"
}

& $python -m answer_hub.cli transfer-collect `
    --system manhattan `
    --endpoint-profile $env:TRANSFER_MANHATTAN_PROFILE `
    --start $start `
    --end $end `
    --db $dbPath

exit $LASTEXITCODE
