param(
    [string]$BackupRoot = "",
    [string]$ComposeProject = "knowledge-kb"
)

$ErrorActionPreference = "Stop"
$workspace = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$czRoot = Join-Path $workspace "cz-knowledge-kb\knowledge-kb-master"
$resolvedBackupRoot = if ($BackupRoot) {
    [System.IO.Path]::GetFullPath($BackupRoot)
} else {
    Join-Path $workspace "backups"
}
New-Item -ItemType Directory -Path $resolvedBackupRoot -Force | Out-Null
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$backupDir = Join-Path $resolvedBackupRoot "cz-$timestamp"
New-Item -ItemType Directory -Path $backupDir -Force | Out-Null

Push-Location $czRoot
try {
    $databaseFile = Join-Path $backupDir "knowledge_base.sql"
    docker compose -p $ComposeProject exec -T postgres sh -c `
        'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB"' |
        Set-Content -LiteralPath $databaseFile -Encoding UTF8
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL backup failed."
    }

    $uploads = Join-Path $czRoot "backend\uploads"
    if (Test-Path -LiteralPath $uploads) {
        Compress-Archive -Path (Join-Path $uploads "*") `
            -DestinationPath (Join-Path $backupDir "uploads.zip") `
            -CompressionLevel Optimal
    }
}
finally {
    Pop-Location
}

[ordered]@{
    created_at = (Get-Date).ToString("o")
    compose_project = $ComposeProject
    database = "knowledge_base.sql"
    uploads = if (Test-Path (Join-Path $backupDir "uploads.zip")) { "uploads.zip" } else { "" }
    contains_secrets = $false
} | ConvertTo-Json -Depth 4 |
    Set-Content -LiteralPath (Join-Path $backupDir "backup_manifest.json") -Encoding UTF8

Write-Host "Backup completed: $backupDir"
