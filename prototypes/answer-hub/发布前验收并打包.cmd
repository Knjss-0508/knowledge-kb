@echo off
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File ".\scripts\verify_release.ps1" -BuildPackage
echo.
pause
