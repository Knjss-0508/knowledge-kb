<#
.SYNOPSIS
  Registers the self-healing scheduled tasks for the local model-gateway proxy.

.DESCRIPTION
  Two tasks are created, modelled on the existing AnswerHub tunnel task so the
  operational behaviour is identical across this runtime:

    ModelGateway-Proxy   runs model-gateway-watchdog.ps1 once (and every minute)
                         and keeps the python proxy + reverse SSH tunnel alive.
    ModelGateway-Tunnel  holds the reverse SSH tunnel open as its main process.

  Both use:
    Boot trigger             -> recovers after a machine reboot
    Time trigger + 1 minute  -> recovers after a crash
    MultipleInstances=IgnoreNew, ExecutionTimeLimit=PT0S (never killed)
    LogonType=S4U, RunLevel=Highest, StartWhenAvailable=true

  S4U matters: the task runs whether or not the user is logged on, and it
  inherits the user environment, so the proxy reads its secrets from the
  environment and no secret ever touches a file.

  The task XML is built explicitly because
  Register-ScheduledTask -Trigger rejects the same repetition pattern that the
  pre-existing AnswerHub-Tunnel task already runs with.
#>

$ErrorActionPreference = 'Stop'

$RT = if ($env:MODEL_GATEWAY_RUNTIME_DIR) { $env:MODEL_GATEWAY_RUNTIME_DIR } else { 'E:\answer-hub-runtime' }
$PwshExe = if ($env:MODEL_GATEWAY_PWSH) { $env:MODEL_GATEWAY_PWSH } else { 'E:\PowerShell\pwsh.exe' }
$SshExe = 'C:\Windows\System32\OpenSSH\ssh.exe'
# No host or address is hard-coded: export these two before registering the tasks.
$SshTarget = if ($env:MODEL_GATEWAY_SSH_TARGET) { $env:MODEL_GATEWAY_SSH_TARGET } else { 'root@<SERVER_HOST>' }
$LocalIp = if ($env:MODEL_GATEWAY_LOCAL_IP) { $env:MODEL_GATEWAY_LOCAL_IP } else { '<LOCAL_TAILSCALE_IP>' }
$Port = if ($env:MODEL_GATEWAY_PORT) { $env:MODEL_GATEWAY_PORT } else { '19000' }

$sid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value

function Get-TaskXml([string]$Command, [string]$Arguments, [string]$WorkingDirectory) {
@"
<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.3" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo />
  <Principals>
    <Principal id="Author">
      <UserId>$sid</UserId>
      <LogonType>S4U</LogonType>
      <RunLevel>HighestAvailable</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure>
      <Count>3</Count>
      <Interval>PT1M</Interval>
    </RestartOnFailure>
    <StartWhenAvailable>true</StartWhenAvailable>
    <IdleSettings>
      <Duration>PT10M</Duration>
      <WaitTimeout>PT1H</WaitTimeout>
      <StopOnIdleEnd>true</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <UseUnifiedSchedulingEngine>true</UseUnifiedSchedulingEngine>
  </Settings>
  <Triggers>
    <BootTrigger />
    <TimeTrigger>
      <StartBoundary>2026-09-30T12:14:47+08:00</StartBoundary>
      <Repetition>
        <Interval>PT1M</Interval>
        <Duration>P3650D</Duration>
        <StopAtDurationEnd>true</StopAtDurationEnd>
      </Repetition>
    </TimeTrigger>
  </Triggers>
  <Actions Context="Author">
    <Exec>
      <Command>$Command</Command>
      <Arguments>$Arguments</Arguments>
      <WorkingDirectory>$WorkingDirectory</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"@
}

$watchdogXml = Get-TaskXml -Command $PwshExe `
    -Arguments "-NoProfile -NonInteractive -ExecutionPolicy Bypass -File &quot;$RT\model-gateway-watchdog.ps1&quot;" `
    -WorkingDirectory $RT

$tunnelArgs = @(
    '-N',
    '-o', 'StrictHostKeyChecking=no',
    '-o', 'ServerAliveInterval=20',
    '-o', 'ServerAliveCountMax=3',
    '-o', 'ExitOnForwardFailure=yes',
    '-o', 'TCPKeepAlive=yes',
    '-o', 'LogLevel=ERROR',
    '-R', "0.0.0.0:$Port`:$LocalIp`:$Port",
    $SshTarget
) -join ' '
$tunnelXml = Get-TaskXml -Command $SshExe -Arguments $tunnelArgs -WorkingDirectory $RT

Register-ScheduledTask -TaskName 'ModelGateway-Proxy' -Xml $watchdogXml -Force | Out-Null
Register-ScheduledTask -TaskName 'ModelGateway-Tunnel' -Xml $tunnelXml -Force | Out-Null

Get-ScheduledTask -TaskName 'ModelGateway-Proxy', 'ModelGateway-Tunnel' |
    Select-Object TaskName, State | Format-Table -AutoSize
