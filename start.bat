@echo off
setlocal EnableExtensions
title NovaMarket Launcher
cd /d "%~dp0"

rem One-click NovaMarket launcher.
rem
rem   start.bat                         reuse running servers, or start on free ports
rem   start.bat --no-open               start/reuse without opening a browser
rem   start.bat --api-port 8012 --web-port 5174
rem   start.bat --dry-run               show the chosen ports, start nothing
rem
rem The launcher reuses an existing NovaMarket API and UI. It never starts a
rem second NovaControl/NovaMarket API; if NovaMarket is disabled on an existing
rem NovaControl, it enables NovaMarket there.
rem A busy port is skipped and the
rem next free port is tried. Both servers bind to loopback on purpose.

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start.ps1" %*
set "EXITCODE=%errorlevel%"
if not "%EXITCODE%"=="0" (
  echo.
  echo Launcher exited with code %EXITCODE%.
  pause
)
exit /b %EXITCODE%
