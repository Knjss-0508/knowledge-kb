[CmdletBinding()]
param(
    [string]$ShortcutPath = (
        Join-Path ([Environment]::GetFolderPath("Desktop")) "知识库模型训练控制台.lnk"
    )
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$ConsolePath = Join-Path $PSScriptRoot "runner-console.ps1"
if (-not (Test-Path -LiteralPath $ConsolePath -PathType Leaf)) {
    throw "找不到本机训练控制台：$ConsolePath"
}

$PwshPath = (Get-Command "pwsh.exe" -ErrorAction Stop).Source
$Shell = New-Object -ComObject WScript.Shell
$Shortcut = $Shell.CreateShortcut($ShortcutPath)
$Shortcut.TargetPath = $PwshPath
$Shortcut.Arguments = (
    "-NoProfile -WindowStyle Hidden -File `"$ConsolePath`""
)
$Shortcut.WorkingDirectory = $PSScriptRoot
$Shortcut.IconLocation = "$env:SystemRoot\System32\imageres.dll,73"
$Shortcut.Description = "知识库 Embedding 模型本机 GPU 训练控制台"
$Shortcut.Save()

Write-Output "已创建桌面快捷方式：$ShortcutPath"
