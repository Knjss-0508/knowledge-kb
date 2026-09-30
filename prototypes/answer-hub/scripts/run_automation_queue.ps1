param(
    [string]$ProjectRoot = (Split-Path -Parent $PSScriptRoot),
    [switch]$RetryFailed,
    # 只做 Python 解释器探测并打印结果，不执行任何队列处理（排障与自动测试用）。
    [switch]$CheckInterpreter
)

# 本文件必须以「UTF-8 带 BOM」保存，请不要去掉 BOM。
# 计划任务（run_automation_queue_hidden.vbs）和 Answer Hub API 都是用 Windows
# PowerShell 5.1（powershell.exe）调用本脚本，而 5.1 会把「无 BOM 的 UTF-8」当成
# ANSI（中文系统上是 cp936）来读：中文提示会变乱码，个别汉字的字节还会吃掉后面的
# 引号，直接导致语法错误、脚本一行都不执行。带 BOM 时 5.1 才按 UTF-8 解析。
# 回归测试 tests/test_run_automation_queue_interpreter.py 会守住这一点。
$ErrorActionPreference = "Stop"
Set-Location -LiteralPath $ProjectRoot

$envFile = Join-Path $ProjectRoot ".env"
if (Test-Path -LiteralPath $envFile) {
    Get-Content -LiteralPath $envFile -Encoding UTF8 | ForEach-Object {
        $line = $_.Trim()
        if ($line -and -not $line.StartsWith("#") -and $line.Contains("=")) {
            $parts = $line.Split("=", 2)
            if (-not [Environment]::GetEnvironmentVariable($parts[0], "Process")) {
                [Environment]::SetEnvironmentVariable($parts[0], $parts[1], "Process")
            }
        }
    }
}

# ---------------------------------------------------------------------------
# Python 解释器探测：先按优先级找候选，再真的执行一次校验，全失败就显式报错退出。
#
# 旧实现只判断「<ProjectRoot>\.venv\Scripts\python.exe 是否存在」，不存在就直接用
# PATH 上的 python.exe。Windows 上的 python.exe 往往只是 Microsoft Store 的
# 「应用执行别名」存根：执行后只打印一句提示并以 9009 退出（在 Windows
# PowerShell 5.1 下还会因为 $ErrorActionPreference = "Stop" 变成终止错误，连重定向
# 都来不及写）。于是脚本静默什么都不做就结束、日志 0 字节，而调用方
# （POST /api/v1/automation/retry-failed）仍然返回 HTTP 202，看起来像成功。
#
# 现在的规则：
#   1) 按优先级收集候选解释器；
#   2) 每个候选都真的执行 `-c "import answer_hub.cli"`，退出码为 0 才算可用
#      （只判断文件存在是不够的：E:\Python312 这类解释器存在但没有 answer_hub 依赖）；
#   3) 已知的 Store 存根路径（含 \WindowsApps\）直接跳过，不执行；
#   4) 一个都不可用时，把「找过哪些路径 + 怎么办」写进 stderr 和日志，并以退出码 3 结束。
#
# 候选顺序（先本机约定、再通用兜底，别的机器也能跑）：
#   a) $env:ANSWER_HUB_PYTHON                 显式指定；设了却不可用就直接报错，不静默降级
#   b) <ProjectRoot>\.venv\Scripts\python.exe 仓库约定的虚拟环境
#   c) $env:ANSWER_HUB_PYTHON_FALLBACKS       分号分隔的补充路径（本机/服务器固定环境）
#   d) <ANSWER_HUB_RUNTIME_DIR>\venv\Scripts\python.exe
#      本机运行时目录，默认 E:\answer-hub-runtime（与 scripts\local-migration 下的脚本同一约定）
#   e) PATH 上的 python.exe
# ---------------------------------------------------------------------------
$env:PYTHONPATH = Join-Path $ProjectRoot "src"

# 日志路径提前算好：解释器都找不到时，也要能把原因写进页面「查看运行日志」读的那个文件。
$logDir = Join-Path $ProjectRoot "outputs\automation-logs"
New-Item -ItemType Directory -Force -Path $logDir | Out-Null
$logPath = Join-Path $logDir ("queue-" + (Get-Date -Format "yyyyMMdd") + ".log")

function Test-AnswerHubInterpreter {
    param([string]$Candidate)

    if ([string]::IsNullOrWhiteSpace($Candidate)) { return $false }
    # Microsoft Store 的应用执行别名存根：执行必然失败，直接跳过（也避免弹出商店提示）。
    if ($Candidate -match "[\\/]WindowsApps[\\/]") { return $false }
    if (-not (Test-Path -LiteralPath $Candidate -PathType Leaf)) { return $false }

    # Windows PowerShell 5.1 会把原生命令写到 stderr 的内容当成终止错误，
    # 这里临时放行，改用退出码判断。
    $savedPreference = $ErrorActionPreference
    $ErrorActionPreference = "Continue"
    $exitCode = 1
    try {
        $null = & $Candidate -c "import answer_hub.cli" 2>&1
        $exitCode = $LASTEXITCODE
    } catch {
        $exitCode = 1
    } finally {
        $ErrorActionPreference = $savedPreference
    }
    return ($exitCode -eq 0)
}

function Stop-WithInterpreterError {
    param(
        [string]$Reason,
        [System.Collections.ArrayList]$Tried
    )

    $lines = @(
        "[Answer Hub] 自动化队列未启动：找不到可用的 Python 解释器。",
        "原因：$Reason",
        "本次没有执行任何队列处理（没有读取、处理或移动队列文件）。",
        "",
        "解释器候选探测结果（按优先级）："
    )
    $lines += @($Tried)
    $lines += @(
        "",
        "怎么办：",
        "  1) 为项目准备虚拟环境：<ProjectRoot>\.venv\Scripts\python.exe，并装好依赖；或",
        "  2) 指定一个已装好 answer_hub 的解释器（部署机/本机通用）：",
        "     setx ANSWER_HUB_PYTHON ""E:\answer-hub-runtime\venv\Scripts\python.exe""",
        "     多条候选可用分号分隔的 ANSWER_HUB_PYTHON_FALLBACKS 补充。",
        '  3) 自检：& "$env:ANSWER_HUB_PYTHON" -c "import answer_hub.cli"',
        "",
        "日志文件：$logPath",
        "退出码：3（解释器不可用）"
    )
    $text = $lines -join [Environment]::NewLine
    [Console]::Error.WriteLine($text)
    try {
        $utf8NoBom = New-Object System.Text.UTF8Encoding($false)
        [System.IO.File]::AppendAllText($logPath, $text + [Environment]::NewLine, $utf8NoBom)
    } catch {
        Write-Output $text
    }
    exit 3
}

$pythonCandidates = New-Object System.Collections.ArrayList
if ($env:ANSWER_HUB_PYTHON) {
    [void]$pythonCandidates.Add(@{
        Label = "ANSWER_HUB_PYTHON 环境变量"
        Path = $env:ANSWER_HUB_PYTHON
        Explicit = $true
    })
}
[void]$pythonCandidates.Add(@{
    Label = "项目虚拟环境"
    Path = Join-Path $ProjectRoot ".venv\Scripts\python.exe"
    Explicit = $false
})
foreach ($fallback in ($env:ANSWER_HUB_PYTHON_FALLBACKS -split ";")) {
    if ($fallback.Trim()) {
        [void]$pythonCandidates.Add(@{
            Label = "ANSWER_HUB_PYTHON_FALLBACKS"
            Path = $fallback.Trim()
            Explicit = $false
        })
    }
}
$runtimeDir = if ($env:ANSWER_HUB_RUNTIME_DIR) {
    $env:ANSWER_HUB_RUNTIME_DIR
} else {
    "E:\answer-hub-runtime"
}
[void]$pythonCandidates.Add(@{
    Label = "本机运行时目录（ANSWER_HUB_RUNTIME_DIR，默认 E:\answer-hub-runtime）"
    Path = Join-Path $runtimeDir "venv\Scripts\python.exe"
    Explicit = $false
})
$pathPython = Get-Command python.exe -ErrorAction SilentlyContinue
if ($pathPython) {
    [void]$pythonCandidates.Add(@{
        Label = "PATH 上的 python.exe"
        Path = $pathPython.Source
        Explicit = $false
    })
}

$python = ""
$pythonTried = New-Object System.Collections.ArrayList
foreach ($candidate in $pythonCandidates) {
    $candidatePath = [string]$candidate.Path
    if (-not (Test-Path -LiteralPath $candidatePath -PathType Leaf)) {
        [void]$pythonTried.Add("  - $($candidate.Label)：$candidatePath（文件不存在）")
        if ($candidate.Explicit) {
            Stop-WithInterpreterError -Reason "ANSWER_HUB_PYTHON 指定的解释器不存在。" -Tried $pythonTried
        }
        continue
    }
    if ($candidatePath -match "[\\/]WindowsApps[\\/]") {
        [void]$pythonTried.Add(
            "  - $($candidate.Label)：$candidatePath（Microsoft Store 应用执行别名存根，已跳过）"
        )
        if ($candidate.Explicit) {
            Stop-WithInterpreterError -Reason "ANSWER_HUB_PYTHON 指向的是 Microsoft Store 应用执行别名存根。" -Tried $pythonTried
        }
        continue
    }
    if (Test-AnswerHubInterpreter -Candidate $candidatePath) {
        $python = $candidatePath
        [void]$pythonTried.Add("  - $($candidate.Label)：$candidatePath（可用）")
        break
    }
    [void]$pythonTried.Add(
        "  - $($candidate.Label)：$candidatePath（校验失败：无法执行 import answer_hub.cli）"
    )
    if ($candidate.Explicit) {
        Stop-WithInterpreterError -Reason "ANSWER_HUB_PYTHON 指定的解释器无法加载 answer_hub。" -Tried $pythonTried
    }
}
if (-not $python) {
    Stop-WithInterpreterError -Reason "已按顺序探测全部候选，没有一个可用。" -Tried $pythonTried
}

Write-Output "Answer Hub 自动化队列使用的 Python 解释器：$python"
if ($CheckInterpreter) {
    Write-Output "解释器探测结果（-CheckInterpreter，未执行任何队列处理）："
    Write-Output ($pythonTried -join [Environment]::NewLine)
    exit 0
}

$queueDir = if ($env:ANSWER_HUB_AUTOMATION_QUEUE) {
    $env:ANSWER_HUB_AUTOMATION_QUEUE
} else {
    Join-Path $ProjectRoot "data\automation-queue"
}
$outputDir = if ($env:ANSWER_HUB_AUTOMATION_OUTPUT) {
    $env:ANSWER_HUB_AUTOMATION_OUTPUT
} else {
    Join-Path $ProjectRoot "outputs\automation-runs"
}
$maxFiles = if ($env:ANSWER_HUB_AUTOMATION_MAX_FILES) {
    $env:ANSWER_HUB_AUTOMATION_MAX_FILES
} else {
    "10"
}
$staleAfterSeconds = if ($env:ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS) {
    $env:ANSWER_HUB_AUTOMATION_STALE_AFTER_SECONDS
} else {
    "7200"
}
$secondPartPullProfile = $env:SECOND_PART_PULL_PROFILE
$secondPartPullState = if ($env:SECOND_PART_PULL_STATE) {
    $env:SECOND_PART_PULL_STATE
} else {
    Join-Path $ProjectRoot "data\second-part-pull\state.json"
}
$secondPartPullMaxPages = if ($env:SECOND_PART_PULL_MAX_PAGES) {
    $env:SECOND_PART_PULL_MAX_PAGES
} else {
    "10"
}
$automationPlanPath = if ($env:ANSWER_HUB_AUTOMATION_PLAN_PATH) {
    $env:ANSWER_HUB_AUTOMATION_PLAN_PATH
} else {
    Join-Path $ProjectRoot "data\automation-plan.json"
}
$automationPlan = $null
if (Test-Path -LiteralPath $automationPlanPath) {
    try {
        $automationPlan = Get-Content -Raw -Encoding UTF8 $automationPlanPath | ConvertFrom-Json
    } catch {
        throw "无法读取执行计划文件：$automationPlanPath"
    }
}
$secondPartQueryFromDate = $env:SECOND_PART_QUERY_FROM_DATE
$secondPartQueryToDate = $env:SECOND_PART_QUERY_TO_DATE
$planFromDate = ""
$planToDate = ""
if ($automationPlan) {
    $planFromDate = [string]$automationPlan.knowledge_settle_from_date
    $planToDate = [string]$automationPlan.knowledge_settle_to_date
    if (-not $planFromDate) {
        $planFromDate = [string]$automationPlan.second_part_query_from_date
    }
    if (-not $planToDate) {
        $planToDate = [string]$automationPlan.second_part_query_to_date
    }
}
if ($planFromDate -or $planToDate) {
    $secondPartQueryFromDate = $planFromDate
    $secondPartQueryToDate = $planToDate
}
$legacySecondPartQueryDate = $env:SECOND_PART_QUERY_DATE
if ($secondPartQueryFromDate -or $secondPartQueryToDate) {
    if (-not $secondPartQueryFromDate -or -not $secondPartQueryToDate) {
        throw (
            "SECOND_PART_QUERY_FROM_DATE and " +
            "SECOND_PART_QUERY_TO_DATE must be set together."
        )
    }
} elseif ($legacySecondPartQueryDate) {
    $secondPartQueryFromDate = $legacySecondPartQueryDate
    $secondPartQueryToDate = $legacySecondPartQueryDate
} else {
    $secondPartQueryWindowDays = 1
    if ($env:SECOND_PART_QUERY_WINDOW_DAYS) {
        try {
            $secondPartQueryWindowDays = [int]$env:SECOND_PART_QUERY_WINDOW_DAYS
        } catch {
            throw "SECOND_PART_QUERY_WINDOW_DAYS must be a positive integer."
        }
    }
    if ($secondPartQueryWindowDays -lt 1) {
        throw "SECOND_PART_QUERY_WINDOW_DAYS must be at least 1."
    }
    $lastCompleteDate = (Get-Date).Date.AddDays(-1)
    $secondPartQueryFromDate = $lastCompleteDate.AddDays(
        1 - $secondPartQueryWindowDays
    ).ToString("yyyy-MM-dd")
    $secondPartQueryToDate = $lastCompleteDate.ToString("yyyy-MM-dd")
}
$env:SECOND_PART_QUERY_FROM_DATE = $secondPartQueryFromDate
$env:SECOND_PART_QUERY_TO_DATE = $secondPartQueryToDate
$useMimo = $env:ANSWER_HUB_AUTOMATION_USE_MIMO -match "^(1|true|yes|on)$"
$syncToCzReviewValue = if ($env:ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW) {
    $env:ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW
} else {
    $env:ANSWER_HUB_AUTOMATION_SUBMIT_TO_CZ
}
$submitToCz = $syncToCzReviewValue -match "^(1|true|yes|on)$"
$clusteringMode = if ($env:ANSWER_HUB_AUTOMATION_CLUSTERING_MODE) {
    $env:ANSWER_HUB_AUTOMATION_CLUSTERING_MODE
} elseif ($useMimo) {
    "direct_mimo"
} else {
    "rule"
}

$arguments = @(
    "-m",
    "answer_hub.cli",
    "automation-queue",
    "--queue-dir",
    $queueDir,
    "--output-dir",
    $outputDir,
    "--clustering-mode",
    $clusteringMode,
    "--max-files",
    $maxFiles,
    "--stale-after-seconds",
    $staleAfterSeconds
)

if ($env:ANSWER_HUB_AUTOMATION_STANDARDS) {
    $arguments += @("--standards", $env:ANSWER_HUB_AUTOMATION_STANDARDS)
}
if ($env:ANSWER_HUB_AUTOMATION_PRODUCT_TYPE) {
    $arguments += @("--product-type", $env:ANSWER_HUB_AUTOMATION_PRODUCT_TYPE)
}
if (-not $useMimo) {
    $arguments += "--rule-only"
}
if ($RetryFailed) {
    $arguments += "--retry-failed"
}
if ($submitToCz) {
    $arguments += "--sync-to-cz-review"
}

$pullExitCode = 0
if ($secondPartPullProfile) {
    $pullArguments = @(
        "-m",
        "answer_hub.cli",
        "second-part-pull",
        "--profile",
        $secondPartPullProfile,
        "--queue-dir",
        $queueDir,
        "--output-dir",
        $outputDir,
        "--state-file",
        $secondPartPullState,
        "--max-pages",
        $secondPartPullMaxPages
    )
    & $python @pullArguments *>> $logPath
    $pullExitCode = $LASTEXITCODE
}

& $python @arguments *>> $logPath
$queueExitCode = $LASTEXITCODE
if ($queueExitCode -ne 0) {
    exit $queueExitCode
}
exit $pullExitCode
