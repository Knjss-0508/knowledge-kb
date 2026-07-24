param(
    [ValidateSet("cpu", "gpu")]
    [string]$Embedding = "cpu",
    [switch]$NoBuild
)

$ErrorActionPreference = "Stop"
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$czRoot = Join-Path $workspace "cz-knowledge-kb\knowledge-kb-master"
$envPath = Join-Path $czRoot ".env"

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker was not found. Install and start Docker Desktop first."
}
if (-not (Test-Path -LiteralPath $envPath)) {
    throw "Missing $envPath. Copy .env.example to .env and configure INTEGRATION_API_KEY."
}

$composeArgs = @("compose", "-f", "docker-compose.yml")
if ($Embedding -eq "gpu") {
    $composeArgs += @("-f", "docker-compose.embedding-gpu.yml")
}
$composeArgs += @("up", "-d")
if (-not $NoBuild) {
    $composeArgs += "--build"
}

Push-Location $czRoot
try {
    & docker @composeArgs
    if ($LASTEXITCODE -ne 0) {
        throw "CZ local services failed to start. Exit code: $LASTEXITCODE"
    }
    & docker compose ps
    Write-Host ""
    Write-Host "CZ started: http://127.0.0.1:8000"
    Write-Host "Qwen3 Embedding is running as the mandatory deduplication service."
} finally {
    Pop-Location
}
