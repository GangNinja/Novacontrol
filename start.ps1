# One-click NovaControl launcher (PowerShell). Optional -Port parameter, default 8000.
#   .\start.ps1
#   .\start.ps1 -Port 8001
param(
    [int]$Port = 8000
)

Set-Location -Path $PSScriptRoot

$Python = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $Python)) {
    Write-Host "[ERROR] No virtual environment found at .venv." -ForegroundColor Red
    Write-Host ""
    Write-Host "Create it once, then re-run this launcher:"
    Write-Host "  python -m venv .venv"
    Write-Host "  .venv\Scripts\Activate.ps1"
    Write-Host "  python -m pip install -e '.[dev]'"
    exit 1
}

& (Join-Path $PSScriptRoot ".venv\Scripts\Activate.ps1")

Write-Host "Starting NovaControl at http://127.0.0.1:$Port ..." -ForegroundColor Cyan
Write-Host "Press Ctrl+C to stop."
& python -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port $Port
exit $LASTEXITCODE
