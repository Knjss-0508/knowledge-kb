$ErrorActionPreference = "Stop"

$ProjectName = if ($env:COMPOSE_PROJECT_NAME) { $env:COMPOSE_PROJECT_NAME } else { "knowledge-kb" }
$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

if (-not (Test-Path ".env")) {
    Copy-Item ".env.example" ".env"
    Write-Host "Created .env from .env.example. Review it before production use."
}

docker compose -p $ProjectName up -d --build
docker compose -p $ProjectName ps
