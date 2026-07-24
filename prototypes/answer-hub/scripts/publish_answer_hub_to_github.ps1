param(
    [string]$Repository = "Knjss-0508/knowledge-kb",
    [string]$BaseBranch = "master",
    [string]$TargetPath = "prototypes/answer-hub",
    [switch]$SkipValidation
)

$ErrorActionPreference = "Stop"
[Console]::InputEncoding = New-Object System.Text.UTF8Encoding($false)
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$OutputEncoding = [Console]::OutputEncoding
$env:GIT_PAGER = "cat"
$env:PAGER = "cat"

$normalizedTargetPath = $TargetPath.Trim().Trim([char[]]"/\")
if ($normalizedTargetPath -ne "prototypes/answer-hub") {
    throw "This workflow only permits the exact target path: prototypes/answer-hub"
}
$TargetPath = $normalizedTargetPath

function Invoke-CheckedCommand {
    param(
        [Parameter(Mandatory = $true)]
        [string]$Description,
        [Parameter(Mandatory = $true)]
        [scriptblock]$Command
    )

    Write-Host ""
    Write-Host "==> $Description" -ForegroundColor Cyan
    & $Command
    if ($LASTEXITCODE -ne 0) {
        throw "$Description failed with exit code $LASTEXITCODE."
    }
}

function Resolve-GitHubCli {
    $installed = Get-Command gh -ErrorAction SilentlyContinue
    if ($installed) {
        return $installed.Source
    }

    $portable = Join-Path $ProjectRoot "tools\github-cli\bin\gh.exe"
    if (Test-Path -LiteralPath $portable) {
        return (Resolve-Path -LiteralPath $portable).Path
    }

    throw "GitHub CLI was not found. Install gh or restore tools\github-cli\bin\gh.exe."
}

function Assert-ChildPath {
    param(
        [Parameter(Mandatory = $true)]
        [string]$ParentPath,
        [Parameter(Mandatory = $true)]
        [string]$ChildPath
    )

    $parent = [IO.Path]::GetFullPath($ParentPath).TrimEnd("\") + "\"
    $child = [IO.Path]::GetFullPath($ChildPath)
    if (-not $child.StartsWith($parent, [StringComparison]::OrdinalIgnoreCase)) {
        throw "Unsafe path detected: $child is not inside $parent."
    }
}

function Copy-AnswerHubSource {
    param(
        [Parameter(Mandatory = $true)]
        [string]$SourcePath,
        [Parameter(Mandatory = $true)]
        [string]$DestinationPath
    )

    $excludedDirectories = @(
        ".git",
        ".agents",
        ".claude",
        ".codex",
        ".codex_stage",
        ".codex_tmp_sheet",
        ".pytest_cache",
        ".venv",
        ".cz_test_venv",
        "__pycache__",
        "github-cli",
        "*.egg-info",
        (Join-Path $SourcePath "cz-knowledge-kb"),
        (Join-Path $SourcePath "data"),
        (Join-Path $SourcePath "handoff"),
        (Join-Path $SourcePath "node_modules"),
        (Join-Path $SourcePath "outputs"),
        (Join-Path $SourcePath "tools\dify"),
        (Join-Path $SourcePath "tools\github-cli")
    )
    $excludedFiles = @(
        ".env",
        ".codex_last_clone_path",
        ".codex_last_app_publish_path",
        "codex_test_*.patch",
        "update_pr12_with_cz_interfaces.ps1",
        "更新PR12合并CZ接口.cmd",
        "*.db",
        "*.log",
        "*.pyc",
        "*.pyo",
        "*.sqlite",
        "*.sqlite3"
    )

    New-Item -ItemType Directory -Force -Path $DestinationPath | Out-Null
    $robocopyArguments = @(
        $SourcePath,
        $DestinationPath,
        "/E",
        "/COPY:DAT",
        "/DCOPY:DAT",
        "/R:2",
        "/W:1",
        "/NFL",
        "/NDL",
        "/NJH",
        "/NJS",
        "/NP",
        "/XD"
    ) + $excludedDirectories + @("/XF") + $excludedFiles

    & robocopy @robocopyArguments
    if ($LASTEXITCODE -gt 7) {
        throw "Source copy failed with robocopy exit code $LASTEXITCODE."
    }
}

function Assert-StagingSafety {
    param(
        [Parameter(Mandatory = $true)]
        [string]$StagingPath
    )

    $forbiddenDirectoryNamesAnywhere = @(
        ".git",
        ".venv",
        ".cz_test_venv",
        "__pycache__",
        "dify",
        "github-cli",
        ".codex_stage"
    )
    $forbiddenRootDirectories = @(
        "cz-knowledge-kb",
        "data",
        "handoff",
        "node_modules",
        "outputs"
    )
    $forbiddenFileNames = @(
        ".env",
        ".codex_last_clone_path",
        ".codex_last_app_publish_path",
        "update_pr12_with_cz_interfaces.ps1",
        "更新PR12合并CZ接口.cmd"
    )
    $forbiddenExtensions = @(
        ".key",
        ".log",
        ".p12",
        ".pfx",
        ".pem",
        ".pyc",
        ".pyo",
        ".patch",
        ".db",
        ".sqlite",
        ".sqlite3"
    )

    $stagingFullPath = [IO.Path]::GetFullPath($StagingPath).TrimEnd("\") + "\"
    $unsafe = Get-ChildItem -LiteralPath $StagingPath -Force -Recurse |
        Where-Object {
            $relativePath = $_.FullName.Substring($stagingFullPath.Length)
            $isRootEntry = $relativePath -notmatch "[\\/]"
            ($_.PSIsContainer -and $forbiddenDirectoryNamesAnywhere -contains $_.Name) -or
            ($_.PSIsContainer -and $_.Name -like "*.egg-info") -or
            ($_.PSIsContainer -and $isRootEntry -and $forbiddenRootDirectories -contains $_.Name) -or
            (-not $_.PSIsContainer -and $forbiddenFileNames -contains $_.Name) -or
            (-not $_.PSIsContainer -and $forbiddenExtensions -contains $_.Extension.ToLowerInvariant())
        }

    if ($unsafe) {
        $paths = $unsafe | ForEach-Object { $_.FullName }
        throw "Forbidden files were found in the upload staging directory:`n$($paths -join "`n")"
    }

    $fileCount = @(Get-ChildItem -LiteralPath $StagingPath -Force -Recurse -File).Count
    if ($fileCount -lt 10) {
        throw "Only $fileCount files were staged. Refusing to replace the remote answer-hub directory."
    }

    Write-Host "Staging safety check passed: $fileCount files." -ForegroundColor Green
}

$ProjectRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$GitHubCli = Resolve-GitHubCli
$Git = (Get-Command git -ErrorAction Stop).Source
$timestamp = Get-Date -Format "yyyyMMdd-HHmmss"
$branchName = "agent/answer-hub-update-$timestamp"
$temporaryRoot = Join-Path ([IO.Path]::GetTempPath()) "knowledge-kb-answer-hub-$timestamp-$PID"
$cloneRoot = Join-Path $temporaryRoot "repository"
$stagingRoot = Join-Path $temporaryRoot "answer-hub-stage"

Write-Host "Repository : $Repository"
Write-Host "Base branch: $BaseBranch"
Write-Host "New branch : $branchName"
Write-Host "Source     : $ProjectRoot"
Write-Host "Target     : $TargetPath"
Write-Host "Temp path  : $temporaryRoot"

if (-not $SkipValidation) {
    $verificationScript = Join-Path $ProjectRoot "scripts\verify_release.ps1"
    if (-not (Test-Path -LiteralPath $verificationScript)) {
        throw "Release verification script was not found: $verificationScript"
    }

    Invoke-CheckedCommand "Run full release verification" {
        & powershell -NoProfile -ExecutionPolicy Bypass -File $verificationScript
    }
}

Invoke-CheckedCommand "Verify GitHub authentication" {
    & $GitHubCli auth status -h github.com
}

New-Item -ItemType Directory -Force -Path $temporaryRoot | Out-Null
Assert-ChildPath -ParentPath ([IO.Path]::GetTempPath()) -ChildPath $temporaryRoot

Invoke-CheckedCommand "Clone the GitHub repository" {
    & $GitHubCli repo clone $Repository $cloneRoot -- `
        --config core.autocrlf=false `
        --branch $BaseBranch `
        --single-branch
}

Assert-ChildPath -ParentPath $temporaryRoot -ChildPath $cloneRoot
Invoke-CheckedCommand "Create an isolated upload branch" {
    & $Git -C $cloneRoot switch -c $branchName
}

Invoke-CheckedCommand "Configure non-interactive Git output" {
    & $Git -C $cloneRoot config core.pager cat
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to disable the Git pager."
    }
    & $Git -C $cloneRoot config core.quotepath false
    if ($LASTEXITCODE -ne 0) {
        throw "Unable to configure readable Git paths."
    }
}

$freshCloneChanges = @(& $Git -C $cloneRoot status --porcelain)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to verify the fresh clone state."
}
if ($freshCloneChanges.Count -gt 0) {
    throw "The fresh clone is not clean. Refusing to continue:`n$($freshCloneChanges -join "`n")"
}

Write-Host ""
Write-Host "==> Build a clean answer-hub staging directory" -ForegroundColor Cyan
Copy-AnswerHubSource -SourcePath $ProjectRoot -DestinationPath $stagingRoot
Assert-StagingSafety -StagingPath $stagingRoot

$targetRoot = Join-Path $cloneRoot ($TargetPath -replace "/", "\")
Assert-ChildPath -ParentPath $cloneRoot -ChildPath $targetRoot
if (Test-Path -LiteralPath $targetRoot) {
    Remove-Item -LiteralPath $targetRoot -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null
Get-ChildItem -LiteralPath $stagingRoot -Force |
    Copy-Item -Destination $targetRoot -Recurse -Force

$outsideChanges = @(
    & $Git -C $cloneRoot status --porcelain -- . ":(exclude)$TargetPath/**"
)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to verify the Git change scope."
}
if ($outsideChanges.Count -gt 0) {
    throw "Changes outside $TargetPath were detected:`n$($outsideChanges -join "`n")"
}

$answerHubChanges = @(& $Git -C $cloneRoot status --porcelain -- $TargetPath)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to read the answer-hub change list."
}
if ($answerHubChanges.Count -eq 0) {
    Write-Host ""
    Write-Host "No differences were found. Nothing needs to be uploaded." -ForegroundColor Green
    Write-Host "Temporary clone: $cloneRoot"
    exit 0
}

Write-Host ""
Write-Host "==> Files that will be submitted" -ForegroundColor Yellow
& $Git -C $cloneRoot status --short -- $TargetPath
Write-Host ""
& $Git -C $cloneRoot --no-pager diff --stat -- $TargetPath
& $Git -C $cloneRoot --no-pager diff --check -- $TargetPath
if ($LASTEXITCODE -ne 0) {
    throw "git diff --check found invalid whitespace or conflict markers."
}

Write-Host ""
Write-Host "$TargetPath will be completely replaced by the safe local staging directory." -ForegroundColor Yellow
Write-Host "Deleted remote-only files are intentionally included in this replacement." -ForegroundColor Yellow
$confirmation = Read-Host "Type PUSH to commit and upload, or press Enter to cancel"
if ($confirmation -cne "PUSH") {
    Write-Host "Upload cancelled. Temporary clone retained at: $cloneRoot" -ForegroundColor Yellow
    exit 2
}

$login = (& $GitHubCli api user --jq ".login").Trim()
$userId = (& $GitHubCli api user --jq ".id").Trim()
if (-not $login -or -not $userId) {
    throw "Unable to determine the authenticated GitHub identity."
}
& $Git -C $cloneRoot config user.name $login
& $Git -C $cloneRoot config user.email "$userId+$login@users.noreply.github.com"

Invoke-CheckedCommand "Stage the answer-hub directory" {
    & $Git -C $cloneRoot add -A -- $TargetPath
}

$stagedOutsideTarget = @(
    & $Git -C $cloneRoot diff --cached --name-only -- . ":(exclude)$TargetPath/**"
)
if ($LASTEXITCODE -ne 0) {
    throw "Unable to verify the staged file scope."
}
if ($stagedOutsideTarget.Count -gt 0) {
    throw "Files outside $TargetPath were staged:`n$($stagedOutsideTarget -join "`n")"
}

$commitMessage = "feat: 接通 CZ 候选价值复核队列"
Invoke-CheckedCommand "Create the Git commit" {
    & $Git -C $cloneRoot commit -m $commitMessage
}

Invoke-CheckedCommand "Push the isolated branch" {
    & $Git -C $cloneRoot push -u origin $branchName
}

$pullRequestBody = @"
## 变更内容

- 在聚类、标注、转写流程中增加“知识点是否值得沉淀”
- 增加模型初标、人工标注及批量送审门禁
- 接通候选知识导出和 CZ 候选价值复核接口的沉淀价值字段
- 更新相关测试、操作文档和交付说明

## 上传范围

- 仅完整替换 $TargetPath
- 清理远端旧版本残留文件
- 明确不修改或上传 CZ 源码目录、历史交付包、`.env`、业务数据、输出文件、虚拟环境或缓存

## 验证

- 发布验收脚本通过
- 包括根项目测试、CZ 接口兼容测试、Python 编译、前端 JavaScript 语法和 Docker Compose 配置检查
"@
$pullRequestBodyPath = Join-Path $temporaryRoot "pull-request-body.md"
Set-Content -LiteralPath $pullRequestBodyPath -Value $pullRequestBody -Encoding UTF8

Write-Host ""
Write-Host "==> Create a draft pull request" -ForegroundColor Cyan
$pullRequestUrl = & $GitHubCli pr create `
    --repo $Repository `
    --base $BaseBranch `
    --head $branchName `
    --draft `
    --title $commitMessage `
    --body-file $pullRequestBodyPath
if ($LASTEXITCODE -ne 0) {
    throw "The branch was pushed, but draft PR creation failed. Temporary clone: $cloneRoot"
}

$commitSha = (& $Git -C $cloneRoot rev-parse HEAD).Trim()
Write-Host ""
Write-Host "Upload completed." -ForegroundColor Green
Write-Host "Branch : $branchName"
Write-Host "Commit : $commitSha"
Write-Host "Draft PR: $pullRequestUrl"
Write-Host "Temp repo: $cloneRoot"
