$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $PSScriptRoot
Set-Location $Root

git config core.hooksPath .githooks
Write-Host "Git hooks enabled. Direct pushes to master are now blocked."

