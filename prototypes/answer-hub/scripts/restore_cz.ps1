param(
    [Parameter(Mandatory = $true)]
    [string]$BackupDirectory,
    [switch]$ConfirmRestore,
    [string]$ComposeProject = "knowledge-kb"
)

$ErrorActionPreference = "Stop"
if (-not $ConfirmRestore) {
    throw "Restore is destructive. Re-run with -ConfirmRestore after confirming the target environment."
}

$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$czRoot = Join-Path $workspace "cz-knowledge-kb\knowledge-kb-master"
$resolvedBackup = (Resolve-Path -LiteralPath $BackupDirectory).Path
$databaseFile = Join-Path $resolvedBackup "knowledge_base.sql"
$manifestFile = Join-Path $resolvedBackup "backup_manifest.json"
if (-not (Test-Path -LiteralPath $databaseFile -PathType Leaf)) {
    throw "Database backup not found: $databaseFile"
}
if (-not (Test-Path -LiteralPath $manifestFile -PathType Leaf)) {
    throw "Backup manifest not found: $manifestFile"
}

Push-Location $czRoot
try {
    Get-Content -LiteralPath $databaseFile -Raw -Encoding UTF8 |
        docker compose -p $ComposeProject exec -T postgres sh -c `
            'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"'
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL restore failed."
    }

    $uploadsArchive = Join-Path $resolvedBackup "uploads.zip"
    if (Test-Path -LiteralPath $uploadsArchive -PathType Leaf) {
        $uploads = Join-Path $czRoot "backend\uploads"
        New-Item -ItemType Directory -Path $uploads -Force | Out-Null
        Expand-Archive -LiteralPath $uploadsArchive -DestinationPath $uploads -Force
    }
}
finally {
    Pop-Location
}

Write-Host "Restore completed from: $resolvedBackup"
