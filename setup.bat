@echo off
setlocal enabledelayedexpansion

:: First-time setup: generate kilo.json and register Snowflake MCP server.
::
::   setup.bat                                interactive
::   setup.bat --account X --user Y           non-interactive (needs SNOWFLAKE_PAT env)

set "SCRIPT_DIR=%~dp0"
set "PROXY=%SCRIPT_DIR%proxy\snowflake-cortex-proxy.py"
set "MCP_YAML=%SCRIPT_DIR%config\snowflake-mcp.yaml"
set "CONFIG_DIR=%APPDATA%\kilo"
set "CONFIG=%CONFIG_DIR%\kilo.json"
if not defined PROXY_PORT set "PROXY_PORT=8080"

:: Parse arguments
set "SF_ACCOUNT="
set "SF_USER="
set "SF_PAT="
set "SF_WAREHOUSE="
set "SF_ROLE="

:parse_args
if "%~1"=="" goto :end_parse
if /i "%~1"=="--account" set "SF_ACCOUNT=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--user" set "SF_USER=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--pat" set "SF_PAT=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--warehouse" set "SF_WAREHOUSE=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--role" set "SF_ROLE=%~2" & shift & shift & goto :parse_args
if /i "%~1"=="--help" goto :show_help
if /i "%~1"=="-h" goto :show_help
shift
goto :parse_args
:end_parse

:: Resolve from env
if not defined SF_ACCOUNT if defined SNOWFLAKE_ACCOUNT set "SF_ACCOUNT=%SNOWFLAKE_ACCOUNT%"
if not defined SF_USER if defined SNOWFLAKE_USER set "SF_USER=%SNOWFLAKE_USER%"
if not defined SF_PAT if defined SNOWFLAKE_PAT set "SF_PAT=%SNOWFLAKE_PAT%"
if not defined SF_WAREHOUSE if defined SNOWFLAKE_WAREHOUSE set "SF_WAREHOUSE=%SNOWFLAKE_WAREHOUSE%"
if not defined SF_ROLE if defined SNOWFLAKE_ROLE set "SF_ROLE=%SNOWFLAKE_ROLE%"

echo ════════════════════════════════════════════════════
echo  Kilo x Snowflake Cortex — Setup
echo ════════════════════════════════════════════════════
echo.

:: Interactive prompts
if not defined SF_ACCOUNT (
    set /p SF_ACCOUNT="Snowflake account identifier (e.g. MYORG-MYACCOUNT): "
) else (
    echo [..] account: %SF_ACCOUNT%
)

if not defined SF_USER (
    set /p SF_USER="Snowflake user (login name / email): "
) else (
    echo [..] user: %SF_USER%
)

if not defined SF_PAT (
    set /p SF_PAT="Programmatic access token (PAT): "
) else (
    echo [..] PAT: ****
)

if not defined SF_WAREHOUSE (
    set /p SF_WAREHOUSE="Warehouse (blank to skip): "
)

if not defined SF_ROLE (
    set /p SF_ROLE="Role (blank for default): "
)

:: Validate
if "%SF_ACCOUNT%"=="" echo [ERROR] account is required & exit /b 1
if "%SF_USER%"=="" echo [ERROR] user is required & exit /b 1
if "%SF_PAT%"=="" echo [ERROR] PAT is required & exit /b 1

:: Find python
where python >nul 2>&1
if %errorlevel% neq 0 (
    where python3 >nul 2>&1
    if %errorlevel% neq 0 (
        echo [ERROR] python3 is required
        exit /b 1
    )
    set "PY=python3"
) else (
    set "PY=python"
)

:: Check snowflake-labs-mcp — removed, use Snowflake managed MCP servers instead

:: Generate models
echo [..] generating model catalog from proxy...
for /f "tokens=*" %%m in ('%PY% "%PROXY%" --print-kilo-models 2^>nul') do set "MODELS_JSON=%%m"

:: Write config via Python
if not exist "%CONFIG_DIR%" mkdir "%CONFIG_DIR%"

%PY% -c "import json,sys,os;from collections import OrderedDict;account='%SF_ACCOUNT%';user='%SF_USER%';pat='%SF_PAT%';wh='%SF_WAREHOUSE%';role='%SF_ROLE%';port='%PROXY_PORT%';mcp_yaml=r'%MCP_YAML%';mcp_bin=r'%MCP_BIN%';setup_mcp='%SETUP_MCP%';config_path=r'%CONFIG%';base_url=f'http://127.0.0.1:{port}/v1';cfg=OrderedDict();cfg['$schema']='https://app.kilo.ai/config.json';cfg['model']='openai/claude-opus-5';cfg['small_model']='openai/claude-haiku-4-5';p=OrderedDict();p['snowflake-cortex']=OrderedDict([('account',account),('user',user),('auth',OrderedDict([('type','pat'),('pat',pat)])),('warehouse',wh),('role',role)]);models=json.loads(open(r'%PROXY%').read().split('MODEL_CATALOG')[0]) if False else {};p['openai']=OrderedDict([('name','Snowflake Cortex'),('api',base_url),('options',OrderedDict([('baseURL',base_url),('apiKey','sk-local-proxy')])),('models',{})]);cfg['provider']=p;f=open(config_path,'w');json.dump(cfg,f,indent=2);f.write('\n');f.close()" 2>nul

if %errorlevel% neq 0 (
    :: Fallback: use the proxy's --print-kilo-models via a temp file
    %PY% "%PROXY%" --print-kilo-models > "%TEMP%\kilo_models.json" 2>nul
    %PY% -c "exec(open(r'%SCRIPT_DIR%setup_helper.py').read())" 2>nul
)

echo.
echo [OK] Setup complete
echo.
echo   Account:   %SF_ACCOUNT%
echo   User:      %SF_USER%
echo   Config:    %CONFIG%
echo   Proxy:     http://127.0.0.1:%PROXY_PORT%
echo.
echo   Next: run.bat
echo.
exit /b 0

:show_help
echo Usage: setup.bat [--account X] [--user Y] [--warehouse W] [--role R]
echo        PAT can be passed via --pat or SNOWFLAKE_PAT env var.
exit /b 0
