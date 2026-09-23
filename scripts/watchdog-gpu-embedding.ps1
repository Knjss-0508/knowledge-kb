param(
    [string]$LogPath = (Join-Path (Split-Path -Parent $PSScriptRoot) "logs\gpu-embedding-watchdog.log"),
    [int]$StartupWaitSeconds = 300,
    [int]$MaxLogLines = 2000
)

# Watchdog for the split topology GPU node.
#
# Why this exists: if Docker Desktop is not running (for example after a reboot
# that nobody logged into, or after an engine crash), the SSH reverse tunnel and
# the embedding service both disappear. The remote backend then cannot embed
# queries, so knowledge import and query-embedding-dependent recall fail while
# the rest of the website still looks healthy. This script restores both.
#
# Register it as a scheduled task (see docs/gpu-embedding-tunnel.md section 6):
#   trigger  : at logon, then repeat every 5 minutes indefinitely
#   action   : powershell.exe -NoProfile -ExecutionPolicy Bypass -File <this file>
#   run as   : the logged-on user (no stored password required)
#
# Messages are ASCII-only on purpose: this file may be read by Windows
# PowerShell 5.1, whose default encoding is the system ANSI code page.

$ErrorActionPreference = "Continue"
$Root = Split-Path -Parent $PSScriptRoot
$Services = @("embedding-qwen", "embedding-tunnel")
$Containers = @("kb-embedding-qwen", "kb-embedding-tunnel")

function Write-Log([string]$Message) {
    $line = "{0} {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $Message
    Write-Host $line
    try {
        $dir = Split-Path -Parent $LogPath
        if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Force -Path $dir | Out-Null }
        Add-Content -Path $LogPath -Value $line -Encoding UTF8
        $count = (Get-Content $LogPath -ErrorAction SilentlyContinue | Measure-Object).Count
        if ($count -gt $MaxLogLines) {
            $keep = Get-Content $LogPath | Select-Object -Last ([int]($MaxLogLines / 2))
            Set-Content -Path $LogPath -Value $keep -Encoding UTF8
        }
    } catch {
        Write-Warning "Could not write the watchdog log: $($_.Exception.Message)"
    }
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    Write-Log "ERROR docker CLI not found on PATH; cannot recover the GPU embedding node."
    exit 1
}

# 1) Is the engine reachable?
& docker info *> $null
if ($LASTEXITCODE -ne 0) {
    Write-Log "Docker engine is not responding; asking Docker Desktop to start."
    & docker desktop start *> $null
    $deadline = (Get-Date).AddSeconds($StartupWaitSeconds)
    $ready = $false
    while ((Get-Date) -lt $deadline) {
        Start-Sleep -Seconds 10
        & docker info *> $null
        if ($LASTEXITCODE -eq 0) { $ready = $true; break }
    }
    if (-not $ready) {
        Write-Log "ERROR Docker engine did not become ready within $StartupWaitSeconds seconds. If nobody is logged into this machine, Docker Desktop cannot start - enable automatic sign-in on this host."
        exit 1
    }
    Write-Log "Docker engine is ready again."
}

# 2) Are both containers running?
$unhealthy = @()
foreach ($name in $Containers) {
    $state = & docker inspect $name --format "{{.State.Status}}" 2>$null
    if ($LASTEXITCODE -ne 0) {
        $unhealthy += "$name(missing)"
    } elseif ($state -ne "running") {
        $unhealthy += "$name($state)"
    }
}

if ($unhealthy.Count -eq 0) {
    Write-Log "OK both containers are running."
    exit 0
}

Write-Log "Recovering: $($unhealthy -join ', ')"
Push-Location $Root
try {
    & docker compose `
        -f docker-compose.yml `
        -f docker-compose.embedding-gpu.yml `
        -f docker-compose.embedding-tunnel.yml `
        up -d @Services *> $null
    $code = $LASTEXITCODE
} finally {
    Pop-Location
}

if ($code -ne 0) {
    Write-Log "ERROR docker compose up exited with code $code. Check .env and the tunnel settings."
    exit 1
}

Start-Sleep -Seconds 15
$stillBad = @()
foreach ($name in $Containers) {
    $state = & docker inspect $name --format "{{.State.Status}}" 2>$null
    if ($LASTEXITCODE -ne 0 -or $state -ne "running") { $stillBad += "$name($state)" }
}
if ($stillBad.Count -eq 0) {
    Write-Log "Recovered successfully; both containers are running."
    exit 0
}

Write-Log "ERROR still not running after recovery: $($stillBad -join ', ')"
exit 1
