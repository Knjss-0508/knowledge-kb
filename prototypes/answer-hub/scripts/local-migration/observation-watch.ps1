# -*- coding: utf-8 -*-
# 并行观察期巡检：每次运行追加一行记录到日志，供后续判断切换时机
#
# ── 字段契约（必须与 _smoke.ps1 的【H】解析保持一致）────────────────────
#   ⚠️ 日志文件里同时存在三种行：① 表头注释行；② 历史格式行（含 prod=）；③ 新格式行（含 rollback=/e2e=）。
#      【H】只统计以时间戳开头的行，两种数据格式各按自己的字段名校验，互不误伤。
#   local=<code>(<ms>ms)  本机 Answer Hub（100.72.97.89:8780）自检 ── 生产判据
#   rollback=<code>       服务器本地旧服务（127.0.0.1:8780）
#                         ⚠️ 它已不是生产路径，只是回滚退路
#                         ⚠️ 该服务已于 2026-09-30 15:51 按用户指令 systemctl stop 停用
#                            （只 stop、未 disable，单元文件仍在，随时可 start）
#                            → 停用之后的 rollback= 预期恒为 000：这是「已按计划停用」的
#                              正常值，不是异常，也不参与生产判定
#                         历史行此处字段名为 prod=，含义相同（那时旧服务确实还在跑）
#   tunnel=<code>         服务器 127.0.0.1:18780（SSH 反向隧道回环）= 新生产路径入口 ── 生产判据
#   e2e=<code>            kb-backend 容器内真实调用本机 Answer Hub 业务接口 ── 生产判据
#                         200 = /health 与 /api/v1/automation/control 均 200
#                         FAIL = 缺环境变量、容器/ssh 不可达，或任一接口非 200
#   apiTask= / tunTask=   本机计划任务状态（Running / Ready 均为正常）
#
# 判断生产是否健康只看 tunnel= 与 e2e=（本机端点另看 local=）；
# rollback= 只反映「退路还在不在」，为 000 属预期，绝不能据此判断生产故障。
$ErrorActionPreference = "Continue"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

$ts   = Get-Date -Format "yyyy-MM-dd HH:mm:ss"
$RT   = "E:\answer-hub-runtime"
$log  = "$RT\observation.log"
$api  = "$RT\last-check.json"
$SRV  = "root@<SERVER_HOST>"

# 容器内 e2e 探针：地址与密钥都从容器内环境变量读取（不硬编码），也绝不打印密钥
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

function Probe($url, $timeout = 15) {
    try {
        $sw = [System.Diagnostics.Stopwatch]::StartNew()
        $r = Invoke-WebRequest -Uri $url -TimeoutSec $timeout -UseBasicParsing -Proxy $null -ErrorAction Stop
        $sw.Stop()
        return @{ ok = $true; code = $r.StatusCode; ms = [int]$sw.Elapsed.TotalMilliseconds }
    } catch {
        $c = if ($_.Exception.Response) { [int]$_.Exception.Response.StatusCode } else { 0 }
        return @{ ok = $false; code = $c; ms = -1 }
    }
}

function SshRun($cmd) {
    return (& ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes -o LogLevel=ERROR $SRV $cmd 2>&1)
}

# 服务器上某个 URL 的 HTTP 状态码（只读探测）
function SshHttpCode($url) {
    $parts = ("$(SshRun "curl -s -o /dev/null -w '%{http_code} %{time_total}' --max-time 8 $url")").Trim() -split '\s+'
    if ($parts.Count -ge 1 -and $parts[0] -match '^\d{3}$') { return $parts[0] } else { return "ERR" }
}

# 新生产路径的端到端检查：容器内 python 探针（容器内没有 curl）
function SshE2ECode() {
    try {
        $py = "$RT\_watch_e2e.py"
        Set-Content -LiteralPath $py -Value $E2E_PY -Encoding ascii
        # 用「本机临时文件 + 管道」上传，避免 ssh 内联引号地狱
        Get-Content -Raw -Encoding ascii $py |
            & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=10 -o BatchMode=yes -o LogLevel=ERROR $SRV "cat > /tmp/_watch_e2e.py" 2>&1 | Out-Null
        $out = SshRun "docker cp /tmp/_watch_e2e.py kb-backend:/tmp/_watch_e2e.py && docker exec kb-backend python /tmp/_watch_e2e.py"
        $txt = ($out | Out-String)
        if ($txt -match 'E2E_OK /health 200' -and $txt -match 'E2E_OK /api/v1/automation/control 200') { return "200" }
        return "FAIL"
    } catch {
        return "FAIL"
    }
}

# 本机 API
$local = Probe "http://100.72.97.89:8780/health"
# 回滚退路：服务器本地旧服务（已不是生产路径）
$rollbackCode = SshHttpCode "http://127.0.0.1:8780/health"
# 新生产路径入口：服务器经隧道访问本机
$tunCode = SshHttpCode "http://127.0.0.1:18780/health"
# 新生产路径端到端：容器 -> 隧道 -> 本机业务接口
$e2eCode = SshE2ECode

# 本机任务状态
$apiTask = Get-ScheduledTask -TaskName "AnswerHub-API-Local" -ErrorAction SilentlyContinue
$tunTask = Get-ScheduledTask -TaskName "AnswerHub-Tunnel" -ErrorAction SilentlyContinue

$line = "{0} | local={1}({2}ms) | rollback={3} | tunnel={4} | e2e={5} | apiTask={6} | tunTask={7}" -f `
    $ts, $(if ($local.ok) { $local.code } else { "FAIL" }), $local.ms, $rollbackCode, $tunCode, $e2eCode, `
    $(if ($apiTask) { $apiTask.State } else { "?" }), $(if ($tunTask) { $tunTask.State } else { "?" })

if (-not (Test-Path $log)) { "# Answer Hub 并行观察期巡检日志（每 10 分钟一行）" | Out-File $log -Encoding utf8 }
Add-Content -Path $log -Value $line -Encoding utf8
$line | Out-File $api -Encoding utf8
Write-Host $line
