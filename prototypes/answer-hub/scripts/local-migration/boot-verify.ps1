# -*- coding: utf-8 -*-
# 重启后自检：由计划任务 AnswerHub-Boot-Verify 在开机时运行，把状态写进文件，供事后查阅
# 路径固定，ASCII 目录，避免中文路径在 cmd/pwsh 下出问题
#
# ⚠️ 本文件是运行时脚本 E:\answer-hub-runtime\_boot_verify.ps1 的仓库镜像（已脱敏）：
#     服务器公网地址 → <SERVER_HOST>；本机 Tailscale IP → <LOCAL_TAILSCALE_IP>；公网域名 → <PUBLIC_DOMAIN>
#     本仓库为 public，禁止把真实地址、Token、密码写回本文件。
#     实际运行时请在本机用原文件；本镜像仅用于留档与评审。
#
# 背景：本机重启后能否自愈「从未实证」（见 docs/answer-hub-迁移执行记录.md 第六节风险 2）。
#       本脚本只负责「记录」开机后 5 分钟内的状态，不代替验证。
$OutDir = "E:\answer-hub-runtime"
$Log    = Join-Path $OutDir "boot-verification.log"

function W([string]$s) {
  $line = "{0} | {1}" -f (Get-Date -Format 'yyyy-MM-dd HH:mm:ss'), $s
  Add-Content -LiteralPath $Log -Value $line -Encoding UTF8
}

W "================ 本次开机自检开始 ================"
try {
  $os = Get-CimInstance Win32_OperatingSystem
  W "开机时间(LastBootUpTime) = $($os.LastBootUpTime.ToString('yyyy-MM-dd HH:mm:ss'))"
} catch { W "读取开机时间失败: $($_.Exception.Message)" }

# 每 30 秒记一轮，共 10 轮（5 分钟），观察四项任务是否自动拉起
for ($i = 1; $i -le 10; $i++) {
  Start-Sleep -Seconds 30
  $parts = @()
  foreach ($n in @("AnswerHub-API-Local","AnswerHub-Tunnel","AnswerHub-Watch","Docker-Embedding-Watchdog")) {
    try {
      $t = Get-ScheduledTask -TaskName $n -ErrorAction Stop
      $parts += ("{0}={1}/{2}" -f ($n -replace 'AnswerHub-','' -replace 'Docker-Embedding-',''), $t.State, $t.Principal.LogonType)
    } catch { $parts += ("{0}=NOTFOUND" -f $n) }
  }
  # 生产链路探测
  $api = "ERR"
  try { $api = (Invoke-WebRequest "http://<LOCAL_TAILSCALE_IP>:8780/health" -TimeoutSec 6 -UseBasicParsing).StatusCode } catch { $api = "ERR" }

  $tun = "ERR"
  try {
    $r = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes -o LogLevel=ERROR root@<SERVER_HOST> "curl -s -o /dev/null -w '%{http_code}' --max-time 6 http://127.0.0.1:18780/health" 2>&1
    $tun = ("$r").Trim()
  } catch { $tun = "ERR" }

  $pub = "ERR"
  try {
    $r2 = & ssh -o StrictHostKeyChecking=no -o ConnectTimeout=8 -o BatchMode=yes -o LogLevel=ERROR root@<SERVER_HOST> "curl -s -o /dev/null -w '%{http_code}' --max-time 10 https://<PUBLIC_DOMAIN>/ready" 2>&1
    $pub = ("$r2").Trim()
  } catch { $pub = "ERR" }

  # ★ 先把关键结论写进日志：这一步绝不能被后面的检查卡住
  #    刻意用最简单的字符串拼接，不用 -f 格式符（之前 -f 那版整行没写出来）
  $secs = $i * 30
  $line = "第" + $i + "轮(开机后约" + [int]($secs/60) + "分" + ($secs%60) + "秒)"
  $line = $line + " | " + ($parts -join " ")
  $line = $line + " | 本机API=" + $api + " 隧道18780=" + $tun + " 公网=" + $pub
  W $line

  # Docker 检查单独做，并加超时保护（S4U 会话访问 Docker 命名管道可能挂住）
  $dock = "TIMEOUT"
  try {
    $job = Start-Job -ScriptBlock { docker ps --format '{{.Names}}' 2>&1 }
    if (Wait-Job $job -Timeout 20) {
      $c = Receive-Job $job
      $dock = (($c | Where-Object { $_ -match 'embedding' }) -join ',')
      if (-not $dock) { $dock = "(无嵌入容器)" }
    }
    Remove-Job $job -Force -ErrorAction SilentlyContinue
  } catch { $dock = "ERR" }
  W ("           └ 嵌入容器(本机)={0}" -f $dock)
}
W "================ 本次开机自检结束 ================"
