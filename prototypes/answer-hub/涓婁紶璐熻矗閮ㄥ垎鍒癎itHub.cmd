@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"

if /I "%~1"=="--check" (
    if not exist "%~dp0scripts\publish_answer_hub_to_github.ps1" (
        echo ERROR: PowerShell upload script was not found.
        exit /b 1
    )
    echo CMD wrapper check passed.
    exit /b 0
)

echo Starting the answer-hub validation and upload workflow.
echo Review the displayed changes and type PUSH when prompted.
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\publish_answer_hub_to_github.ps1"
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" (
    echo Upload workflow completed.
) else if "%EXIT_CODE%"=="2" (
    echo Upload was cancelled.
) else (
    echo Upload workflow failed with exit code %EXIT_CODE%.
)

pause
exit /b %EXIT_CODE%
