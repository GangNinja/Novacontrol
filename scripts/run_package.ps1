$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$env:PYTHONPATH = "src"
& ".\.venv\Scripts\python.exe" -m novacontrol package
