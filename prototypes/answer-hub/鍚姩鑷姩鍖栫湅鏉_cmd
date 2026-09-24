@echo off
cd /d "%~dp0"

echo Starting Answer Hub automation dashboard...
echo Open: http://localhost:8501
echo.

powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_streamlit.ps1"

if errorlevel 1 (
    echo.
    echo Startup failed. Keep this window open and copy the error message.
    pause
)
