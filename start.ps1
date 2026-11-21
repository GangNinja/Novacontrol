# NovaMarket launcher.
#
#   .\start.ps1                       reuse running servers, or start on free ports
#   .\start.ps1 --no-open             start/reuse without opening a browser
#   .\start.ps1 --api-port 8012 --web-port 5174
#   .\start.ps1 --dry-run             print the chosen ports, start nothing
#
# The launcher never starts a second market-capable API. It first looks for an
# existing NovaMarket API on the configured port range; if it finds a NovaControl
# API with NovaMarket disabled, it enables NovaMarket on that same process. An
# old NovaControl build without /market/enable cannot host NovaMarket, so the
# launcher leaves it alone and starts NovaMarket on the next free port. A
# running NovaMarket UI is reused in the same way. Only a missing server is
# started, and only on a free port.

$ErrorActionPreference = "Stop"

$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location -Path $Root
$SmtpEnvironmentFile = Join-Path $Root ".env"
if (Test-Path -LiteralPath $SmtpEnvironmentFile) {
    # Passwords are read only from a quoted literal assignment. PowerShell
    # expressions in this file are never evaluated or mistaken for secrets.
    $SmtpVariablePattern = '^\s*(?:\$env:)?(NOVAMARKET_SMTP_(?:HOST|PORT|USER|PASSWORD|FROM|TLS))\s*=\s*(.*?)\s*$'
    foreach ($Line in Get-Content -LiteralPath $SmtpEnvironmentFile) {
        if ($Line -match $SmtpVariablePattern) {
            $Name = $Matches[1]
            $Value = $Matches[2].Trim()
            if ($Name -eq "NOVAMARKET_SMTP_PASSWORD") {
                if ($Value.Length -lt 2) { continue }
                $FirstCharacter = $Value[0]
                $LastCharacter = $Value[$Value.Length - 1]
                if (($FirstCharacter -ne '"' -or $LastCharacter -ne '"') -and
                    ($FirstCharacter -ne "'" -or $LastCharacter -ne "'")) {
                    continue
                }
                $Value = $Value.Substring(1, $Value.Length - 2) -replace '\s', ''
            }
            elseif ($Value.Length -ge 2) {
                $FirstCharacter = $Value[0]
                $LastCharacter = $Value[$Value.Length - 1]
                if (($FirstCharacter -eq '"' -and $LastCharacter -eq '"') -or
                    ($FirstCharacter -eq "'" -and $LastCharacter -eq "'")) {
                    $Value = $Value.Substring(1, $Value.Length - 2)
                }
            }
            [Environment]::SetEnvironmentVariable($Name, $Value, "Process")
        }
    }
}
# The loopback-only admin key is opt-in in the backend, so a deployed copy
# cannot inherit a predictable credential.  Local runs opt in here; the API
# and Vite child processes inherit this value, keeping proxy auto-connect.
if ([string]::IsNullOrWhiteSpace($env:NOVACONTROL_ADMIN_TOKEN)) {
    $env:NOVACONTROL_ADMIN_TOKEN = "narasimha-novacontrol"
}
$Parent = Split-Path -Parent $Root
$Python = Join-Path $Parent ".venv\Scripts\python.exe"
$WebDir = Join-Path $Root "web"
$DefaultApiPort = 8011
$DefaultWebPort = 5173
$PortSpan = 49

function Show-Usage {
    Write-Host "Usage: start.bat [options]"
    Write-Host ""
    Write-Host "  --api-port N   first API port to try (default 8011)"
    Write-Host "  --web-port N   first web port to try (default 5173)"
    Write-Host "  --no-open      do not open a browser"
    Write-Host "  --dry-run      show the ports that would be used; start nothing"
    Write-Host "  --help         show this help"
    Write-Host ""
    Write-Host "A running NovaMarket API/UI is reused. If a port is busy by another"
    Write-Host "program, the launcher tries the next free port in the range."
}

function Fail {
    param([string]$Message)
    Write-Host ""
    Write-Host "[ERROR] $Message" -ForegroundColor Red
    Write-Host ""
    exit 1
}

# ---- arguments -----------------------------------------------------------
$ApiPort = $DefaultApiPort
$WebPort = $DefaultWebPort
$Open = $true
$DryRun = $false
$Cli = @($args)
$Index = 0
while ($Index -lt $Cli.Count) {
    $Token = [string]$Cli[$Index]
    $Lower = $Token.ToLowerInvariant()
    if ($Lower -eq "--no-open") {
        $Open = $false
        $Index++
    }
    elseif ($Lower -eq "--open") {
        $Open = $true
        $Index++
    }
    elseif ($Lower -eq "--dry-run") {
        $DryRun = $true
        $Index++
    }
    elseif ($Lower -eq "--api-port") {
        if ($Index + 1 -ge $Cli.Count) { Fail "--api-port needs a port number." }
        $Parsed = 0
        if (-not [int]::TryParse([string]$Cli[$Index + 1], [ref]$Parsed)) {
            Fail "--api-port needs a port number between 1 and 65535."
        }
        $ApiPort = $Parsed
        $Index += 2
    }
    elseif ($Lower -eq "--web-port") {
        if ($Index + 1 -ge $Cli.Count) { Fail "--web-port needs a port number." }
        $Parsed = 0
        if (-not [int]::TryParse([string]$Cli[$Index + 1], [ref]$Parsed)) {
            Fail "--web-port needs a port number between 1 and 65535."
        }
        $WebPort = $Parsed
        $Index += 2
    }
    elseif ($Lower -eq "--help" -or $Lower -eq "-h") {
        Show-Usage
        exit 0
    }
    else {
        Fail "Unknown argument: $Token. Run start.bat --help."
    }
}

if ($ApiPort -lt 1 -or $ApiPort -gt 65535) { Fail "The API port must be between 1 and 65535." }
if ($WebPort -lt 1 -or $WebPort -gt 65535) { Fail "The web port must be between 1 and 65535." }
if ($ApiPort -eq $WebPort) { Fail "The API and web ports must be different." }

# ---- prerequisites -------------------------------------------------------
if (-not (Test-Path -LiteralPath $Python)) {
    Fail "No virtual environment found at $Python.`n`nCreate it once from $Parent, then run again:`n  python -m venv .venv`n  .venv\Scripts\python.exe -m pip install -e `".[dev]`""
}
if (-not (Test-Path -LiteralPath (Join-Path $Root "src\novacontrol\api\app.py"))) {
    Fail "The NovaMarket source tree is missing at $Root\src\novacontrol."
}
if (-not (Test-Path -LiteralPath (Join-Path $WebDir "node_modules"))) {
    Fail "The web dependencies are missing.`n`nInstall them once, then run again:`n  cd `"$WebDir`"`n  npm install"
}
if (-not (Get-Command npm -ErrorAction SilentlyContinue)) {
    Fail "npm was not found on PATH. Install Node.js, then run again."
}

# ---- listening ports -----------------------------------------------------
# One snapshot is enough for choosing ports. A port is considered free when no
# TCP listener owns it; the server then binds it in a separate console.
$script:ListeningPorts = @()
try {
    $script:ListeningPorts = @(
        [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties().GetActiveTcpListeners() |
            ForEach-Object { [int]$_.Port }
    )
}
catch {
    $script:ListeningPorts = @()
}

function Find-FreePort {
    param(
        [int]$Start,
        [int]$Span,
        [int]$Exclude = 0
    )
    $Last = [Math]::Min(65535, $Start + $Span)
    for ($Port = $Start; $Port -le $Last; $Port++) {
        if ($Port -eq $Exclude) { continue }
        if ($script:ListeningPorts -notcontains $Port) { return $Port }
    }
    return 0
}

function Get-AuthHeaders {
    param([switch]$Json)
    $Headers = @{}
    if ($Json) { $Headers["Content-Type"] = "application/json" }
    if ($env:NOVACONTROL_API_TOKEN) {
        $Headers["Authorization"] = "Bearer $($env:NOVACONTROL_API_TOKEN)"
    }
    return $Headers
}

# A 401 still proves the route exists on a token-protected API. The UI will ask
# for the token; the launcher must not start a second instance just because it
# cannot read the protected status body.
function Test-NovaMarketApi {
    param([int]$Port)
    try {
        $Body = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/market/status" -Headers (Get-AuthHeaders) -TimeoutSec 5 -ErrorAction Stop
        return ($null -ne $Body -and [string]$Body.subsystem -eq "novamarket")
    }
    catch {
        $Response = $_.Exception.Response
        if ($null -ne $Response -and [int]$Response.StatusCode -eq 401) { return $true }
        return $false
    }
}

function Test-NovaControlApi {
    param([int]$Port)
    try {
        $Body = Invoke-RestMethod -Uri "http://127.0.0.1:$Port/status" -Headers (Get-AuthHeaders) -TimeoutSec 5 -ErrorAction Stop
        return ($null -ne $Body -and [string]$Body.name -eq "NovaControl")
    }
    catch {
        $Response = $_.Exception.Response
        if ($null -ne $Response -and [int]$Response.StatusCode -eq 401) { return $true }
        return $false
    }
}

function Test-NovaMarketUi {
    param([int]$Port)
    try {
        $Body = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
        return ($Body.StatusCode -eq 200 -and ([string]$Body.Content) -match "NovaMarket")
    }
    catch {
        return $false
    }
}

function Enable-NovaMarket {
    param([int]$Port)
    try {
        Invoke-RestMethod -Method Post -Uri "http://127.0.0.1:$Port/market/enable" -Headers (Get-AuthHeaders -Json) -Body "{}" -TimeoutSec 60 -ErrorAction Stop | Out-Null
        return "enabled"
    }
    catch {
        $StatusCode = 0
        if ($null -ne $_.Exception.Response) {
            $StatusCode = [int]$_.Exception.Response.StatusCode
        }
        $Detail = $_.ErrorDetails.Message
        if (-not $Detail) { $Detail = $_.Exception.Message }
        if ($StatusCode -eq 404 -or $StatusCode -eq 405) {
            Write-Host "  This NovaControl build has no /market/enable route (HTTP $StatusCode); leaving it untouched." -ForegroundColor Yellow
            return "unsupported"
        }
        Write-Host "  NovaMarket enable failed: $Detail" -ForegroundColor Yellow
        return "failed"
    }
}

function Wait-Api {
    param([int]$Port)
    $Deadline = (Get-Date).AddSeconds(90)
    while ((Get-Date) -lt $Deadline) {
        try {
            $Body = Invoke-WebRequest -Uri "http://127.0.0.1:$Port/health" -UseBasicParsing -TimeoutSec 2 -ErrorAction Stop
            if ($Body.StatusCode -eq 200) { return $true }
        }
        catch { }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-Market {
    param([int]$Port)
    $Deadline = (Get-Date).AddSeconds(30)
    while ((Get-Date) -lt $Deadline) {
        if (Test-NovaMarketApi -Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Wait-Ui {
    param([int]$Port)
    $Deadline = (Get-Date).AddSeconds(60)
    while ((Get-Date) -lt $Deadline) {
        if (Test-NovaMarketUi -Port $Port) { return $true }
        Start-Sleep -Milliseconds 500
    }
    return $false
}

function Start-Api {
    param([int]$Port)
    $env:PYTHONPATH = Join-Path $Root "src"
    # `title` first: cmd.exe parses a quoted executable correctly after a
    # command keyword, and the console window keeps a readable name.
    $Command = 'title NovaMarket API && "' + $Python + '" -m uvicorn novacontrol.api.app:create_app --factory --host 127.0.0.1 --port ' + $Port + ' --no-access-log'
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $Command -WorkingDirectory $Root | Out-Null
}

function Start-Web {
    param([int]$Port, [int]$ApiPort)
    $env:NOVAMARKET_API_ORIGIN = "http://127.0.0.1:$ApiPort"
    $Command = 'title NovaMarket Web && npm run dev -- --host 127.0.0.1 --port ' + $Port
    Start-Process -FilePath "cmd.exe" -ArgumentList "/k", $Command -WorkingDirectory $WebDir | Out-Null
}

# ---- use an existing API, or choose a free port --------------------------
$ApiFound = 0
$ApiNeedsEnable = $false
$ApiScanEnd = [Math]::Min(65535, $ApiPort + $PortSpan)
for ($Port = $ApiPort; $Port -le $ApiScanEnd; $Port++) {
    if ($script:ListeningPorts -notcontains $Port) { continue }
    if (Test-NovaMarketApi -Port $Port) {
        $ApiFound = $Port
        break
    }
    if (Test-NovaControlApi -Port $Port) {
        if ($DryRun) {
            $ApiFound = $Port
            $ApiNeedsEnable = $true
            break
        }
        Write-Host "Found NovaControl on port $Port without NovaMarket; enabling it there..." -ForegroundColor Cyan
        $EnableResult = Enable-NovaMarket -Port $Port
        if ($EnableResult -eq "unsupported") {
            # This listener is an older NovaControl build. It cannot host the
            # NovaMarket routes, so skip it and let the free-port selection
            # below start the current NovaMarket API instead of aborting.
            continue
        }
        if ($EnableResult -ne "enabled") {
            Fail "NovaControl is already running on port $Port, but NovaMarket could not be enabled there ($EnableResult).`nStop that instance or fix its NovaMarket configuration, then run again."
        }
        if (-not (Wait-Market -Port $Port)) {
            Fail "NovaMarket did not become ready on the existing NovaControl at port $Port."
        }
        $ApiFound = $Port
        break
    }
}

$ApiReused = $ApiFound -ne 0
if ($ApiReused) {
    $ApiPort = $ApiFound
}
else {
    $FreeApi = Find-FreePort -Start $ApiPort -Span $PortSpan
    if ($FreeApi -eq 0) {
        Fail "No free API port was found between $ApiPort and $([Math]::Min(65535, $ApiPort + $PortSpan)). Stop another server or pass --api-port with a different range."
    }
    $ApiPort = $FreeApi
}

# ---- use an existing UI, or choose a free port ---------------------------
$WebFound = 0
$WebScanEnd = [Math]::Min(65535, $WebPort + $PortSpan)
for ($Port = $WebPort; $Port -le $WebScanEnd; $Port++) {
    if ($script:ListeningPorts -notcontains $Port) { continue }
    # Reuse any running NovaMarket UI. A transient proxy hiccup must never
    # start a second UI; the user can reload the existing one.
    if (Test-NovaMarketUi -Port $Port) {
        $WebFound = $Port
        break
    }
}

$WebReused = $WebFound -ne 0
if ($WebReused) {
    $WebPort = $WebFound
}
else {
    $FreeWeb = Find-FreePort -Start $WebPort -Span $PortSpan -Exclude $ApiPort
    if ($FreeWeb -eq 0) {
        Fail "No free web port was found between $WebPort and $([Math]::Min(65535, $WebPort + $PortSpan)). Stop another server or pass --web-port with a different range."
    }
    $WebPort = $FreeWeb
}

if ($DryRun) {
    $ApiState = if ($ApiReused) { "existing" } else { "free, would start" }
    $WebState = if ($WebReused) { "existing" } else { "free, would start" }
    Write-Host "Dry run: API port $ApiPort ($ApiState); web port $WebPort ($WebState)."
    if ($ApiNeedsEnable) {
        Write-Host "Dry run: NovaMarket would be enabled on the existing NovaControl API at port $ApiPort."
    }
    exit 0
}

# ---- start only what is missing -----------------------------------------
if (-not $ApiReused) {
    if (-not [string]::IsNullOrWhiteSpace($env:NOVAMARKET_SMTP_USER) -and
        [string]::IsNullOrWhiteSpace($env:NOVAMARKET_SMTP_PASSWORD)) {
        Fail 'SMTP username is configured but the password is empty. Put a quoted literal in .env: $env:NOVAMARKET_SMTP_PASSWORD = "<new app password>"'
    }
    Write-Host "Starting NovaMarket API on http://127.0.0.1:$ApiPort ..." -ForegroundColor Cyan
    Start-Api -Port $ApiPort
    if (-not (Wait-Api -Port $ApiPort)) {
        Fail "The API did not become ready within 90 seconds.`nRead the 'NovaMarket API' console window for the actual error."
    }
    if (-not (Test-NovaMarketApi -Port $ApiPort)) {
        Write-Host "Enabling NovaMarket ..." -ForegroundColor Cyan
        if (-not (Enable-NovaMarket -Port $ApiPort)) {
            Fail "The API is running on port $ApiPort, but NovaMarket could not be enabled. Read the console window for the error."
        }
        if (-not (Wait-Market -Port $ApiPort)) {
            Fail "NovaMarket did not become ready on port $ApiPort."
        }
    }
}
else {
    Write-Host "Using the running NovaMarket API on http://127.0.0.1:$ApiPort" -ForegroundColor Cyan
    if (-not [string]::IsNullOrWhiteSpace($env:NOVAMARKET_SMTP_HOST) -and
        -not [string]::IsNullOrWhiteSpace($env:NOVAMARKET_SMTP_USER)) {
        Write-Host "The existing API keeps its original SMTP environment. Close its console and rerun start.bat to apply credential changes." -ForegroundColor Yellow
    }
}

if (-not $WebReused) {
    Write-Host "Starting NovaMarket web UI on http://127.0.0.1:$WebPort ..." -ForegroundColor Cyan
    Start-Web -Port $WebPort -ApiPort $ApiPort
    if (-not (Wait-Ui -Port $WebPort)) {
        Fail "The web UI did not become ready within 60 seconds.`nRead the 'NovaMarket Web' console window for the actual error."
    }
}
else {
    Write-Host "Using the running NovaMarket UI on http://127.0.0.1:$WebPort" -ForegroundColor Cyan
}

Write-Host ""
Write-Host "NovaMarket is ready: http://127.0.0.1:$WebPort/#/market" -ForegroundColor Green
Write-Host "API: http://127.0.0.1:$ApiPort"
if ($ApiReused -and $WebReused) {
    Write-Host "Nothing new was started; the running API and UI were reused."
}
if ($env:NOVACONTROL_API_TOKEN) {
    Write-Host "API authentication is enabled; paste your token in the UI."
}
else {
    Write-Host "API authentication is OFF. The API is loopback-only; set NOVACONTROL_API_TOKEN before launching if this PC is shared."
}
Write-Host "Close the 'NovaMarket API' and 'NovaMarket Web' windows to stop them."
Write-Host ""

if ($Open) {
    Start-Process "http://127.0.0.1:$WebPort/#/market" | Out-Null
}
exit 0
