@echo off
setlocal
rem One-click NovaControl launcher (double-click or run from cmd).
rem   start.bat          -> http://127.0.0.1:8000
rem   start.bat 8001     -> http://127.0.0.1:8001
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] No virtual environment found at .venv
  echo.
  echo Create it once, then re-run this launcher:
  echo   python -m venv .venv
  echo   .venv\Scripts\python.exe -m pip install -e ".[dev]"
  echo.
  pause
  exit /b 1
)

set PORT=8000
if not "%~1"=="" set PORT=%~1

echo Starting NovaControl at http://127.0.0.1:%PORT% ...
echo Press Ctrl+C to stop.
".venv\Scripts\python.exe" -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port %PORT%
set EXITCODE=%errorlevel%
if not "%EXITCODE%"=="0" (
  echo.
  echo [ERROR] NovaControl exited with code %EXITCODE%.
  echo If the port is busy, try:  start.bat 8001
  echo.
  pause
)
exit /b %EXITCODE%