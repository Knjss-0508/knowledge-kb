param(
    [int]$TimeoutSeconds = $(if ($env:DEPLOY_TIMEOUT_SECONDS) { [int]$env:DEPLOY_TIMEOUT_SECONDS } else { 900 })
)

# Deploys the GPU embedding node for the split topology:
#   the model runs here, while the application backend and database stay on
#   the remote server. See docs/gpu-embedding-tunnel.md.
#
# This script intentionally does NOT start backend/migrate/redis: in this
# topology the GPU node must never run a database or a second writer.
#
# Messages are ASCII-only on purpose: this file may be read by Windows
# PowerShell 5.1, whose default encoding is the system ANSI code page.

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required. Install Docker Desktop before deploying."
}

& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required."
}

if (-not (Test-Path ".env")) {
    throw ".env not found. Copy .env.example to .env and fill in the values."
}

function Get-EnvValue([string]$Name) {
    $match = Select-String -Path ".env" -Pattern "^$([regex]::Escape($Name))=" -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if (-not $match) { return "" }
    return ($match.Line -split "=", 2)[1].Trim()
}

function Assert-Set([string]$Name, [string]$Value) {
    if ([string]::IsNullOrWhiteSpace($Value) -or $Value -like "replace-with-*") {
        throw "$Name must be set in .env for the split topology. See docs/gpu-embedding-tunnel.md."
    }
}

$sshHost = Get-EnvValue "EMBEDDING_TUNNEL_SSH_HOST"
$keyPath = Get-EnvValue "EMBEDDING_TUNNEL_KEY_PATH"
$remotePort = Get-EnvValue "EMBEDDING_TUNNEL_REMOTE_PORT"
if ([string]::IsNullOrWhiteSpace($remotePort)) { $remotePort = "18080" }

Assert-Set "EMBEDDING_TUNNEL_SSH_HOST" $sshHost
Assert-Set "EMBEDDING_TUNNEL_KEY_PATH" $keyPath

if (-not (Test-Path $keyPath)) {
    throw "EMBEDDING_TUNNEL_KEY_PATH does not exist: $keyPath"
}

$dimensions = Get-EnvValue "EMBEDDING_DIMENSIONS"
if ($dimensions -ne "1024") {
    throw "EMBEDDING_DIMENSIONS must be 1024, got '$dimensions'. The remote database vectors are 1024-dimensional."
}

$model = Get-EnvValue "EMBEDDING_MODEL"
if ([string]::IsNullOrWhiteSpace($model)) { $model = "Qwen/Qwen3-Embedding-0.6B" }

Write-Host "GPU embedding node deployment"
Write-Host "  model            : $model"
Write-Host "  dimensions       : $dimensions"
Write-Host "  tunnel target    : ${sshHost}:${remotePort} -> embedding-qwen:80"
Write-Host "  private key      : $keyPath"
Write-Host ""

Write-Host "[1/4] Starting embedding service and tunnel..."
& docker compose `
    -f docker-compose.yml `
    -f docker-compose.embedding-gpu.yml `
    -f docker-compose.embedding-tunnel.yml `
    up -d embedding-qwen embedding-tunnel
if ($LASTEXITCODE -ne 0) {
    throw "docker compose up failed with exit code $LASTEXITCODE."
}

Write-Host ""
Write-Host "[2/4] Waiting for the embedding service to become healthy (model load can take minutes)..."
$deadline = (Get-Date).AddSeconds($TimeoutSeconds)
$healthy = $false
while ((Get-Date) -lt $deadline) {
    $status = & docker inspect kb-embedding-qwen --format "{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}" 2>$null
    if ($status -eq "healthy") { $healthy = $true; break }
    if ($status -eq "unhealthy") { throw "kb-embedding-qwen reported unhealthy. Check: docker logs kb-embedding-qwen" }
    Start-Sleep -Seconds 5
}
if (-not $healthy) {
    throw "Timed out after $TimeoutSeconds seconds waiting for kb-embedding-qwen to become healthy."
}
Write-Host "  kb-embedding-qwen is healthy."

Write-Host ""
Write-Host "[3/4] Checking the tunnel container..."
$tunnelStatus = & docker inspect kb-embedding-tunnel --format "{{.State.Status}}" 2>$null
if ($tunnelStatus -ne "running") {
    Write-Warning "kb-embedding-tunnel status is '$tunnelStatus'."
    Write-Warning "Check: docker logs kb-embedding-tunnel"
    Write-Warning "A common cause is GatewayPorts not enabled on the remote sshd."
} else {
    Write-Host "  kb-embedding-tunnel is running."
}

Write-Host ""
Write-Host "[4/4] Verifying the embedding endpoint locally..."
# TEI answers /health with HTTP 200 and an EMPTY body, so the exit code is the
# only reliable signal. Do not test the response body here.
& docker exec kb-embedding-qwen curl --fail --silent --max-time 10 http://127.0.0.1:80/health *> $null
if ($LASTEXITCODE -ne 0) {
    throw "The embedding service did not answer /health inside its container (curl exit code $LASTEXITCODE)."
}
Write-Host "  local /health answered with HTTP 200."

Write-Host ""
Write-Host "Deployment finished."
Write-Host ""
Write-Host "Verify from the remote server (the application backend host):"
Write-Host "  ss -tlnp | grep $remotePort"
Write-Host "  curl -s http://127.0.0.1:${remotePort}/health"
Write-Host ""
Write-Host "Then confirm the backend uses this node:"
Write-Host "  curl -s http://127.0.0.1:8000/ready"
Write-Host ""
Write-Host "Decisive check - stop the remote CPU embedding container and confirm"
Write-Host "embeddings still work; see docs/gpu-embedding-tunnel.md section 4.3."
