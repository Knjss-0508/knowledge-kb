@echo off
setlocal
cd /d "%~dp0"

if "%~1"=="" goto PICK_FILE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_full_flow_test.ps1" -SourcePath "%~1"
goto FINISHED

:PICK_FILE
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\run_full_flow_test.ps1"

:FINISHED
set "EXIT_CODE=%ERRORLEVEL%"

echo.
if "%EXIT_CODE%"=="0" goto SUCCESS
echo Full-flow test did not complete. Review the message above.
goto END

:SUCCESS
echo Full-flow test completed. Open Streamlit and CZ to review the result.

:END
echo.
pause
exit /b %EXIT_CODE%
