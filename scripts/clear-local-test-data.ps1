[CmdletBinding()]
param(
    [switch]$ConfirmDelete,
    [ValidateSet("cpu", "gpu")]
    [string]$Runtime = "cpu",
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

function Get-DotEnvValue([string]$Name) {
    if (-not (Test-Path ".env")) {
        return $null
    }
    $line = Get-Content ".env" |
        Where-Object { $_ -match "^$([regex]::Escape($Name))=" } |
        Select-Object -Last 1
    if ($line) {
        return $line.Substring($Name.Length + 1).Trim()
    }
    return $null
}

function Get-ConfiguredValue([string]$Name) {
    $processValue = [Environment]::GetEnvironmentVariable($Name)
    if ($processValue) {
        return $processValue.Trim()
    }
    return Get-DotEnvValue $Name
}

function Invoke-PostgresSql([string]$Sql) {
    & docker @ComposeArgs exec -T postgres sh -lc 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -P pager=off -c "$1"' sh $Sql
    if ($LASTEXITCODE -ne 0) {
        throw "PostgreSQL command failed."
    }
}

function Invoke-BackendDatabaseSql([string]$Sql) {
    # 通过与运行中 backend 相同的连接配置执行 SQL，避免因同名 Compose
    # 容器或不同工作目录而清到另一套本地 PostgreSQL。
    $encodedSql = [Convert]::ToBase64String(
        [System.Text.Encoding]::UTF8.GetBytes($Sql)
    )
    $python = @"
import base64
from app.core.database import engine

sql = base64.b64decode('$encodedSql').decode('utf-8')
with engine.begin() as connection:
    connection.exec_driver_sql(sql)
print('Backend database cleanup SQL completed.')
"@
    & docker @ComposeArgs run --rm --no-deps backend python -c $python
    if ($LASTEXITCODE -ne 0) {
        throw "Backend database command failed."
    }
}

function Assert-BackendKnowledgeDataCleared {
    $python = @'
from sqlalchemy import text
from app.core.database import engine

tables = (
    "knowledge_items",
    "knowledge_import_tasks",
    "knowledge_embeddings",
    "knowledge_search_embeddings",
    "knowledge_change_logs",
    "knowledge_deduplication_feedback",
    "knowledge_media",
    "knowledge_tags",
    "usage_stats",
    "integration_ingestions",
    "retrieval_quality_events",
    "media_upload_staging",
    "media_deletion_tasks",
)
query = " UNION ALL ".join(
    f"SELECT '{table}' AS table_name, count(*) AS row_count FROM {table}"
    for table in tables
)
with engine.connect() as connection:
    counts = dict(connection.execute(text(query)).all())
    sequence = connection.execute(
        text("SELECT last_value, is_called FROM knowledge_item_number_seq")
    ).one()

for table_name in tables:
    print(f"{table_name}: {counts[table_name]}")
print(f"knowledge_item_number_seq: last_value={sequence.last_value}, is_called={sequence.is_called}")

remaining = {
    table_name: row_count
    for table_name, row_count in counts.items()
    if row_count
}
if remaining:
    raise SystemExit(f"Active backend still has test data: {remaining}")
if sequence.last_value != 1 or sequence.is_called:
    raise SystemExit("Knowledge ID sequence was not reset to A-00001.")
'@
    & docker @ComposeArgs exec -T backend python -c $python
    if ($LASTEXITCODE -ne 0) {
        throw "Post-cleanup verification against the active backend failed."
    }
}

function Wait-ForBackendReady {
    for ($attempt = 1; $attempt -le 30; $attempt++) {
        & docker @ComposeArgs exec -T backend python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/ready', timeout=3).read()" *> $null
        if ($LASTEXITCODE -eq 0) {
            return
        }
        Start-Sleep -Seconds 2
    }
    throw "Local backend did not become ready within 60 seconds."
}

if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
    throw "Docker is required."
}

& docker compose version | Out-Null
if ($LASTEXITCODE -ne 0) {
    throw "Docker Compose v2 is required."
}

$databaseMode = Get-ConfiguredValue "DEPLOY_DATABASE_MODE"
if ($databaseMode -eq "cloud") {
    throw "Refusing to run: DEPLOY_DATABASE_MODE=cloud. This script is local-only."
}

$databaseUrl = Get-ConfiguredValue "DATABASE_URL"
if (
    $databaseUrl -and
    $databaseUrl -notmatch "@(postgres|localhost|127\.0\.0\.1)(:|/)"
) {
    throw "Refusing to run: DATABASE_URL does not point to a local Docker database."
}

$mediaBackend = Get-ConfiguredValue "MEDIA_STORAGE_BACKEND"
if ($mediaBackend -and $mediaBackend -ne "local") {
    throw "Refusing to run: MEDIA_STORAGE_BACKEND must be local."
}

$projectName = if ($env:COMPOSE_PROJECT_NAME) {
    $env:COMPOSE_PROJECT_NAME
} else {
    "knowledge-kb"
}
$overrideFile = "docker-compose.embedding-$Runtime.yml"
$ComposeArgs = @(
    "compose", "-p", $projectName,
    "-f", "docker-compose.yml",
    "-f", "docker-compose.local.yml",
    "-f", $overrideFile
)

& docker @ComposeArgs config --quiet
if ($LASTEXITCODE -ne 0) {
    throw "Local Docker Compose configuration is invalid."
}

$postgresContainer = (& docker @ComposeArgs ps -q postgres).Trim()
if (-not $postgresContainer) {
    throw "Local PostgreSQL is not running. Start the local stack first."
}

$activeMediaBackend = (& docker @ComposeArgs exec -T backend python -c "from app.core.config import settings; print(settings.MEDIA_STORAGE_BACKEND)").Trim()
if ($LASTEXITCODE -ne 0 -or $activeMediaBackend -ne "local") {
    throw "Refusing to run: the active backend media storage is not local."
}

$statsSql = @"
SELECT 'knowledge_items' AS table_name, count(*) AS row_count FROM knowledge_items
UNION ALL SELECT 'knowledge_import_tasks', count(*) FROM knowledge_import_tasks
UNION ALL SELECT 'knowledge_embeddings', count(*) FROM knowledge_embeddings
UNION ALL SELECT 'knowledge_search_embeddings', count(*) FROM knowledge_search_embeddings
UNION ALL SELECT 'knowledge_change_logs', count(*) FROM knowledge_change_logs
UNION ALL SELECT 'knowledge_deduplication_feedback', count(*) FROM knowledge_deduplication_feedback
UNION ALL SELECT 'knowledge_media', count(*) FROM knowledge_media
UNION ALL SELECT 'knowledge_tags', count(*) FROM knowledge_tags
UNION ALL SELECT 'usage_stats', count(*) FROM usage_stats
UNION ALL SELECT 'integration_ingestions', count(*) FROM integration_ingestions
UNION ALL SELECT 'retrieval_quality_events', count(*) FROM retrieval_quality_events
UNION ALL SELECT 'media_upload_staging', count(*) FROM media_upload_staging
UNION ALL SELECT 'media_deletion_tasks', count(*) FROM media_deletion_tasks
ORDER BY table_name;
"@

$uploadDir = Join-Path $Root "backend\uploads"
$uploadFiles = if (Test-Path $uploadDir) {
    @(Get-ChildItem -LiteralPath $uploadDir -File -Recurse -Force)
} else {
    @()
}

Write-Host "Local test-data cleanup target:"
Invoke-PostgresSql $statsSql
Write-Host "Local upload files: $($uploadFiles.Count)"
Write-Host "Preserved: users, user sessions, categories, tag dimensions, tag values, .env, Docker volumes."

if ($DryRun) {
    Write-Host "Dry run only. No data was deleted."
    exit 0
}

if (-not $ConfirmDelete) {
    throw "Refusing to delete. Re-run with -ConfirmDelete after reviewing the counts above."
}

$clearSql = @"
TRUNCATE TABLE
    knowledge_items,
    knowledge_import_tasks,
    knowledge_change_logs,
    knowledge_embeddings,
    knowledge_search_embeddings,
    knowledge_deduplication_feedback,
    knowledge_media,
    knowledge_tags,
    usage_stats,
    integration_ingestions,
    retrieval_quality_events,
    media_upload_staging,
    media_deletion_tasks
RESTART IDENTITY CASCADE;

-- knowledge_item_number_seq is an independent display-ID sequence, rather
-- than an IDENTITY owned by knowledge_items, so TRUNCATE cannot reset it.
SELECT setval('knowledge_item_number_seq', 1, false);
"@

$backendStopped = $false
try {
    Write-Host "Stopping local backend to prevent concurrent writes..."
    & docker @ComposeArgs stop backend
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to stop local backend."
    }
    $backendStopped = $true

    Write-Host "Clearing local knowledge business tables, vectors, and display-ID sequence..."
    Invoke-BackendDatabaseSql $clearSql

    foreach ($file in $uploadFiles) {
        Remove-Item -LiteralPath $file.FullName -Force
    }

    Write-Host "Starting local backend..."
    & docker @ComposeArgs up -d --no-deps backend
    if ($LASTEXITCODE -ne 0) {
        throw "Failed to start local backend."
    }
    $backendStopped = $false
    Wait-ForBackendReady
}
finally {
    if ($backendStopped) {
        Write-Warning "Cleanup stopped the local backend before completion; attempting to start it again."
        & docker @ComposeArgs up -d --no-deps backend *> $null
    }
}

Write-Host "Post-cleanup verification:"
Invoke-PostgresSql $statsSql
Write-Host "Active backend verification:"
Assert-BackendKnowledgeDataCleared
$remainingUploads = if (Test-Path $uploadDir) {
    @(Get-ChildItem -LiteralPath $uploadDir -File -Recurse -Force).Count
} else {
    0
}
Write-Host "Local upload files: $remainingUploads"
Write-Host "Local test data cleanup completed. The next knowledge ID will be A-00001."
