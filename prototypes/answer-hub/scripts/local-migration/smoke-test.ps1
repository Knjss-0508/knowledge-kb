# -*- coding: utf-8 -*-
# Answer Hub 迁移冒烟测试：一次跑完所有关键检查点
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$env:PYTHONIOENCODING = "utf-8"

$VPY = "E:\answer-hub-runtime\venv\Scripts\python.exe"
$RT  = "E:\answer-hub-runtime"
$SRV = "root@<SERVER_HOST>"
$pass = 0; $fail = 0; $warn = 0

function Res($name, $ok, $detail, $isWarn = $false) {
    $tag = if ($ok) { "[PASS]" } elseif ($isWarn) { "[WARN]" } else { "[FAIL]" }
    if ($ok) { $script:pass++ } elseif ($isWarn) { $script:warn++ } else { $script:fail++ }
    Write-Host ("  {0} {1,-42} {2}" -f $tag, $name, $detail)
}

function HttpProbe($url, $headers = @{}, $timeout = 15) {
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-WebRequest -Uri $url -TimeoutSec $timeout -UseBasicParsing -Proxy $null -Headers $headers -ErrorAction Stop
        $sw.Stop()
        return @{ ok = $true; code = $r.StatusCode; ms = [int]$sw.Elapsed.TotalMilliseconds; body = $r.Content }
    } catch {
        $c = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        return @{ ok = $false; code = $c; ms = -1; body = "" }
    }
}

# 从容器内 e2e 探针输出里取某个接口的状态码/错误码；用 [regex] 显式匹配，避免依赖 $Matches 残留值
function E2EStatus($txt, $path) {
    $m = [regex]::Match($txt, 'E2E_(?:OK|FAIL) ' + [regex]::Escape($path) + ' (\S+)')
    if ($m.Success) { return $m.Groups[1].Value } else { return '?' }
}

Write-Host "=" * 78
Write-Host "Answer Hub 迁移冒烟测试   $(Get-Date -Format 'yyyy-MM-dd HH:mm:ss')"
Write-Host "=" * 78

Write-Host ""
Write-Host "【A】本机计划任务"
foreach ($t in @("AnswerHub-API-Local","AnswerHub-Tunnel","AnswerHub-Watch")) {
    $st = Get-ScheduledTask -TaskName $t -ErrorAction SilentlyContinue
    Res $t ($st -and $st.State -in @("Running","Ready")) "状态=$($st.State)"
}

Write-Host ""
Write-Host "【B】本机 Answer Hub API"
$env:GROUP_LLM_API_KEY = [Environment]::GetEnvironmentVariable("GROUP_LLM_API_KEY","User")
$keyLine = (Get-Content "$RT\..\答疑中台知识库Docker容器\prototypes\answer-hub\.env" -Encoding UTF8 -ErrorAction SilentlyContinue) |
           Where-Object { $_ -match '^ANSWER_HUB_API_KEY=' } | Select-Object -First 1
$AHKEY = ($keyLine -replace '^ANSWER_HUB_API_KEY=','').Trim()
$h = @{ "X-Answer-Hub-Key" = $AHKEY }
$r1 = HttpProbe "http://100.72.97.89:8780/health"
Res "本机 /health" ($r1.ok -and $r1.code -eq 200) "HTTP $($r1.code)  $($r1.ms)ms"
$r2 = HttpProbe "http://100.72.97.89:8780/api/v1/automation/control" $h 20
Res "本机 /automation/control" ($r2.ok -and $r2.code -eq 200) "HTTP $($r2.code)  $($r2.ms)ms"
$r3 = HttpProbe "http://100.72.97.89:8780/api/v1/automation/jobs?limit=3" $h 25
Res "本机 /automation/jobs" ($r3.ok -and $r3.code -eq 200) "HTTP $($r3.code)  $($r3.ms)ms"

Write-Host ""
Write-Host "【C】本机数据库"
$db = "$RT\data\answer_hub.db"
Res "数据库存在" (Test-Path $db) ("{0:N0} 字节" -f (Get-Item $db).Length)
$pycode = @"
import sqlite3
con = sqlite3.connect(r'$db')
c = con.cursor()
print('integrity=%s' % c.execute('PRAGMA integrity_check').fetchone()[0])
for t in ('candidates','topic_registry','model_runs','ingestion_records'):
    print('%s=%d' % (t, c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0]))
"@
$pycode | Out-File "$RT\_smoke_db.py" -Encoding utf8
$dbout = (& $VPY "$RT\_smoke_db.py" 2>&1) -join ' | '
Res "DB 完整性" ($dbout -match 'integrity=ok') $dbout

Write-Host ""
Write-Host "【D】本机 GPU 嵌入"
$emb = HttpProbe "http://127.0.0.1:8080/info" @{} 15
Res "TEI /info" ($emb.ok -and $emb.code -eq 200) "HTTP $($emb.code)  $($emb.ms)ms"
if ($emb.ok) {
    try {
        $j = $emb.body | ConvertFrom-Json
        Res "嵌入模型" ($j.model_id -like "*Qwen3-Embedding*") $j.model_id
    } catch {}
}
$body = @{ model = "Qwen/Qwen3-Embedding-0.6B"; input = @("冒烟测试") } | ConvertTo-Json -Depth 4
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $er = Invoke-WebRequest -Uri "http://127.0.0.1:8080/v1/embeddings" -Method Post -Body $body -ContentType "application/json" -TimeoutSec 30 -UseBasicParsing -Proxy $null
    $sw.Stop()
    $ej = $er.Content | ConvertFrom-Json
    Res "嵌入推理" ($ej.data[0].embedding.Count -eq 1024) "维度=$($ej.data[0].embedding.Count)  $([int]$sw.Elapsed.TotalMilliseconds)ms"
} catch { Res "嵌入推理" $false $_.Exception.Message }

Write-Host ""
Write-Host "【E】内网 DeepSeek（真实调用）"
$dbody = @{ model = "deepseek-flash"; messages = @(@{role="user";content="回复两个字：冒烟"}); max_completion_tokens = 512; thinking = @{type="disabled"} } | ConvertTo-Json -Depth 6
try {
    $sw = [System.Diagnostics.Stopwatch]::StartNew()
    $dr = Invoke-WebRequest -Uri "https://tokenhub.zhuanspirit.com/codex/v1/chat/completions" -Method Post `
        -Headers @{ "Authorization" = "Bearer $($env:GROUP_LLM_API_KEY)"; "Content-Type" = "application/json" } `
        -Body $dbody -TimeoutSec 60 -UseBasicParsing -Proxy $null
    $sw.Stop()
    $dj = $dr.Content | ConvertFrom-Json
    $c = $dj.choices[0].message.content
    $reasoningTok = $dj.usage.completion_tokens_details.reasoning_tokens
    Res "DeepSeek 真实调用" (-not [string]::IsNullOrWhiteSpace($c)) "回复='$c' reasoning=$reasoningTok  $([int]$sw.Elapsed.TotalMilliseconds)ms"
} catch { Res "DeepSeek 真实调用" $false $_.Exception.Message }

Write-Host ""
Write-Host "【F】隧道延迟采样（服务器 -> 本机，5 次）"
$shFile = "E:\answer-hub-runtime\_smoke_tun.sh"
$tunCodes = @(); $tunTimes = @()
for ($k = 1; $k -le 3; $k++) {
    $one = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o LogLevel=ERROR $SRV "curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 10 http://127.0.0.1:18780/health" 2>&1
    $parts = ("$one").Trim() -split '\s+'
    if ($parts.Count -ge 2 -and $parts[0] -match '^\d{3}$') {
        $tunCodes += $parts[0]
        $tunTimes += [double]$parts[1]
    } else {
        $tunCodes += "ERR"
    }
    Start-Sleep -Milliseconds 500
}
Res "隧道 3 次全 200" (($tunCodes | Where-Object { $_ -eq '200' }).Count -eq 3) "codes=$($tunCodes -join ',')"
if ($tunTimes.Count -gt 0) {
    $avg = ($tunTimes | Measure-Object -Average).Average * 1000
    $max = ($tunTimes | Measure-Object -Maximum).Maximum * 1000
    Res "隧道延迟" ($avg -lt 500) ("平均 {0:N0}ms  最大 {1:N0}ms" -f $avg, $max) ($avg -ge 200)
}

Write-Host ""
Write-Host "【G】生产路径（新路径：kb-backend 容器 → 隧道 :18780 → 本机 Answer Hub）"
# 真正的生产检查：容器内没有 curl，用 python 探针；地址与密钥都读容器内环境变量，不硬编码、不打印
# 上传方式：本机临时文件 → Get-Content -Raw → 管道给 ssh "cat >"，避免 ssh 内联引号问题
$E2E_PY = @'
import os, urllib.request
u = os.environ.get("ANSWER_HUB_API_BASE_URL") or ""
k = os.environ.get("ANSWER_HUB_API_KEY") or ""
if not u:
    print("E2E_FAIL base-url-missing")
    raise SystemExit(1)
print("E2E_BASE " + u.split("@")[-1])
for path in ["/health", "/api/v1/automation/control"]:
    try:
        req = urllib.request.Request(u + path, headers={"X-Answer-Hub-Key": k})
        with urllib.request.urlopen(req, timeout=15) as r:
            print("E2E_OK %s %s" % (path, r.status))
    except Exception as e:
        print("E2E_FAIL %s %s %s" % (path, getattr(e, "code", "ERR"), type(e).__name__))
'@
$e2ePy = "$RT\_smoke_e2e.py"
Set-Content -LiteralPath $e2ePy -Value $E2E_PY -Encoding ascii
Get-Content -Raw -Encoding ascii $e2ePy |
    & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes -o LogLevel=ERROR $SRV "cat > /tmp/_smoke_e2e.py" 2>&1 | Out-Null
$e2eRaw = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=20 -o BatchMode=yes -o LogLevel=ERROR $SRV "docker cp /tmp/_smoke_e2e.py kb-backend:/tmp/_smoke_e2e.py && docker exec kb-backend python /tmp/_smoke_e2e.py" 2>&1
$e2eTxt = ($e2eRaw | Out-String)
$e2eBase = if ([regex]::Match($e2eTxt, 'E2E_BASE (\S+)').Success) { [regex]::Match($e2eTxt, 'E2E_BASE (\S+)').Groups[1].Value } else { '?' }
$e2eH = E2EStatus $e2eTxt "/health"
$e2eC = E2EStatus $e2eTxt "/api/v1/automation/control"
Res "容器内 e2e 生产路径（容器→隧道→本机）" ($e2eH -eq '200' -and $e2eC -eq '200') "目标=$e2eBase  /health=$e2eH  /automation/control=$e2eC"
$cnt = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "docker ps --filter health=healthy --format '{{.Names}}' | wc -l; docker ps --format '{{.Names}}' | wc -l" 2>&1
$cn = @($cnt | Where-Object { $_ -match '^\d+$' })
if ($cn.Count -ge 2) { Res "服务器容器" ([int]$cn[0] -ge 6) "healthy=$($cn[0]) / total=$($cn[1])" } else { Res "服务器容器" $false "无法读取" }
$web = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "curl -s -o /dev/null -w '%{http_code}' --max-time 15 https://knowledgekb.powerzhuan.cn/" 2>&1
Res "公网站点" (("$web").Trim() -eq '200') "HTTP $(("$web").Trim())"

Write-Host ""
Write-Host "【G2】回滚退路（服务器本地旧服务，已于 2026-09-30 15:51 停用；⚠️ 不是生产路径）"
# ⚠️ 判据已于 2026-09-30 按「停用后的终态」修订（旧判据要求 active + 200，停用后会永久 FAIL）：
#   预期终态 = 服务 inactive + :8780 返回 000 + 单元仍 enabled
#              → 这正是「已按计划停用、随时可 systemctl start 拉回」的正常样子 = PASS
#   只有「偏离终态」才报警（判据与第 2.3 节「确认停用成功」一致）：
#     服务 active 且 :8780 = 200 → 旧服务被启动了（人工回滚，或服务器重启后 enabled 单元自启）→ WARN 人工确认
#     inactive 但 :8780 = 200   → 8780 被别的进程占用，将来回滚会失败 → FAIL
#     active 但 :8780 非 200    → 服务在跑却不服务，退路已损坏 → FAIL
#     inactive 但 is-enabled 非 enabled → 自启配置被误 disable，服务器重启后退路不会自动接回 → WARN
#   ⚠️ 这一组与生产健康无关：生产是否健康只看【G】的 tunnel / e2e 与公网站点。
$p = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8780/health" 2>&1
$pCode = ("$p").Trim()
$svc = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "systemctl is-active answer-hub-api" 2>&1
$svcState = ("$svc").Trim()
$svcEn = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "systemctl is-enabled answer-hub-api" 2>&1
$svcEnabled = ("$svcEn").Trim()
$portOk = ($pCode -eq '000')      # 停用成功：端口无监听
$portUp = ($pCode -eq '200')      # 旧服务（或别的进程）在 8780 上应答
$svcUp  = ($svcState -eq 'active')
$probed = ($pCode -match '^\d{3}$') -and ($svcState -ne '')

if (-not $probed) {
    Res "回滚退路 :8780（已停用，预期 000）" $false "无法判定：HTTP='$pCode' 服务='$svcState'（SSH 或探测失败）"
} elseif ($portUp -and $svcUp) {
    Res "回滚退路 :8780（已停用，预期 000）" $false "HTTP 200 —— ⚠️ 旧服务被启动了，请确认是否有意回滚" $true
} elseif ($portUp) {
    Res "回滚退路 :8780（已停用，预期 000）" $false "HTTP 200 但服务 inactive —— ⚠️ 8780 被别的进程占用，回滚会失败"
} elseif ($portOk -and -not $svcUp) {
    Res "回滚退路 :8780（已停用，预期 000）" $true "HTTP $pCode  （000 = 已按计划停用，端口无监听）"
} else {
    Res "回滚退路 :8780（已停用，预期 000）" $false "HTTP $pCode 但 is-active=$svcState —— ⚠️ 端口状态与服务状态自相矛盾，退路已损坏，需人工查看"
}

if (-not $probed) {
    Res "answer-hub-api 单元（预期 inactive + enabled）" $false "无法判定：is-active='$svcState'"
} elseif ($svcUp -and $portUp) {
    Res "answer-hub-api 单元（预期 inactive + enabled）" $false "is-active=active —— ⚠️ 旧服务在跑（人工回滚？还是服务器重启后 enabled 单元自启？）请确认" $true
} elseif ($svcUp) {
    Res "answer-hub-api 单元（预期 inactive + enabled）" $false "is-active=active 但 :8780 非 200 —— 服务在跑却不服务，退路已损坏"
} elseif ($svcState -ne 'inactive') {
    Res "answer-hub-api 单元（预期 inactive + enabled）" $false "is-active=$svcState —— 既不是预期终态 inactive，也不是 active，需人工查看"
} elseif ($svcEnabled -ne 'enabled') {
    Res "answer-hub-api 单元（预期 inactive + enabled）" $false "inactive 但 is-enabled=$svcEnabled —— ⚠️ 自启配置被误 disable，需 systemctl enable 补回" $true
} else {
    Res "answer-hub-api 单元（预期 inactive + enabled）" $true "is-active=inactive / is-enabled=enabled（已停用、随时可启）"
}

Write-Host ""
Write-Host "【H】观察日志"
$logf = "E:\answer-hub-runtime\observation.log"
if (-not (Test-Path -LiteralPath $logf)) {
    Res "巡检日志文件" $false "不存在: $logf"
} else {
    $all = @(Get-Content -LiteralPath $logf -Encoding UTF8 -ErrorAction SilentlyContinue)
    $lines = @($all | Where-Object { $_ -match '^\d{4}-\d{2}-\d{2} ' })
    Res "巡检记录" ($lines.Count -ge 1) ("$($lines.Count) 条  文件 $((Get-Item -LiteralPath $logf).Length) 字节")
    if ($lines.Count -gt 0) {
        # 字段契约（与 _watch.ps1 对齐）：
        #   新格式（2026-09-30 15:27 起）：local= | rollback= | tunnel= | e2e=
        #   历史格式（2026-09-30 15:22 及之前）：local= | prod= | tunnel=
        #   ⚠️ rollback= 与 prod= 探的都是「服务器本地旧服务 :8780」，**都不参与生产健康判定**：
        #      · 历史行：当时旧服务仍是生产 → prod= 必须 200（保留当时的判据，不改写历史结论）；
        #      · 新格式行：旧服务已于 2026-09-30 15:51 停用 → rollback= 预期 000（停用前的行仍会是 200）
        #        → rollback= 一律只统计、不判定，不再把「已按计划停用」当成异常。
        #   生产判据：新格式行 = local= / tunnel= / e2e=；历史行 = local= / tunnel= / prod=。
        $newLines = @($lines | Where-Object { $_ -match 'rollback=' })
        $oldLines = @($lines | Where-Object { $_ -match 'prod=' -and $_ -notmatch 'rollback=' })
        $bad = @($lines | Where-Object {
            if ($_ -match 'rollback=') {
                $_ -notmatch 'local=200' -or $_ -notmatch 'tunnel=200' -or $_ -notmatch 'e2e=200'
            } else {
                $_ -notmatch 'local=200' -or $_ -notmatch 'tunnel=200' -or $_ -notmatch 'prod=200'
            }
        })
        $rb200 = @($newLines | Where-Object { $_ -match 'rollback=200' })
        $rb000 = @($newLines | Where-Object { $_ -match 'rollback=000' })
        $detail = if ($bad.Count -eq 0) { "无异常（新格式 $($newLines.Count) 条 / 历史格式 $($oldLines.Count) 条）" } else { "$($bad.Count) 条生产字段异常（历史瞬时事件，不可修复；当前故障请看【G】）" }
        Res "记录生产字段健康（local/tunnel/e2e）" ($bad.Count -eq 0) $detail ($bad.Count -gt 0)
        Write-Host ("        rollback= 只统计不判定：200 x {0}（停用前）/ 000 x {1}（停用后，预期）" -f $rb200.Count, $rb000.Count)
        if ($bad.Count -gt 0) {
            $bad | Select-Object -First 3 | ForEach-Object { Write-Host "        异常行: $_" }
        }
        $lines | Select-Object -Last 3 | ForEach-Object { Write-Host "        最近行: $_" }
    }
}

Write-Host ""
Write-Host "=" * 78
Write-Host ("结果: PASS={0}  WARN={1}  FAIL={2}" -f $pass, $warn, $fail)
Write-Host "=" * 78
