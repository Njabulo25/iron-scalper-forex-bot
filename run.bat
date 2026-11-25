@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    call setup.bat
    if errorlevel 1 goto :failed
)

".venv\Scripts\python.exe" main.py %*
if errorlevel 1 goto :failed
exit /b 0

:failed
echo Iron Scalper did not start. Review the error above, then press any key to close this window.
pause >nul
exit /b 1
