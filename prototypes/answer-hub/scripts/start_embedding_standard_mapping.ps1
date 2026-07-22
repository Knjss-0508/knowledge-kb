$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
$pythonPath = Join-Path $projectRoot ".venv\Scripts\python.exe"
$stdoutPath = Join-Path $projectRoot "outputs\cluster-v2-test\embedding_map.stdout.log"
$stderrPath = Join-Path $projectRoot "outputs\cluster-v2-test\embedding_map.stderr.log"
$pidPath = Join-Path $projectRoot "outputs\cluster-v2-test\embedding_map.pid"

Remove-Item -LiteralPath $stdoutPath, $stderrPath, $pidPath -ErrorAction SilentlyContinue

$arguments = @(
    "scripts\map_atomic_standards.py"
    "--atomic-json"
    "outputs\cluster-v2-test\atomic_topic_clusters.json"
    "--standards-json"
    "outputs\cluster-v2-test\qc_standard_catalog_4_categories.json"
    "--review-xlsx"
    "outputs\cluster-v2-test\workbooks\新版主题聚类_74原子知识点_审核.xlsx"
    "--output-json"
    "outputs\cluster-v2-test\atomic_standard_candidates_embedding.json"
    "--top-k"
    "5"
)

$process = Start-Process `
    -FilePath $pythonPath `
    -ArgumentList $arguments `
    -WorkingDirectory $projectRoot `
    -WindowStyle Hidden `
    -RedirectStandardOutput $stdoutPath `
    -RedirectStandardError $stderrPath `
    -PassThru

Set-Content -LiteralPath $pidPath -Value $process.Id -Encoding ascii
Write-Output $process.Id
