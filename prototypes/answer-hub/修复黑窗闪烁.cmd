@echo off
cd /d "%~dp0"

echo Replacing the visible automation task with a hidden background task...
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\repair_black_window.ps1" -IntervalMinutes 1

if errorlevel 1 (
    echo.
    echo Repair failed. Please copy the error message shown in the administrator window.
    pause
    exit /b 1
)

echo.
echo Repair completed. The automation queue will continue running without terminal windows.
pause
