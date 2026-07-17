param(
    [ValidateSet("cpu", "gpu")]
    [string]$EmbeddingMode = "gpu"
)

$ErrorActionPreference = "Stop"

$ProjectName = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { "knowledge-kb" }
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Review it before production use."
}

$EmbeddingComposeFile = "docker-compose.embedding-$EmbeddingMode.yml"
if (-not (Test-Path $EmbeddingComposeFile)) {
    throw "Embedding Compose file not found: $EmbeddingComposeFile"
}

docker compose -p $ProjectName -f docker-compose.yml -f $EmbeddingComposeFile up -d --build
docker compose -p $ProjectName -f docker-compose.yml -f $EmbeddingComposeFile ps
