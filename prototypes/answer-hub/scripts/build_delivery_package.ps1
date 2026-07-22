param(
    [string]$OutputRoot = "handoff",
    [string]$Version = (Get-Date -Format "yyyyMMdd-HHmmss")
)

$ErrorActionPreference = "Stop"
$workspace = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot ".."))
$outputBase = [System.IO.Path]::GetFullPath((Join-Path $workspace $OutputRoot))
if (-not $outputBase.StartsWith($workspace, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "输出目录必须位于项目工作区内：$workspace"
}

$packageName = "answer-hub-delivery-$Version"
$packageDir = Join-Path $outputBase $packageName
$zipPath = "$packageDir.zip"
if ((Test-Path -LiteralPath $packageDir) -or (Test-Path -LiteralPath $zipPath)) {
    throw "交付目标已存在，请更换 Version：$packageName"
}

$excludedDirectories = @(
    "__pycache__",
    ".pytest_cache",
    ".git",
    ".venv",
    ".cz_test_venv",
    "node_modules",
    "outputs",
    "data",
    "uploads"
)
$excludedFileNames = @(".env")
$excludedExtensions = @(
    ".db",
    ".sqlite",
    ".sqlite3",
    ".log",
    ".xlsx",
    ".xls",
    ".csv",
    ".zip",
    ".pkl",
    ".npy",
    ".npz",
    ".pem",
    ".key"
)

function Test-SafeDeliveryFile {
    param([System.IO.FileInfo]$File)
    if ($excludedFileNames -contains $File.Name) {
        return $false
    }
    if ($excludedExtensions -contains $File.Extension.ToLowerInvariant()) {
        return $false
    }
    foreach ($part in $File.FullName.Split([System.IO.Path]::DirectorySeparatorChar)) {
        if ($excludedDirectories -contains $part) {
            return $false
        }
    }
    return $true
}

function Copy-SafeTree {
    param(
        [string]$SourceRelative,
        [string]$DestinationRelative
    )
    $source = [System.IO.Path]::GetFullPath((Join-Path $workspace $SourceRelative))
    if (-not (Test-Path -LiteralPath $source)) {
        return
    }
    $destinationRoot = Join-Path $packageDir $DestinationRelative
    foreach ($file in Get-ChildItem -LiteralPath $source -Recurse -File -Force) {
        if (-not (Test-SafeDeliveryFile $file)) {
            continue
        }
        $relative = $file.FullName.Substring($source.Length).TrimStart("\")
        $destination = Join-Path $destinationRoot $relative
        $destinationParent = Split-Path -Parent $destination
        New-Item -ItemType Directory -Path $destinationParent -Force | Out-Null
        Copy-Item -LiteralPath $file.FullName -Destination $destination
    }
}

New-Item -ItemType Directory -Path $packageDir -Force | Out-Null

$rootFiles = @(
    ".dockerignore",
    ".env.example",
    ".gitignore",
    "pyproject.toml",
    "README.md",
    "START_HERE.md",
    "automation-api-reference.md",
    "CZ_INTEGRATION_RUNBOOK.md",
    "DELIVERY_GUIDE.md",
    "USER_OPERATIONS_GUIDE.md",
    "ACCEPTANCE_CHECKLIST.md",
    "TRANSFER_ANALYSIS.md",
    "streamlit_app.py",
    "start_streamlit.ps1"
)
foreach ($relative in $rootFiles) {
    $source = Join-Path $workspace $relative
    if (Test-Path -LiteralPath $source) {
        Copy-Item -LiteralPath $source -Destination (Join-Path $packageDir $relative)
    }
}

# Copy root launchers by extension so Windows PowerShell 5 does not depend on
# decoding Chinese filename literals from this script.
foreach ($launcher in Get-ChildItem -LiteralPath $workspace -File -Filter "*.cmd") {
    Copy-Item -LiteralPath $launcher.FullName -Destination (Join-Path $packageDir $launcher.Name)
}

Copy-SafeTree "src\answer_hub" "src\answer_hub"
Copy-SafeTree "tests" "tests"
Copy-SafeTree "scripts" "scripts"
Copy-SafeTree "examples" "examples"
Copy-SafeTree "config" "config"

$handoffFiles = @(
    "handoff\docker-compose.embedding-cpu.yml",
    "handoff\docker-compose.embedding-cpu-offline.yml",
    "handoff\docker-compose.embedding-gpu.yml",
    "handoff\setup_home.ps1",
    "handoff\start_streamlit.ps1"
)
foreach ($relative in $handoffFiles) {
    $source = Join-Path $workspace $relative
    if (Test-Path -LiteralPath $source) {
        $destination = Join-Path $packageDir $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }
}

$czRoot = "cz-knowledge-kb\knowledge-kb-master"
Copy-SafeTree "$czRoot\backend\app" "$czRoot\backend\app"
Copy-SafeTree "$czRoot\backend\migrations" "$czRoot\backend\migrations"
Copy-SafeTree "$czRoot\backend\tests" "$czRoot\backend\tests"
Copy-SafeTree "$czRoot\docs" "$czRoot\docs"
Copy-SafeTree "$czRoot\frontend" "$czRoot\frontend"
Copy-SafeTree "$czRoot\scripts" "$czRoot\scripts"

$czFiles = @(
    "$czRoot\.dockerignore",
    "$czRoot\.env.example",
    "$czRoot\.gitignore",
    "$czRoot\docker-compose.yml",
    "$czRoot\docker-compose.embedding-cpu.yml",
    "$czRoot\docker-compose.embedding-gpu.yml",
    "$czRoot\backend\Dockerfile",
    "$czRoot\backend\requirements.txt",
    "$czRoot\backend\alembic.ini"
)
foreach ($relative in $czFiles) {
    $source = Join-Path $workspace $relative
    if (Test-Path -LiteralPath $source) {
        $destination = Join-Path $packageDir $relative
        New-Item -ItemType Directory -Path (Split-Path -Parent $destination) -Force | Out-Null
        Copy-Item -LiteralPath $source -Destination $destination
    }
}

$taxonomy = Get-Content (Join-Path $workspace "src\answer_hub\product_categories.json") -Raw -Encoding UTF8 | ConvertFrom-Json
$deliveryFiles = Get-ChildItem -LiteralPath $packageDir -Recurse -File
$manifest = [ordered]@{
    package = $packageName
    built_at = (Get-Date).ToString("o")
    taxonomy_version = $taxonomy.version
    product_categories = @($taxonomy.categories | Where-Object { $_.active } | ForEach-Object {
        [ordered]@{ code = $_.code; name = $_.name }
    })
    file_count = $deliveryFiles.Count
    excludes_secrets_and_business_data = $true
}
$manifest | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath (Join-Path $packageDir "manifest.json") -Encoding UTF8

$hashLines = Get-ChildItem -LiteralPath $packageDir -Recurse -File |
    Where-Object { $_.Name -ne "checksums.sha256" } |
    Sort-Object FullName |
    ForEach-Object {
        $relative = $_.FullName.Substring($packageDir.Length).TrimStart("\")
        $hash = (Get-FileHash -LiteralPath $_.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
        "$hash  $relative"
    }
$hashLines | Set-Content -LiteralPath (Join-Path $packageDir "checksums.sha256") -Encoding UTF8

Compress-Archive -LiteralPath $packageDir -DestinationPath $zipPath -CompressionLevel Optimal

[ordered]@{
    package_directory = $packageDir
    zip_file = $zipPath
    file_count = (Get-ChildItem -LiteralPath $packageDir -Recurse -File).Count
} | ConvertTo-Json -Depth 4
