$ErrorActionPreference = "Stop"
Set-Location -Path (Split-Path -Parent $PSScriptRoot)
$env:PYTHONPATH = "src"
.\.venv\Scripts\python.exe -m uvicorn novacontrol.api.app:create_app --factory --reload
