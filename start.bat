@echo off
setlocal EnableExtensions
rem One-click NovaControl launcher.
rem
rem Double-click it, or run it from a terminal as  .\start.bat  [port] [--no-open].
rem
rem   start.bat                 -> http://127.0.0.1:8000, browser opens when ready
rem   start.bat 8001            -> http://127.0.0.1:8001
rem   start.bat 8001 --no-open  -> do not open a browser
cd /d "%~dp0"

rem ---- arguments ---------------------------------------------------------
set "PORT="
set "OPEN=1"
:parse
if "%~1"=="" goto :parsed
if /i "%~1"=="--no-open" (
  set "OPEN=0"
  shift
  goto :parse
)
if /i "%~1"=="--open" (
  set "OPEN=1"
  shift
  goto :parse
)
if not defined PORT (
  set "PORT=%~1"
  shift
  goto :parse
)
echo [ERROR] Unexpected argument: %~1
echo Usage: start.bat [port] [--no-open]
echo.
pause
exit /b 2
:parsed
if not defined PORT set "PORT=8000"

rem ---- environment -------------------------------------------------------
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

rem A venv without the editable install starts uvicorn and dies on an import
rem error; say that plainly instead of showing a traceback the user must read.
".venv\Scripts\python.exe" -c "import novacontrol" >nul 2>nul
if errorlevel 1 (
  echo [ERROR] NovaControl is not installed in .venv
  echo.
  echo Install it once, then re-run this launcher:
  echo   .venv\Scripts\python.exe -m pip install -e ".[dev]"
  echo.
  pause
  exit /b 1
)

rem ---- port --------------------------------------------------------------
set "PORT_NUM="
set /a "PORT_NUM=%PORT%" 2>nul
if not "%PORT_NUM%"=="%PORT%" (
  echo [ERROR] "%PORT%" is not a port number.
  echo Usage: start.bat [port]   e.g. start.bat 8001
  echo.
  pause
  exit /b 2
)
if %PORT_NUM% LSS 1 goto :bad_port
if %PORT_NUM% GTR 65535 goto :bad_port
goto :port_ok
:bad_port
echo [ERROR] Port %PORT% is out of range; use a port between 1 and 65535.
echo.
pause
exit /b 2
:port_ok

netstat -ano | findstr /r /c:":%PORT% .*LISTENING" >nul
if not errorlevel 1 (
  echo [ERROR] Port %PORT% is already in use.
  echo   Stop the other process, or start NovaControl on another port:
  echo     .\start.bat 8001
  echo.
  pause
  exit /b 3
)

rem ---- run ---------------------------------------------------------------
echo Starting NovaControl at http://127.0.0.1:%PORT% ...
echo Press Ctrl+C to stop.
echo.

if "%OPEN%"=="1" (
  where powershell >nul 2>nul && start "" /b powershell -NoProfile -WindowStyle Hidden -Command "$u='http://127.0.0.1:%PORT%/'; for($i=0;$i -lt 120;$i++){ try { if((Invoke-WebRequest -UseBasicParsing -TimeoutSec 1 $u).StatusCode -eq 200){ Start-Process $u; break } } catch {} ; Start-Sleep -Milliseconds 500 }"
)

".venv\Scripts\python.exe" -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port %PORT%
set "EXITCODE=%errorlevel%"

rem Ctrl+C stops the server with 0xC000013A (-1073741510); that is how this
rem launcher is SUPPOSED to end, so it is not an error. 130/143 are the
rem equivalent signals when it is stopped from a shell.
if "%EXITCODE%"=="0" goto :stopped
if "%EXITCODE%"=="-1073741510" goto :stopped
if "%EXITCODE%"=="3221225786" goto :stopped
if "%EXITCODE%"=="130" goto :stopped
if "%EXITCODE%"=="143" goto :stopped

echo.
echo [ERROR] NovaControl exited with code %EXITCODE%.
echo Run it from a terminal to read the full error above.
echo If the port became busy, try:  .\start.bat 8001
echo.
pause
exit /b %EXITCODE%

:stopped
echo.
echo NovaControl stopped.
exit /b 0
