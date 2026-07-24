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

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}
if (-not $env:ANSWER_HUB_API_KEY) {
    $bytes = New-Object byte[] 32
    $generator = [System.Security.Cryptography.RandomNumberGenerator]::Create()
    try {
        $generator.GetBytes($bytes)
    } finally {
        $generator.Dispose()
    }
    $generatedKey = [Convert]::ToBase64String($bytes)
    if (-not (Test-Path -LiteralPath $envFile)) {
        New-Item -ItemType File -Path $envFile -Force | Out-Null
    }
    Add-Content `
        -LiteralPath $envFile `
        -Value "`nANSWER_HUB_API_KEY=$generatedKey" `
        -Encoding UTF8
    [Environment]::SetEnvironmentVariable(
        "ANSWER_HUB_API_KEY",
        $generatedKey,
        "Process"
    )
    Write-Host "Generated ANSWER_HUB_API_KEY and saved it to the local .env file." -ForegroundColor Yellow
}

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
$hostName = if ($env:ANSWER_HUB_API_HOST) {
    $env:ANSWER_HUB_API_HOST
} else {
    "0.0.0.0"
}
$port = if ($env:ANSWER_HUB_API_PORT) {
    $env:ANSWER_HUB_API_PORT
} else {
    "8780"
}

Write-Host "Starting Answer Hub automation API..." -ForegroundColor Cyan
Write-Host "Health: http://127.0.0.1:$port/health" -ForegroundColor Green
Write-Host "Dify URL: http://host.docker.internal:$port" -ForegroundColor Green

$env:ANSWER_HUB_API_HOST = $hostName
$env:ANSWER_HUB_API_PORT = $port
& $python -m answer_hub.automation_api
exit $LASTEXITCODE
