@echo off
setlocal
cd /d "%~dp0.."
set PYTHONPATH=src
".\.venv\Scripts\python.exe" -m novacontrol doctor
