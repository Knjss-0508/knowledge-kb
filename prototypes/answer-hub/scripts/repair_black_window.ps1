param(
    [int]$IntervalMinutes = 1
)

$ErrorActionPreference = "Stop"
$identity = [System.Security.Principal.WindowsIdentity]::GetCurrent()
$principal = New-Object `
    System.Security.Principal.WindowsPrincipal($identity)
$isAdministrator = $principal.IsInRole(
    [System.Security.Principal.WindowsBuiltInRole]::Administrator
)

if (-not $isAdministrator) {
    $arguments = (
        "-NoProfile -ExecutionPolicy Bypass -File `"$PSCommandPath`" " +
        "-IntervalMinutes $IntervalMinutes"
    )
    $process = Start-Process `
        -FilePath "powershell.exe" `
        -Verb RunAs `
        -ArgumentList $arguments `
        -Wait `
        -PassThru
    exit $process.ExitCode
}

$projectRoot = Split-Path -Parent $PSScriptRoot
$installer = Join-Path $PSScriptRoot "install_automation_task.ps1"

& powershell.exe `
    -NoProfile `
    -ExecutionPolicy Bypass `
    -File $installer `
    -ProjectRoot $projectRoot `
    -IntervalMinutes $IntervalMinutes
exit $LASTEXITCODE
