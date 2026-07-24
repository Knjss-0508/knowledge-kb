param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$DifyVersion = "1.15.0",
    [int]$HttpPort = 8080,
    [int]$HttpsPort = 8443,
    [switch]$SkipStart
)

$ErrorActionPreference = "Stop"

function Set-EnvValue {
    param(
        [string]$Path,
        [string]$Name,
        [string]$Value
    )
    $lines = @()
    if (Test-Path -LiteralPath $Path) {
        $lines = @(Get-Content -LiteralPath $Path -Encoding UTF8)
    }
    $pattern = "^" + [regex]::Escape($Name) + "="
    $updated = $false
    $result = foreach ($line in $lines) {
        if ($line -match $pattern) {
            "$Name=$Value"
            $updated = $true
        } else {
            $line
        }
    }
    if (-not $updated) {
        $result += "$Name=$Value"
    }
    Set-Content -LiteralPath $Path -Value $result -Encoding UTF8
}

$toolsRoot = Join-Path $ProjectRoot "tools\dify"
$archivePath = Join-Path $toolsRoot "dify-$DifyVersion.zip"
$sourceRoot = Join-Path $toolsRoot "dify-$DifyVersion"
$dockerRoot = Join-Path $sourceRoot "docker"
New-Item -ItemType Directory -Force -Path $toolsRoot | Out-Null

if (-not (Test-Path -LiteralPath $dockerRoot)) {
    $downloadUrl = "https://github.com/langgenius/dify/archive/refs/tags/$DifyVersion.zip"
    Write-Host "Downloading official Dify $DifyVersion..." -ForegroundColor Cyan
    Invoke-WebRequest -Uri $downloadUrl -OutFile $archivePath
    Expand-Archive -LiteralPath $archivePath -DestinationPath $toolsRoot
}

$envExample = Join-Path $dockerRoot ".env.example"
$difyEnv = Join-Path $dockerRoot ".env"
if (-not (Test-Path -LiteralPath $envExample)) {
    throw "Dify Docker environment template was not found: $envExample"
}
if (-not (Test-Path -LiteralPath $difyEnv)) {
    Copy-Item -LiteralPath $envExample -Destination $difyEnv
}

Set-EnvValue -Path $difyEnv -Name "EXPOSE_NGINX_PORT" -Value "$HttpPort"
Set-EnvValue -Path $difyEnv -Name "EXPOSE_NGINX_SSL_PORT" -Value "$HttpsPort"
Set-EnvValue `
    -Path $difyEnv `
    -Name "SSRF_PROXY_ALLOW_PRIVATE_DOMAINS" `
    -Value "host.docker.internal"

Write-Host "Dify configuration prepared: $dockerRoot" -ForegroundColor Green
Write-Host "Console URL: http://localhost:$HttpPort" -ForegroundColor Green

if ($SkipStart) {
    exit 0
}

docker info | Out-Null
Push-Location $dockerRoot
try {
    docker compose up -d
} finally {
    Pop-Location
}

$installUrl = "http://127.0.0.1:$HttpPort/install"
Write-Host "Dify containers started. Waiting for the install page..." -ForegroundColor Cyan
$ready = $false
for ($attempt = 1; $attempt -le 60; $attempt++) {
    try {
        $response = Invoke-WebRequest `
            -UseBasicParsing `
            -Uri $installUrl `
            -TimeoutSec 5
        if ($response.StatusCode -ge 200 -and $response.StatusCode -lt 500) {
            $ready = $true
            break
        }
    } catch {
        Start-Sleep -Seconds 5
    }
}
if (-not $ready) {
    throw (
        "Dify containers were started, but the install page did not become " +
        "available at $installUrl. Open Docker Desktop and inspect the " +
        "dify containers for an unhealthy or restarting service."
    )
}

Write-Host "Dify is ready." -ForegroundColor Green
Write-Host "Open http://localhost:$HttpPort/install to create the administrator account." -ForegroundColor Cyan
