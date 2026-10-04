# -*- coding: utf-8 -*-
<#
Answer Hub 本机夜间调度器（Windows 等价于服务器 run_scheduled_queue.sh）

与服务器版本的唯一差别：
  1. 第 4 步【不传】--sync-to-cz-review  —— 本机绝不向 CZ 推送
  2. 运行前硬校验配置与 .env 的推 CZ 开关，任一为真即拒绝运行（安全闸）
  3. 数据路径指向 E:\answer-hub-runtime

退出码：0=成功  1=队列有未解决文件  2=拉取失败  3=队列消费失败  9=安全闸拦截
#>
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

# 配置文件必须是 false 或缺失（缺失=不开启）；.env 必须是 false；kill switch 必须是 true
if ($cfgSync -eq $true) { Log "  ❌ 安全闸拦截：配置文件 sync_to_cz_review 为 True"; exit 9 }
if ($envSync -ne "false") { Log "  ❌ 安全闸拦截：.env SYNC_TO_CZ_REVIEW 不是 false"; exit 9 }
if ($envSubmit -ne "false") { Log "  ❌ 安全闸拦截：.env SUBMIT_TO_CZ 不是 false"; exit 9 }
if ($kill -ne "true") { Log "  ❌ 安全闸拦截：AUTO_REVIEW_KILL_SWITCH 不是 true"; exit 9 }
Log "  ✅ 安全闸通过：本次运行不会向 CZ 推送任何数据"

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

# ---------- 1) prepare ----------
$out = & $PY -m answer_hub.automation_schedule prepare --plan $PLAN 2>&1
$code = $LASTEXITCODE
if ($code -ne 0) { Log "  ❌ prepare 失败（退出码 $code）"; $out | ForEach-Object { Log ("     $_") }; exit 3 }
$win = ("$out" -join "`n") | ConvertFrom-Json
$from = $win.from_date; $to = $win.to_date
Log ("  1) 窗口: {0} ~ {1}" -f $from, $to)

# ---------- 2) 队列阻塞检查 ----------
foreach ($b in @("pending", "processing", "failed")) {
    $d = Join-Path $QUEUE $b
    $n = @(Get-ChildItem $d -Filter "*.xlsx" -File -ErrorAction SilentlyContinue).Count
    Log ("  2) 桶 {0,-11} {1} 个 xlsx" -f $b, $n)
    if ($n -gt 0) {
        Log ("  ⛔ 队列有未解决的 {0} 文件，跳过 {1} ~ {2}" -f $b, $from, $to)
        & $PY -m answer_hub.automation_schedule failure --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "队列存在未解决的 $b 文件。" 2>&1 | Out-Null
        exit 1
    }
}

# ---------- 3) 拉取（重试 3 次） ----------
$stateFile = "$RT\data\second-part-pull\scheduled-$($from -replace '-','')-$($to -replace '-','')-state.json"
$env:SECOND_PART_QUERY_FROM_DATE = $from
$env:SECOND_PART_QUERY_TO_DATE   = $to
Log ("  3) 拉取 {0} ~ {1}  ->  {2}" -f $from, $to, $stateFile)
$pullOk = $false
for ($i = 1; $i -le 3; $i++) {
    $c = RunPy @("-m","answer_hub.cli","second-part-pull","--profile",$CFG,"--queue-dir",$QUEUE,
                 "--output-dir",$OUT,"--state-file",$stateFile,
                 "--max-pages","0","--exclude-existing-records") "second-part-pull 第 $i/3 次"
    if ($c -eq 0) { $pullOk = $true; break }
    if ($i -lt 3) { Log "     等待 60 秒后重试"; Start-Sleep -Seconds 60 }
}
if (-not $pullOk) {
    Log "  ❌ 拉取连续 3 次失败，游标停在 $from"
    & $PY -m answer_hub.automation_schedule failure --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "第二部分拉取连续 3 次失败。" 2>&1 | Out-Null
    exit 2
}

# ---------- 4) 消费队列（★ 不传 --sync-to-cz-review） ----------
Log "  4) 消费队列（★ 不传 --sync-to-cz-review）"
$c = RunPy @("-m","answer_hub.cli","automation-queue","--queue-dir",$QUEUE,"--standards",$STD,
             "--output-dir",$OUT,"--clustering-mode","direct_mimo","--max-files","10",
             "--stale-after-seconds","7200") "automation-queue"
if ($c -ne 0) {
    Log "  ❌ 队列消费失败（退出码 $c），游标停在 $from"
    & $PY -m answer_hub.automation_schedule failure --plan $PLAN --today (Get-Date -Format "yyyy-MM-dd") --reason "自动化队列退出码：$c" 2>&1 | Out-Null
    exit 3
}

# ---------- 5) commit ----------
$c = RunPy @("-m","answer_hub.automation_schedule","commit","--plan",$PLAN,"--from-date",$from,"--to-date",$to) "commit 推进游标"
if ($c -ne 0) { Log "  ⚠️ commit 失败"; exit 3 }

Log "==================== 调度完成 ✅ ===================="
exit 0
