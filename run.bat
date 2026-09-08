@echo off
setlocal enabledelayedexpansion

:: Launch the Snowflake Cortex proxy and Kilo Code together.
::
::   run.bat                start proxy + kilo
::   run.bat --no-kilo      start proxy only
::   run.bat --stop         stop the proxy
::   run.bat --status       show proxy health

set "SCRIPT_DIR=%~dp0"
set "PROXY=%SCRIPT_DIR%proxy\snowflake-cortex-proxy.py"
if not defined PROXY_PORT set "PROXY_PORT=8080"
set "STATE_DIR=%LOCALAPPDATA%\kilo-snowflake-cortex"
set "PIDFILE=%STATE_DIR%\proxy.pid"
set "LOG=%STATE_DIR%\proxy.log"

if not exist "%STATE_DIR%" mkdir "%STATE_DIR%"

:: Parse arguments
set "LAUNCH_KILO=1"
set "ACTION=start"
:parse_args
if "%~1"=="" goto :end_parse
if /i "%~1"=="--no-kilo" set "LAUNCH_KILO=0" & shift & goto :parse_args
if /i "%~1"=="--stop" set "ACTION=stop" & shift & goto :parse_args
if /i "%~1"=="--status" set "ACTION=status" & shift & goto :parse_args
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help
shift
goto :parse_args
:end_parse

echo ════════════════════════════════════════════════════
echo  Kilo x Snowflake Cortex
echo ════════════════════════════════════════════════════

if "%ACTION%"=="stop" goto :stop_proxy
if "%ACTION%"=="status" goto :show_status

:: --- Check config, run setup if missing ---
set "KILO_CONFIG=%APPDATA%\kilo\kilo.json"
if not exist "%KILO_CONFIG%" (
    echo [..] config not found — running setup...
    call "%SCRIPT_DIR%setup.bat"
    if %errorlevel% neq 0 exit /b 1
)

:: --- Start proxy ---
call :check_healthy
if %errorlevel%==0 (
    echo [OK] proxy already running on port %PROXY_PORT%
    goto :after_proxy
)

:: Check python
where python >nul 2>&1
if %errorlevel% neq 0 (
    where python3 >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] python3 is required but not found on PATH
        exit /b 1
    )
    set "PY=python3"
) else (
    set "PY=python"
)

if not exist "%PROXY%" (
    echo [ERROR] proxy not found at %PROXY%
    exit /b 1
)

echo [..] starting proxy on port %PROXY_PORT% (log: %LOG%)
set "PORT=%PROXY_PORT%"
start /b "" %PY% "%PROXY%" > "%LOG%" 2>&1

:: Wait for healthy
set "TRIES=0"
:wait_healthy
if %TRIES% geq 40 (
    echo [ERROR] proxy did not become healthy in 20s
    type "%LOG%" 2>nul | more +0
    exit /b 1
)
timeout /t 1 /nobreak >nul 2>&1
call :check_healthy
if %errorlevel%==0 (
    echo [OK] proxy up on http://127.0.0.1:%PROXY_PORT%
    goto :after_proxy
)
set /a TRIES+=1
goto :wait_healthy

:after_proxy
if "%LAUNCH_KILO%"=="0" (
    echo [OK] proxy is up. Skipping Kilo ^(--no-kilo^).
    exit /b 0
)

:: Find and launch Kilo
where kilo >nul 2>&1
if %errorlevel% neq 0 (
    if exist "%USERPROFILE%\.kilo\bin\kilo.exe" (
        set "KILO=%USERPROFILE%\.kilo\bin\kilo.exe"
    ) else (
        echo [WARN] Kilo CLI not found.
        echo.
        echo   Install:  npm install -g @kilocode/cli
        echo.
        echo   The proxy is running; install Kilo then run:  kilo
        exit /b 0
    )
) else (
    set "KILO=kilo"
)

echo [OK] launching Kilo
echo.
%KILO%
exit /b 0

:: --- Subroutines ---

:check_healthy
curl -fsS -m 3 "http://127.0.0.1:%PROXY_PORT%/health" >nul 2>&1
exit /b %errorlevel%

:stop_proxy
for /f "tokens=5" %%p in ('netstat -aon ^| findstr ":%PROXY_PORT% " ^| findstr "LISTENING"') do (
    taskkill /PID %%p /F >nul 2>&1
    echo [OK] stopped proxy ^(PID %%p^)
    exit /b 0
)
echo [WARN] proxy not running
exit /b 0

:show_status
call :check_healthy
if %errorlevel%==0 (
    echo [OK] proxy healthy on port %PROXY_PORT%
    curl -fsS -m 3 "http://127.0.0.1:%PROXY_PORT%/health" 2>nul
    echo.
) else (
    echo [WARN] proxy not running
)
exit /b 0

:show_help
echo   run.bat                start proxy + kilo
echo   run.bat --no-kilo      start proxy only
echo   run.bat --stop         stop the proxy
echo   run.bat --status       show proxy health
exit /b 0
