@echo off
setlocal
rem One-click NovaControl launcher (double-click or run from cmd).
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  echo [ERROR] No virtual environment found at .venv.
  echo.
  echo Create it once, then re-run this launcher:
  echo   python -m venv .venv
  echo   .venv\Scripts\Activate.ps1
  echo   python -m pip install -e ".[dev]"
  echo.
  pause
  exit /b 1
)

call ".venv\Scripts\activate.bat"

set PORT=8000
if not "%~1"=="" set PORT=%~1

echo Starting NovaControl at http://127.0.0.1:%PORT% ...
echo Press Ctrl+C to stop.
python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port %PORT%
exit /b %errorlevel%
