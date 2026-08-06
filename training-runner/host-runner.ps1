[CmdletBinding()]
param(
    [ValidateSet("install", "check", "smoke", "probe", "run")]
    [string]$Action = "run",
    [string]$ConfigPath = (Join-Path $PSScriptRoot ".env"),
    [string]$RuntimeRoot = ""
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$RunnerDirectory = $PSScriptRoot
$RequirementsPath = Join-Path $RunnerDirectory "requirements.txt"
$RunnerPath = Join-Path $RunnerDirectory "runner.py"
$CheckPath = Join-Path $RunnerDirectory "check_host.py"
$SmokePath = Join-Path $RunnerDirectory "smoke_train.py"
$ProjectContainerNames = @(
    "kb-backend",
    "kb-postgres",
    "kb-redis",
    "kb-embedding-qwen",
    "kb-embedding-training-runner"
)

function Import-RunnerEnvironment {
    param([Parameter(Mandatory = $true)][string]$Path)

    if (-not (Test-Path -LiteralPath $Path -PathType Leaf)) {
        throw "缺少 Runner 配置文件：$Path。请先复制 .env.example 为 .env 并填写服务器地址和 Runner 密钥。"
    }

    foreach ($rawLine in Get-Content -LiteralPath $Path -Encoding UTF8) {
        $line = $rawLine.Trim()
        if (-not $line -or $line.StartsWith("#")) {
            continue
        }
        $separator = $line.IndexOf("=")
        if ($separator -le 0) {
            throw "Runner 配置存在无效行：$rawLine"
        }
        $name = $line.Substring(0, $separator).Trim()
        $value = $line.Substring($separator + 1).Trim()
        if (
            ($value.StartsWith('"') -and $value.EndsWith('"')) -or
            ($value.StartsWith("'") -and $value.EndsWith("'"))
        ) {
            $value = $value.Substring(1, $value.Length - 2)
        }
        Set-Item -LiteralPath "Env:$name" -Value $value
    }
}

function Resolve-RuntimeRoot {
    if ($RuntimeRoot) {
        return [System.IO.Path]::GetFullPath($RuntimeRoot)
    }
    if ($env:TRAINING_RUNTIME_ROOT) {
        return [System.IO.Path]::GetFullPath($env:TRAINING_RUNTIME_ROOT)
    }
    return [System.IO.Path]::GetFullPath(
        (Join-Path $env:LOCALAPPDATA "KnowledgeKB\embedding-training")
    )
}

function Assert-NoProjectContainers {
    $docker = Get-Command "docker.exe" -ErrorAction SilentlyContinue
    if (-not $docker) {
        return
    }

    $runningNames = @(
        & $docker.Source ps --format "{{.Names}}" 2>$null
    )
    if ($LASTEXITCODE -ne 0) {
        return
    }
    $runningProjectContainers = @(
        $runningNames | Where-Object { $_ -in $ProjectContainerNames }
    )
    if ($runningProjectContainers.Count -gt 0) {
        throw (
            "本机仍有项目容器运行：" +
            ($runningProjectContainers -join "、") +
            "。请先停止它们，Runner 不与本地项目服务并行运行。"
        )
    }
}

function Resolve-BasePython {
    $launcher = Get-Command "py.exe" -ErrorAction SilentlyContinue
    if ($launcher) {
        $resolved = (
            & $launcher.Source -3.12 -c "import sys; print(sys.executable)"
        ).Trim()
        if ($LASTEXITCODE -eq 0 -and $resolved) {
            return $resolved
        }
    }

    $python = Get-Command "python.exe" -ErrorAction SilentlyContinue
    if ($python -and $python.Source -notlike "*\WindowsApps\*") {
        return $python.Source
    }
    throw "未找到可用的 Python 3.12。请安装正式版 Python，不能使用 WindowsApps 别名。"
}

if ($Action -in @("run", "probe")) {
    Import-RunnerEnvironment -Path $ConfigPath
} elseif (
    $Action -in @("check", "smoke") -and
    (Test-Path -LiteralPath $ConfigPath)
) {
    Import-RunnerEnvironment -Path $ConfigPath
}

$ResolvedRuntimeRoot = Resolve-RuntimeRoot
$VenvRoot = Join-Path $ResolvedRuntimeRoot ".venv"
$VenvPython = Join-Path $VenvRoot "Scripts\python.exe"
$VenvScripts = Join-Path $VenvRoot "Scripts"

New-Item -ItemType Directory -Force -Path $ResolvedRuntimeRoot | Out-Null
New-Item -ItemType Directory -Force -Path (
    Join-Path $ResolvedRuntimeRoot "artifacts"
) | Out-Null
New-Item -ItemType Directory -Force -Path (
    Join-Path $ResolvedRuntimeRoot "cache\huggingface"
) | Out-Null
New-Item -ItemType Directory -Force -Path (
    Join-Path $ResolvedRuntimeRoot "cache\modelscope"
) | Out-Null

if ($Action -eq "install") {
    $BasePython = Resolve-BasePython
    if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
        Write-Host "创建独立训练环境：$VenvRoot"
        & $BasePython -m venv $VenvRoot
        if ($LASTEXITCODE -ne 0) {
            throw "创建训练虚拟环境失败"
        }
    }

    Write-Host "安装 PyTorch CUDA 12.6 与训练依赖..."
    & $VenvPython -m pip install --upgrade pip
    if ($LASTEXITCODE -ne 0) {
        throw "升级 pip 失败"
    }
    & $VenvPython -m pip install `
        "torch==2.11.0+cu126" `
        --index-url "https://download.pytorch.org/whl/cu126"
    if ($LASTEXITCODE -ne 0) {
        throw "安装 PyTorch CUDA 版本失败"
    }
    & $VenvPython -m pip install -r $RequirementsPath
    if ($LASTEXITCODE -ne 0) {
        throw "安装训练依赖失败"
    }
    & $VenvPython -m pip check
    if ($LASTEXITCODE -ne 0) {
        throw "训练依赖存在冲突"
    }
}

if (-not (Test-Path -LiteralPath $VenvPython -PathType Leaf)) {
    throw "训练环境尚未安装，请先执行：pwsh -File `"$PSCommandPath`" -Action install"
}

$env:PATH = "$VenvScripts;$env:PATH"
$env:TRAINING_RUNTIME_ROOT = $ResolvedRuntimeRoot
$env:TRAINING_ARTIFACT_ROOT = Join-Path $ResolvedRuntimeRoot "artifacts"
$env:HF_HOME = Join-Path $ResolvedRuntimeRoot "cache\huggingface"
$env:MODELSCOPE_CACHE = Join-Path $ResolvedRuntimeRoot "cache\modelscope"

Assert-NoProjectContainers

if ($Action -eq "probe") {
    Write-Host "检测本机 Runner 与服务器控制面的连接..."
    & $VenvPython $RunnerPath --heartbeat-once
    if ($LASTEXITCODE -ne 0) {
        throw "Runner 连接检测失败"
    }
    exit 0
}

if ($Action -in @("install", "check", "smoke")) {
    Write-Host "校验 Windows 宿主机 CUDA 训练环境..."
    & $VenvPython $CheckPath
    if ($LASTEXITCODE -ne 0) {
        throw "宿主机训练环境校验失败"
    }
    if ($Action -ne "smoke") {
        Write-Host "训练环境已就绪，运行目录：$ResolvedRuntimeRoot"
        exit 0
    }
}

if ($Action -eq "smoke") {
    Write-Host "执行最小化 QLoRA 实训；不会连接服务器或启动项目服务。"
    & $VenvPython $SmokePath --runtime-root $ResolvedRuntimeRoot
    if ($LASTEXITCODE -ne 0) {
        throw "宿主机最小化 QLoRA 实训失败"
    }
    exit 0
}

$requiredNames = @(
    "TRAINING_CONTROL_BASE_URL",
    "TRAINING_RUNNER_TOKEN",
    "TRAINING_RUNNER_ID"
)
foreach ($name in $requiredNames) {
    $value = [Environment]::GetEnvironmentVariable($name)
    if (-not $value) {
        throw "Runner 配置缺少：$name"
    }
}

Write-Host "启动宿主机 GPU Runner；本机不会启动任何知识库容器或业务服务。"
Write-Host "训练产物目录：$env:TRAINING_ARTIFACT_ROOT"
& $VenvPython $RunnerPath
exit $LASTEXITCODE
