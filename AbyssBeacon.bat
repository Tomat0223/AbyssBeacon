@echo off
setlocal
cd /d "%~dp0"

if not exist "venv\Scripts\python.exe" (
    echo Creating AbyssBeacon virtual environment...
    python -m venv venv || goto :error
)

"venv\Scripts\python.exe" -c "import flask,requests,PIL,playwright,selenium" >nul 2>&1
if errorlevel 1 (
    echo Installing or updating AbyssBeacon requirements...
    "venv\Scripts\python.exe" -m pip install -r requirements.txt || goto :error
)

"venv\Scripts\python.exe" app.py
goto :eof

:error
echo.
echo AbyssBeacon could not start. Review the error above.
pause
