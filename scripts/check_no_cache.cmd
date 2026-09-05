@echo off
setlocal
rem Deploy-time cache-header assertion (cmd twin of check_no_cache.ps1).
rem Curls / and /static/* and fails if any response is not HTTP 200 or lacks
rem the no-cache trio. Usage: scripts\check_no_cache.cmd [http://127.0.0.1:8000]
set "BASE=%~1"
if "%BASE%"=="" set "BASE=http://127.0.0.1:8000"
set "EXPECTED=cache-control: no-cache, no-store, max-age=0, must-revalidate"
set FAILED=0
set "HDR=%TEMP%\nc_check_headers.tmp"

for %%P in (
    "/"
    "/static/index.html"
    "/static/styles.css"
    "/static/app.js"
    "/static/js/dom.js"
    "/static/js/state.js"
    "/static/js/render-utils.js"
    "/static/js/render-explore.js"
    "/static/js/render-panels.js"
    "/static/js/voice.js"
    "/static/js/effects.js"
    "/static/nova-mark.svg"
) do (
    rem GET (not HEAD): the index route is GET-only and answers 405 to HEAD.
    curl.exe -s -o NUL -D "%HDR%" "%BASE%%%~P" 2>NUL
    findstr /I /C:"HTTP/1.1 200" /C:"HTTP/2 200" "%HDR%" >NUL
    if errorlevel 1 (
        echo FAIL %%~P not HTTP 200
        set FAILED=1
    ) else (
        findstr /I /C:"%EXPECTED%" "%HDR%" >NUL
        if errorlevel 1 (
            echo FAIL %%~P missing cache-control trio
            set FAILED=1
        ) else (
            echo OK   %%~P
        )
    )
)
del "%HDR%" >NUL 2>NUL

if "%FAILED%"=="1" (
    echo NO-CACHE CHECK FAILED.
    exit /b 1
)
echo OK: all HTML/CSS/JS responses carry the no-cache trio.
exit /b 0
