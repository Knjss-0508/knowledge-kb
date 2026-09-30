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
Write-Host "【G2】回滚退路（服务器本地旧服务，停用前应保留；⚠️ 已不是生产路径）"
$p = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "curl -s -o /dev/null -w '%{http_code}' --max-time 8 http://127.0.0.1:8780/health" 2>&1
Res "回滚目标（服务器本地旧服务 :8780）" (("$p").Trim() -eq '200') "HTTP $(("$p").Trim())"
$svc = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=15 -o BatchMode=yes $SRV "systemctl is-active answer-hub-api" 2>&1
Res "answer-hub-api 服务（回滚退路，停用前应为 active）" (("$svc").Trim() -eq 'active') "$(("$svc").Trim())"

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
        #   新格式（2026-09-30 起）：local= | rollback= | tunnel= | e2e=
        #   历史格式：local= | prod= | tunnel=   —— 其中 prod= 探的就是服务器本地旧服务，
        #             与新格式的 rollback= 含义相同，只是当时被误称为「生产」。
        #   两种格式分别按各自的字段名校验，保证历史行不会因为改名而被静默漏检。
        $newLines = @($lines | Where-Object { $_ -match 'rollback=' })
        $oldLines = @($lines | Where-Object { $_ -match 'prod=' })
        $bad = @($lines | Where-Object {
            if ($_ -match 'rollback=') {
                $_ -notmatch 'local=200' -or $_ -notmatch 'rollback=200' -or $_ -notmatch 'tunnel=200' -or $_ -notmatch 'e2e=200'
            } else {
                $_ -notmatch 'local=200' -or $_ -notmatch 'prod=200' -or $_ -notmatch 'tunnel=200'
            }
        })
        Res "全部记录健康" ($bad.Count -eq 0) $(if ($bad.Count -eq 0) { "无异常（新格式 $($newLines.Count) 条 / 历史格式 $($oldLines.Count) 条）" } else { "$($bad.Count) 条异常（含历史自愈测试）" }) ($bad.Count -gt 0)
        $lines | Select-Object -Last 3 | ForEach-Object { Write-Host "        $_" }
    }
}

Write-Host ""
Write-Host "=" * 78
Write-Host ("结果: PASS={0}  WARN={1}  FAIL={2}" -f $pass, $warn, $fail)
Write-Host "=" * 78
