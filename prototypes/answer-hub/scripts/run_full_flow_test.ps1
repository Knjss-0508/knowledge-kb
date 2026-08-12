param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [string]$SourcePath = ""
)

$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $ProjectRoot

$python = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
if (-not (Test-Path -LiteralPath $python)) {
    throw "Project Python was not found: $python"
}

if ($SourcePath) {
    $selectedSourcePath = $SourcePath
} else {
    Add-Type -AssemblyName System.Windows.Forms
    $dialog = New-Object System.Windows.Forms.OpenFileDialog
    $dialog.Title = "选择用于全流程测试的第二部分 Excel"
    $dialog.Filter = "Excel 文件 (*.xlsx;*.xlsm)|*.xlsx;*.xlsm"
    $dialog.Multiselect = $false
    $dialog.CheckFileExists = $true

    try {
        if ($dialog.ShowDialog() -ne [System.Windows.Forms.DialogResult]::OK) {
            Write-Host "已取消选择文件。" -ForegroundColor Yellow
            exit 0
        }
        $selectedSourcePath = $dialog.FileName
    } finally {
        $dialog.Dispose()
    }
}

if (-not (Test-Path -LiteralPath $selectedSourcePath -PathType Leaf)) {
    Write-Host "没有找到所选 Excel 文件：$selectedSourcePath" -ForegroundColor Red
    exit 1
}

Write-Host ""
Write-Host "即将运行完整测试链路：" -ForegroundColor Cyan
Write-Host "Excel -> 聚类 -> 价值判断 -> 转写 -> CZ候选价值复核"
Write-Host "本流程不会自动送审或发布知识。" -ForegroundColor Yellow
Write-Host "运行期间请勿关闭窗口，模型处理可能需要几分钟。" -ForegroundColor DarkGray
Write-Host ""

$env:PYTHONPATH = Join-Path $ProjectRoot "src"
& $python -m answer_hub.full_flow_test --source $selectedSourcePath
exit $LASTEXITCODE
