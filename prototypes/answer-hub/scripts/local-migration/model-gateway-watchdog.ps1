# Watchdog for the local model-gateway proxy + its reverse SSH tunnel.
#
# Why a watchdog instead of a bare long-running task:
#   a crash of either the python proxy or the ssh tunnel must recover by itself.
#   This script is invoked once per minute by the "ModelGateway-Proxy" scheduled
#   task (Boot trigger + 1 minute repetition, MultipleInstances=IgnoreNew), so it
#   touches nothing at all when everything is already healthy.
#
# Process hygiene:
#   processes are always killed by exact PID (never by image name), and the PID is
#   only trusted after its command line is verified to be ours.
#
# Secrets:
#   never printed, never written to disk. The proxy reads GROUP_LLM_API_KEY,
#   MODEL_GATEWAY_ACCESS_TOKEN and MODEL_GATEWAY_CLIENT_TOKEN from its environment
#   (which the S4U task inherits from the user environment) with a registry
#   fallback. Set the two MODEL_GATEWAY_* tokens once with
#   [Environment]::SetEnvironmentVariable(<name>, <value>, 'User'); their values
#   must never be committed, logged or pasted anywhere.

$ErrorActionPreference = 'Continue'

$RT          = if ($env:MODEL_GATEWAY_RUNTIME_DIR) { $env:MODEL_GATEWAY_RUNTIME_DIR } else { 'E:\answer-hub-runtime' }
$PyExe       = Join-Path $RT 'venv\Scripts\python.exe'
$ProxyScript = Join-Path $RT 'model-gateway-proxy.py'
$ProxyLog    = Join-Path $RT 'model-gateway-proxy.log'
$ProxyStdout = Join-Path $RT 'model-gateway-proxy.stdout.log'
$WatchdogLog = Join-Path $RT 'model-gateway-watchdog.log'
$ProxyPidFile = Join-Path $RT 'model-gateway-proxy.pid'
$TunnelPidFile = Join-Path $RT 'model-gateway-tunnel.pid'

$SshExe     = 'C:\Windows\System32\OpenSSH\ssh.exe'
# No host or address is hard-coded here: export these before registering the task.
$SshTarget  = if ($env:MODEL_GATEWAY_SSH_TARGET) { $env:MODEL_GATEWAY_SSH_TARGET } else { 'root@<SERVER_HOST>' }
$TailscaleIp = if ($env:MODEL_GATEWAY_LOCAL_IP) { $env:MODEL_GATEWAY_LOCAL_IP } else { '<LOCAL_TAILSCALE_IP>' }
$RemotePort = if ($env:MODEL_GATEWAY_PORT) { [int]$env:MODEL_GATEWAY_PORT } else { 19000 }
$LocalPort  = $RemotePort

$ProxyProcName = 'python'
$LockFile = Join-Path $RT 'model-gateway-watchdog.lock'

function Write-Log([string]$Level, [string]$Message) {
    $stamp = Get-Date -Format 'yyyy-MM-ddTHH:mm:ss'
    $path = $WatchdogLog
    try {
        if ((Test-Path $path) -and ((Get-Item $path).Length -gt 3MB)) {
            Move-Item -Force -Path $path -Destination "$path.1"
        }
    } catch { }
    try { Add-Content -Path $path -Value "$stamp [$Level] $Message" -Encoding UTF8 } catch { }
}

# Single-instance guard: a 1-minute timer must never let two watchdogs overlap.
$Lock = $null
try {
    $Lock = [System.IO.File]::Open($LockFile, [System.IO.FileMode]::OpenOrCreate,
        [System.IO.FileAccess]::ReadWrite, [System.IO.FileShare]::None)
} catch {
    Write-Log 'INFO' 'another watchdog run holds the lock; exiting'
    exit 0
}

function Get-ProcCmdLine([int]$ProcessId) {
    try {
        $p = Get-CimInstance Win32_Process -Filter "ProcessId=$ProcessId" -ErrorAction Stop
        if ($p) { return [string]$p.CommandLine }
    } catch { }
    return ''
}

function Stop-OwnedProcess([string]$PidFile, [string]$MustContain) {
    if (-not (Test-Path $PidFile)) { return }
    $raw = (Get-Content -Path $PidFile -ErrorAction SilentlyContinue | Select-Object -First 1)
    $targetPid = 0
    if (-not [int]::TryParse(($raw -as [string]), [ref]$targetPid)) { return }
    if ($targetPid -le 4) { return }
    $cmd = Get-ProcCmdLine $targetPid
    if ($cmd -and ($cmd -like "*$MustContain*")) {
        try {
            Stop-Process -Id $targetPid -Force -ErrorAction Stop
            Write-Log 'WARN' "killed pid=$targetPid ($MustContain)"
        } catch {
            Write-Log 'WARN' "failed to kill pid=$targetPid : $($_.Exception.Message)"
        }
    } else {
        Write-Log 'INFO' "pid=$targetPid in $PidFile no longer matches '$MustContain'; not killing"
    }
    try { Remove-Item -Force $PidFile -ErrorAction SilentlyContinue } catch { }
}

function Get-OwnedProcessIds([string]$MustContain) {
    # Start-Process -PassThru can hand back a short-lived launcher, so a PID file
    # alone is not a reliable handle. Pick processes by exact command-line marker
    # instead, and never by image name.
    $found = @()
    try {
        foreach ($p in (Get-CimInstance Win32_Process -Filter "Name='ssh.exe'" -ErrorAction Stop)) {
            if ($p.CommandLine -and ($p.CommandLine -like "*$MustContain*")) { $found += [int]$p.ProcessId }
        }
    } catch { }
    return $found
}

function Stop-OwnedTunnel([string]$Marker) {
    $ids = Get-OwnedProcessIds $Marker
    if ($ids.Count -eq 0) {
        Write-Log 'INFO' 'no tunnel process matched the marker; nothing to stop'
        return
    }
    foreach ($id in $ids) {
        try {
            Stop-Process -Id $id -Force -ErrorAction Stop
            Write-Log 'WARN' "killed tunnel pid=$id (cmdline matched '$Marker')"
        } catch {
            Write-Log 'WARN' "failed to kill tunnel pid=$id : $($_.Exception.Message)"
        }
    }
    try { Remove-Item -Force $TunnelPidFile -ErrorAction SilentlyContinue } catch { }
}

function Test-Port([string]$TargetHost, [int]$Port, [int]$TimeoutMs = 3000) {
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect($TargetHost, $Port, $null, $null)
        if (-not $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)) { return $false }
        $client.EndConnect($iar)
        return $true
    } catch {
        return $false
    } finally {
        try { $client.Close() } catch { }
    }
}

function Test-ProxyHealth([int]$TimeoutSec = 6) {
    try {
        $resp = Invoke-WebRequest -Uri "http://127.0.0.1:$LocalPort/health" -Method Get `
            -TimeoutSec $TimeoutSec -UseBasicParsing -Proxy $null -ErrorAction Stop
        return ($resp.StatusCode -eq 200)
    } catch {
        return $false
    }
}

function Start-Proxy {
    if (-not (Test-Path $PyExe)) { Write-Log 'ERROR' "python missing: $PyExe"; return }
    if (-not (Test-Path $ProxyScript)) { Write-Log 'ERROR' "proxy script missing: $ProxyScript"; return }
    try {
        $proc = Start-Process -FilePath $PyExe -ArgumentList @($ProxyScript) `
            -WorkingDirectory $RT -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput $ProxyStdout -RedirectStandardError "$ProxyStdout.err"
        Set-Content -Path $ProxyPidFile -Value $proc.Id -Encoding ASCII
        Write-Log 'INFO' "started proxy pid=$($proc.Id)"
    } catch {
        Write-Log 'ERROR' "failed to start proxy: $($_.Exception.Message)"
    }
}

function Start-Tunnel {
    if (-not (Test-Path $SshExe)) { Write-Log 'ERROR' "ssh missing: $SshExe"; return }
    $sshArgs = @(
        '-N',
        '-o', 'StrictHostKeyChecking=no',
        '-o', 'ServerAliveInterval=20',
        '-o', 'ServerAliveCountMax=3',
        '-o', 'ExitOnForwardFailure=yes',
        '-o', 'TCPKeepAlive=yes',
        '-o', 'LogLevel=ERROR',
        '-R', "0.0.0.0:${RemotePort}:${TailscaleIp}:${LocalPort}",
        $SshTarget
    )
    try {
        $proc = Start-Process -FilePath $SshExe -ArgumentList $sshArgs `
            -WorkingDirectory $RT -WindowStyle Hidden -PassThru `
            -RedirectStandardOutput (Join-Path $RT 'model-gateway-tunnel.stdout.log') `
            -RedirectStandardError  (Join-Path $RT 'model-gateway-tunnel.stderr.log')
        Set-Content -Path $TunnelPidFile -Value $proc.Id -Encoding ASCII
        Write-Log 'INFO' "started reverse tunnel pid=$($proc.Id) remote=:$RemotePort"
    } catch {
        Write-Log 'ERROR' "failed to start tunnel: $($_.Exception.Message)"
    }
}

function Test-RemoteTunnel {
    # Probe from the server exactly the way production will use it: a plain HTTP
    # GET to the tunnel port, which the local proxy turns into an HTTPS call to
    # the real gateway. python3 is used instead of bash /dev/tcp so the HTTP
    # response is parsed properly.
    $probe = "python3 -c `"import urllib.request as u; print('TUNNEL_OK', u.urlopen('http://127.0.0.1:$RemotePort/health', timeout=8).status)`" 2>&1"
    $out = & $SshExe -o StrictHostKeyChecking=no -o BatchMode=yes -o ConnectTimeout=12 `
        -o LogLevel=ERROR $SshTarget $probe 2>&1
    $text = ("$out" -join ' ')
    return ($text -match 'TUNNEL_OK 200')
}

# ---- 1. proxy -------------------------------------------------------------
$proxyOk = Test-ProxyHealth
if (-not $proxyOk) {
    Write-Log 'WARN' 'proxy health check failed'
    Stop-OwnedProcess $ProxyPidFile $ProxyProcName
    Start-Proxy
    Start-Sleep -Seconds 3
    $proxyOk = Test-ProxyHealth
    Write-Log ($(if ($proxyOk) { 'INFO' } else { 'ERROR' })) "proxy recovered=$proxyOk"
}

# ---- 2. reverse tunnel ----------------------------------------------------
if ($proxyOk) {
    $tunnelOk = Test-RemoteTunnel
    if (-not $tunnelOk) {
        Write-Log 'WARN' "server 127.0.0.1:$RemotePort did not answer; restarting tunnel"
        Stop-OwnedTunnel "0.0.0.0:$RemotePort`:$TailscaleIp`:$LocalPort"
        Start-Sleep -Seconds 2
        Start-Tunnel
        Start-Sleep -Seconds 5
        $tunnelOk = Test-RemoteTunnel
        Write-Log ($(if ($tunnelOk) { 'INFO' } else { 'ERROR' })) "tunnel recovered=$tunnelOk"
    }
}

try { $Lock.Close() } catch { }
try { $Lock.Dispose() } catch { }
exit 0
