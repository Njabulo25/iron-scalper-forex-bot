@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python 3 is required. Install Python 3.10 or newer, then run setup.bat again.
    exit /b 1
)

if not exist ".venv\Scripts\python.exe" (
    echo Creating the Iron Scalper Python environment...
    py -3.12 -m venv .venv
    if errorlevel 1 (
        echo Could not create .venv. Install Python 3.12, then run setup.bat again.
        exit /b 1
    )
)

echo Installing dashboard and live-trading dependencies...
".venv\Scripts\python.exe" -m pip install --upgrade pip
if errorlevel 1 exit /b 1
".venv\Scripts\python.exe" -m pip install -r requirements-runtime.txt
if errorlevel 1 (
    echo Dependency installation failed. Check the message above, then run setup.bat again.
    exit /b 1
)

echo Setup complete. Run run.bat to open the dashboard.
exit /b 0
