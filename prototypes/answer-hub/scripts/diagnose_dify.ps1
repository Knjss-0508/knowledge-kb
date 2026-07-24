param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DifyVersion = "1.15.0"
)

$ErrorActionPreference = "Continue"
$dockerRoot = Join-Path $ProjectRoot "tools\dify\dify-$DifyVersion\docker"
$outputDir = Join-Path $ProjectRoot "outputs"
$outputPath = Join-Path $outputDir "dify-diagnostics.txt"
New-Item -ItemType Directory -Force -Path $outputDir | Out-Null

if (-not (Test-Path -LiteralPath $dockerRoot)) {
    throw "Dify Docker directory was not found: $dockerRoot"
}

Set-Content `
    -LiteralPath $outputPath `
    -Encoding UTF8 `
    -Value @(
        "Dify diagnostics",
        "Generated: $((Get-Date).ToString('s'))",
        "Docker root: $dockerRoot",
        ""
    )

Push-Location $dockerRoot
try {
    "=== docker version ===" | Tee-Object -FilePath $outputPath -Append
    & docker version 2>&1 | Tee-Object -FilePath $outputPath -Append

    "`n=== docker compose ps -a ===" | Tee-Object -FilePath $outputPath -Append
    & docker compose ps -a 2>&1 | Tee-Object -FilePath $outputPath -Append

    "`n=== nginx, web and api logs ===" | Tee-Object -FilePath $outputPath -Append
    & docker compose logs `
        --tail 200 `
        nginx `
        web `
        api 2>&1 | Tee-Object -FilePath $outputPath -Append

    "`n=== supporting service logs ===" | Tee-Object -FilePath $outputPath -Append
    & docker compose logs `
        --tail 100 `
        db `
        redis `
        plugin_daemon `
        sandbox `
        ssrf_proxy 2>&1 | Tee-Object -FilePath $outputPath -Append
} finally {
    Pop-Location
}

Write-Host ""
Write-Host "Diagnostics written to:" -ForegroundColor Green
Write-Host $outputPath -ForegroundColor Cyan
