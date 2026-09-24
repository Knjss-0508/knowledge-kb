@echo off
cd /d "%~dp0"

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start_dify_answer_hub.ps1"

if errorlevel 1 (
    echo.
    echo Dify startup failed. Keep this window open and copy the error message.
    pause
)
