# Deploy-time cache-header assertion.
#
# Curls the running server and fails (non-zero exit) if ANY HTML/CSS/JS response
# is missing the no-cache trio. A deployed UI without these headers is how the
# "old cached interface" regression comes back after a deploy.
#
# Usage:
#   powershell -File scripts\check_no_cache.ps1 [-BaseUrl http://127.0.0.1:8000]
#
# GET is required (not HEAD): the index route is GET-only and answers 405 to HEAD.

param(
    [string]$BaseUrl = "http://127.0.0.1:8000"
)

$ErrorActionPreference = "Stop"
$expected = "cache-control: no-cache, no-store, max-age=0, must-revalidate"
$paths = @(
    "/",
    "/static/index.html",
    "/static/styles.css",
    "/static/app.js",
    "/static/js/dom.js",
    "/static/js/state.js",
    "/static/js/render-utils.js",
    "/static/js/render-explore.js",
    "/static/js/render-panels.js",
    "/static/js/voice.js",
    "/static/js/effects.js",
    "/static/nova-mark.svg"
)

$failures = @()
foreach ($path in $paths) {
    # -D - dumps response headers to stdout; -o NUL discards the body.
    $headers = & curl.exe -s -o NUL -D - "$BaseUrl$path"
    if ($LASTEXITCODE -ne 0) {
        $failures += "$path (curl failed)"
        continue
    }
    $statusLine = ($headers | Select-String -Pattern "^HTTP/").Line
    if ($statusLine -notmatch "\s200\s") {
        $failures += "$path ($statusLine)"
        continue
    }
    $hasTrio = $headers | Select-String -SimpleMatch -Quiet -Pattern $expected
    if (-not $hasTrio) {
        $failures += "$path (missing $expected)"
    }
}

if ($failures.Count -gt 0) {
    Write-Error "NO-CACHE CHECK FAILED:"
    $failures | ForEach-Object { Write-Error "  $_" }
    exit 1
}
Write-Host "OK: all $($paths.Count) HTML/CSS/JS responses carry the no-cache trio."
exit 0
