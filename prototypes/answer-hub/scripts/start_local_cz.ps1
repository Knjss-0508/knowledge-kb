param(
    [ValidateSet("cpu", "gpu")]
    [string]$Embedding = "cpu",
    [switch]$NoBuild,
    [ValidateRange(1, 65535)]
    [int]$BackendPort = 8801
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

$composeArgs = @(
    "compose",
    "-f", "docker-compose.yml",
    "-f", "docker-compose.local.yml"
)
if ($Embedding -eq "gpu") {
    $composeArgs += @("-f", "docker-compose.embedding-gpu.yml")
} else {
    $composeArgs += @("-f", "docker-compose.embedding-cpu.yml")
}
$upArgs = $composeArgs + @("up", "-d")
if (-not $NoBuild) {
    $upArgs += "--build"
}

$backendPortWasSet = Test-Path Env:BACKEND_PORT
$previousBackendPort = $env:BACKEND_PORT
try {
    $env:BACKEND_PORT = [string]$BackendPort
    Push-Location $czRoot
    try {
        & docker @upArgs
        if ($LASTEXITCODE -ne 0) {
            throw "CZ local services failed to start. Exit code: $LASTEXITCODE"
        }
        $psArgs = $composeArgs + "ps"
        & docker @psArgs
        Write-Host ""
        Write-Host "CZ started: http://127.0.0.1:$BackendPort"
        Write-Host "Qwen3 Embedding is running as the mandatory deduplication service."
    } finally {
        Pop-Location
    }
} finally {
    if ($backendPortWasSet) {
        $env:BACKEND_PORT = $previousBackendPort
    } else {
        Remove-Item Env:BACKEND_PORT -ErrorAction SilentlyContinue
    }
}
