# -*- coding: utf-8 -*-
<#
Answer Hub 本机夜间调度器（Windows 等价于服务器 run_scheduled_queue.sh）

与服务器版本的差别：
  1. 第 4 步的 --sync-to-cz-review 由 .env 的 ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW
     决定：true 时把候选送入 CZ「候选价值复核」（人工终审，不自动发布）
  2. 运行前校验安全闸：自动审核/自动发布必须保持关闭（AUTO_REVIEW_KILL_SWITCH=true）
  3. 数据路径指向 E:\answer-hub-runtime
  4. 额外提供 -DryRun：只走到第 3 步参数就绪即退出（冒烟用，不拉取、不消费）

退出码：0=成功  1=队列有未解决文件  2=拉取失败  3=队列消费失败  9=安全闸拦截
#>
param([switch]$DryRun)
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$RT   = "E:\answer-hub-runtime"
$AH   = "E:\答疑中台知识库Docker容器\prototypes\answer-hub"
$PY   = "$RT\venv\Scripts\python.exe"
$LOG  = "$RT\scheduler.log"
$PLAN = "$RT\data\automation-plan.json"
$QUEUE= "$RT\data\automation-queue"
$OUT  = "$RT\outputs\automation-runs"
$STD  = "$RT\data\standards\active_standards.json"
$CFG  = "$RT\config\second-part-pull.powerzhuan.local.json"
$env:PYTHONPATH = "$AH\src"
$env:PYTHONUTF8 = "1"

function Log($m) {
    $line = "[{0}] {1}" -f (Get-Date -Format "yyyy-MM-dd HH:mm:ss"), $m
    Write-Host $line
    Add-Content -Path $LOG -Value $line -Encoding utf8
}

function RunPy($argsArray, $label) {
    Log ("  >> $label")
    $out = & $PY @argsArray 2>&1
    $code = $LASTEXITCODE
    $out | Select-Object -Last 12 | ForEach-Object { Log ("     $_") }
    if ($code -ne 0) { Log ("     (退出码 $code)") }
    return $code
}

Log "==================== 调度开始 ===================="

# ---------- 安全闸：任何推 CZ 的开关为真就拒绝运行 ----------
Log "  [安全闸] 校验推 CZ 开关"
$envMap = @{}
foreach ($raw in (Get-Content "$AH\.env" -Encoding UTF8)) {
    $ln = $raw.Trim()
    if ($ln -and -not $ln.StartsWith("#") -and $ln.Contains("=")) {
        $k, $v = $ln.Split("=", 2); $envMap[$k.Trim()] = $v.Trim()
    }
}
$cfgSync = $null
try { $cfgSync = (Get-Content $CFG -Raw -Encoding UTF8 | ConvertFrom-Json).workflow.sync_to_cz_review } catch {}
$envSync  = $envMap["ANSWER_HUB_AUTOMATION_SYNC_TO_CZ_REVIEW"]
$envSubmit= $envMap["ANSWER_HUB_AUTOMATION_SUBMIT_TO_CZ"]
$kill     = $envMap["AUTO_REVIEW_KILL_SWITCH"]
Log ("    配置文件 workflow.sync_to_cz_review = {0}" -f $cfgSync)
Log ("    .env SYNC_TO_CZ_REVIEW               = {0}" -f $envSync)
Log ("    .env SUBMIT_TO_CZ                    = {0}" -f $envSubmit)
Log ("    .env AUTO_REVIEW_KILL_SWITCH         = {0}" -f $kill)

# 只放行「送候选价值复核」，不放行自动发布：
#   - 候选进入 CZ 的「候选价值复核」队列后仍需人工终审，不会自动变成已发布知识
#   - AUTO_REVIEW_KILL_SWITCH 必须保持 true（自动审核/自动发布关闭）
# 旧实现要求 sync_to_cz_review 必须为 false，导致自动化产物永远到不了候选价值复核。
if ($cfgSync -eq $true) { Log "  [提示] 配置文件 workflow.sync_to_cz_review = True（仅送候选价值复核）" }
if ($envSync -ne "true" -and $envSync -ne "false") { Log "  ❌ 安全闸拦截：.env SYNC_TO_CZ_REVIEW 必须是 true 或 false"; exit 9 }
if ($envSubmit -ne "true" -and $envSubmit -ne "false") { Log "  ❌ 安全闸拦截：.env SUBMIT_TO_CZ 必须是 true 或 false"; exit 9 }
if ($kill -ne "true") { Log "  ❌ 安全闸拦截：AUTO_REVIEW_KILL_SWITCH 不是 true（自动审核/自动发布必须关闭）"; exit 9 }

$syncToCzReview = ($envSync -eq "true") -or ($envSubmit -eq "true")
if ($syncToCzReview) {
    Log "  ✅ 安全闸通过：本次会把候选送入 CZ「候选价值复核」，不会自动发布"
} else {
    Log "  ✅ 安全闸通过：本次运行不会向 CZ 推送任何数据"
}

# ---------- 把 .env 注入子进程环境（与 run_automation_queue.ps1 保持一致） ----------
# 没有这一步，${SECOND_PART_API_TOKEN} 之类的占位符在子进程里永远展开不了，
# 拉取会以「引用的环境变量未设置」失败。已存在的进程环境变量优先，不覆盖。
$envInjected = 0; $envSkippedEmpty = 0; $envKeptExisting = 0
foreach ($k in $envMap.Keys) {
    $v = $envMap[$k]
    if ([string]::IsNullOrWhiteSpace($v)) { $envSkippedEmpty++; continue }
    if ([Environment]::GetEnvironmentVariable($k, "Process")) { $envKeptExisting++; continue }
    [Environment]::SetEnvironmentVariable($k, $v, "Process")
    $envInjected++
}
Log ("  [环境] .env 注入 {0} 个键；空值跳过 {1} 个；已存在保留 {2} 个" -f $envInjected, $envSkippedEmpty, $envKeptExisting)
if (-not $env:SECOND_PART_API_TOKEN) { Log "  ❌ 环境注入后 SECOND_PART_API_TOKEN 仍为空，拉取必然失败，提前退出"; exit 2 }
Log "  [环境] SECOND_PART_API_TOKEN 已就绪"

# ---------- 第二部分全量读取（与服务器 run_scheduled_queue.sh 对齐） ----------
# 接口不传 limit 时只返回 1000 条（接口硬顶 5000）。不设 SECOND_PART_QUERY_LIMIT 就会
# 被静默截断到 1000 条/天。.env 里若已设该键，走上面的注入值；否则取
# ANSWER_HUB_SECOND_PART_QUERY_LIMIT；都没有时兜底 10000，与服务器脚本一致。
if (-not $env:SECOND_PART_QUERY_LIMIT) {
    if ($env:ANSWER_HUB_SECOND_PART_QUERY_LIMIT) { $env:SECOND_PART_QUERY_LIMIT = $env:ANSWER_HUB_SECOND_PART_QUERY_LIMIT }
    else { $env:SECOND_PART_QUERY_LIMIT = "10000" }
}
Log ("  [环境] SECOND_PART_QUERY_LIMIT = {0}" -f $env:SECOND_PART_QUERY_LIMIT)


# ---------- 试点上限守卫（C 方案：本轮只跑 2026-09-13 这一天） ----------
# .env 里 ANSWER_HUB_PULL_PILOT_MAX_DATE 非空时，计划游标一旦达到或超过该日期，
# 本轮直接成功退出：不拉取、不记失败。
$pilotMax = $env:ANSWER_HUB_PULL_PILOT_MAX_DATE
if ($pilotMax) {
    $pilotCursor = ""
    try { $pilotCursor = [string](Get-Content $PLAN -Raw -Encoding UTF8 | ConvertFrom-Json).cursor_date } catch { $pilotCursor = "" }
    Log ("  [试点] 上限 {0}；计划游标 {1}" -f $pilotMax, $pilotCursor)
    if ($pilotCursor -and $pilotCursor -ge $pilotMax) {
        Log "  [试点] 游标已达到试点上限，本轮不拉取，成功退出（要续跑请清空 .env 里 ANSWER_HUB_PULL_PILOT_MAX_DATE）"
        exit 0
    }
}

# ---------- 主循环：逐日推进 ----------
# 第二部分某些日期可能整天没有数据（接口返回 0 条）。旧实现只处理一天就结束，
# 遇到空日仍会提交游标、但当晚不再继续，导致后面的日期要等第二天才能补。
# 现在改为：只要某个窗口「没拉到任何数据」，就当作空日直接提交并继续下一天，
# 直到某天真正拉到数据（需要跑模型）或达到单次运行上限为止。
# 这样一次夜间任务就能自动跨过连续的空日期，不会卡住沉淀进度。
$maxWindowsPerRun = 31
if ($env:ANSWER_HUB_MAX_WINDOWS_PER_RUN) {
    try { $maxWindowsPerRun = [Math]::Max(1, [int]$env:ANSWER_HUB_MAX_WINDOWS_PER_RUN) } catch { }
}

$processedWindows = 0
$emptyWindows = 0
$consumedWindows = 0
$lastExitCode = 0

while ($processedWindows -lt $maxWindowsPerRun) {
    # ---------- 1) prepare ----------
    $prepareJson = & $PY -m answer_hub.automation_schedule prepare --plan $PLAN 2>&1
    $code = $LASTEXITCODE
    if ($code -ne 0) { Log "  ❌ prepare 失败（退出码 $code）"; $prepareJson | ForEach-Object { Log ("     $_") }; exit 3 }
    $win = ("$prepareJson" -join "`n") | ConvertFrom-Json
    $from = $win.from_date; $to = $win.to_date
    $processedWindows++
    Log ("  1) 窗口: {0} ~ {1}" -f $from, $to)

    # ---------- 2) 队列阻塞检查 ----------
    $queueBusy = $false
    foreach ($b in @("pending", "processing", "failed")) {
        $d = Join-Path $QUEUE $b
        $n = @(Get-ChildItem $d -Filter "*.xlsx" -File -ErrorAction SilentlyContinue).Count
        Log ("  2) 桶 {0,-11} {1} 个 xlsx" -f $b, $n)
        if ($n -gt 0) {
            Log ("  ⛔ 队列有未解决的 {0} 文件，跳过 {1} ~ {2}" -f $b, $from, $to)
            # 这是「本轮跳过」，不是失败：队列还没清干净，本次不拉取而已。用 failure 会累积
            # consecutive_failure_count，三个晚上就把 auto_advance_paused 置为 true。
            & $PY -m answer_hub.automation_schedule defer --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "队列存在未解决的 $b 文件。" 2>&1 | Out-Null
            $queueBusy = $true
            break
        }
    }
    if ($queueBusy) { exit 1 }

    # ---------- 3) 拉取（重试 3 次） ----------
    $stateFile = "$RT\data\second-part-pull\scheduled-$($from -replace '-','')-$($to -replace '-','')-state.json"
    $env:SECOND_PART_QUERY_FROM_DATE = $from
    $env:SECOND_PART_QUERY_TO_DATE   = $to
    Log ("  3) 拉取 {0} ~ {1}  ->  {2}" -f $from, $to, $stateFile)
    if ($DryRun) {
        Log ("  [DryRun] output-dir = {0}" -f $OUT)
        Log ("  [DryRun] queue-dir  = {0}" -f $QUEUE)
        Log ("  [DryRun] SECOND_PART_QUERY_LIMIT = {0}；页数不限制" -f $env:SECOND_PART_QUERY_LIMIT)
        Log "  [DryRun] 参数就绪：未拉取、未消费、未 commit"
        exit 0
    }

    $pullOk = $false
    $pulledRecords = 0
    $pullLimit = 0
    for ($i = 1; $i -le 3; $i++) {
        $pullOut = & $PY -m answer_hub.cli second-part-pull --profile $CFG --queue-dir $QUEUE `
            --output-dir $OUT --state-file $stateFile --max-pages 0 --exclude-existing-records 2>&1
        $c = $LASTEXITCODE
        $pullOut | Select-Object -Last 12 | ForEach-Object { Log ("     $_") }
        if ($c -ne 0) { Log ("     (退出码 $c)") }
        if ($c -eq 0) {
            $pullOk = $true
            try {
                $summary = (($pullOut -join "`n") | ConvertFrom-Json)
                $pulledRecords = [int]$summary.fetched_records
                $pullLimit = [int]$env:SECOND_PART_QUERY_LIMIT
            } catch { }
            break
        }
        if ($i -lt 3) { Log "     等待 60 秒后重试"; Start-Sleep -Seconds 60 }
    }
    if (-not $pullOk) {
        Log "  ❌ 拉取连续 3 次失败，游标停在 $from"
        & $PY -m answer_hub.automation_schedule failure --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "第二部分拉取连续 3 次失败。" 2>&1 | Out-Null
        exit 2
    }

    # 接口没有分页游标，单次最多返回 SECOND_PART_QUERY_LIMIT 条（硬顶 5000）。
    if ($pullLimit -gt 0 -and $pulledRecords -ge $pullLimit) {
        Log ("  ⚠️ 本窗口拉到 {0} 条，已达到接口上限 {1}；可能存在截断。建议把 window_days 调小后重跑该日期。" -f $pulledRecords, $pullLimit)
    }

    # ---------- 3.1) 空日：直接提交并继续下一天 ----------
    if ($pulledRecords -le 0) {
        $emptyWindows++
        Log ("  ⏭️ {0} ~ {1} 第二部分返回 0 条，按空日处理：直接推进游标并继续下一天。" -f $from, $to)
        $c = & $PY -m answer_hub.automation_schedule commit --plan $PLAN --from-date $from --to-date $to 2>&1
        if ($LASTEXITCODE -ne 0) {
            $c | ForEach-Object { Log ("     $_") }
            Log "  ⚠️ 空日 commit 失败，停止本轮（游标未推进）"
            exit 3
        }
        Log ("  ✅ 空日已提交：游标推进到 {0} 之后的一天" -f $to)
        continue
    }

    # ---------- 4) 消费队列 ----------
    $consumeArgs = @("-m","answer_hub.cli","automation-queue","--queue-dir",$QUEUE,"--standards",$STD,
                     "--output-dir",$OUT,"--clustering-mode","direct_mimo","--max-files","10",
                     "--stale-after-seconds","7200")
    if ($syncToCzReview) {
        # 只送「候选价值复核」队列；发布仍需人工终审。
        $consumeArgs += "--sync-to-cz-review"
        Log "  4) 消费队列（★ 送 CZ 候选价值复核）"
    } else {
        Log "  4) 消费队列（不送 CZ）"
    }
    $c = RunPy $consumeArgs "automation-queue"
    if ($c -ne 0) {
        Log "  ❌ 队列消费失败（退出码 $c），游标停在 $from"
        & $PY -m answer_hub.automation_schedule failure --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "自动化队列退出码：$c" 2>&1 | Out-Null
        exit 3
    }
    $consumedWindows++

    # ---------- 5) commit ----------
    $c = RunPy @("-m","answer_hub.automation_schedule","commit","--plan",$PLAN,"--from-date",$from,"--to-date",$to) "commit 推进游标"
    if ($c -ne 0) { Log "  ⚠️ commit 失败"; exit 3 }

    # 真正消费过数据就结束本轮：单日 2000~3500 条通常要跑 6-9 小时，
    # 继续往下会叠加模型调用与磁盘占用，交给下一次计划任务更安全。
    Log ("  本轮已处理到有数据的日期，结束（空日 {0} 个，消费 {1} 个窗口）。" -f $emptyWindows, $consumedWindows)
    $lastExitCode = 0
    break
}

Log ("==================== 调度完成 ✅（窗口 {0} 个，其中空日 {1} 个）====================" -f $processedWindows, $emptyWindows)
exit $lastExitCode
