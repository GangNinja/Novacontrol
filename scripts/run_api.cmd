@echo off
setlocal
cd /d "%~dp0.."
set PYTHONPATH=src
".\.venv\Scripts\python.exe" -m uvicorn novacontrol.api.app:create_app --factory --reload
