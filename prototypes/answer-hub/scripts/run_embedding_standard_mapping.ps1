$ErrorActionPreference = "Stop"

$projectRoot = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $projectRoot

$logPath = Join-Path $projectRoot "outputs\cluster-v2-test\embedding_map.log"
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

& ".\.venv\Scripts\python.exe" @arguments *> $logPath
exit $LASTEXITCODE
