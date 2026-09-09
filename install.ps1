# Install ksc (Kilo x Snowflake Cortex) on Windows.
#
# Usage:
#   irm https://raw.githubusercontent.com/sfc-gh-kkeller/kilo-snowflake-cortex/main/install.ps1 | iex
#
$ErrorActionPreference = "Stop"

$Repo = "https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex.git"
$InstallDir = if ($env:KSC_INSTALL_DIR) { $env:KSC_INSTALL_DIR } else { "$env:LOCALAPPDATA\kilo-snowflake-cortex" }
$BinDir = if ($env:KSC_BIN_DIR) { $env:KSC_BIN_DIR } else { "$env:LOCALAPPDATA\Microsoft\WindowsApps" }

function Write-Ok($msg)   { Write-Host "[OK] $msg" -ForegroundColor Green }
function Write-Info($msg)  { Write-Host "[->] $msg" -ForegroundColor Cyan }
function Write-Warn($msg)  { Write-Host "[!!] $msg" -ForegroundColor Yellow }
function Write-Fail($msg)  { Write-Host "[XX] $msg" -ForegroundColor Red; exit 1 }

Write-Host ""
Write-Host "  Kilo x Snowflake Cortex - Installer" -ForegroundColor Blue
Write-Host ""

# --- Check Python 3.9+ ---
$py = $null
foreach ($cmd in @("python3", "python", "py")) {
    try {
        $ver = & $cmd -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')" 2>$null
        if ($ver) {
            $parts = $ver.Split(".")
            if ([int]$parts[0] -ge 3 -and [int]$parts[1] -ge 9) {
                $py = $cmd
                break
            }
        }
    } catch {}
}

if (-not $py) {
    Write-Fail "Python 3.9+ is required. Install from https://www.python.org/downloads/"
}
Write-Ok "Python: $py ($(& $py --version 2>&1))"

# --- Check / install Kilo ---
$kilo = $null
if (Get-Command kilo -ErrorAction SilentlyContinue) {
    $kilo = (Get-Command kilo).Source
} elseif (Test-Path "$env:USERPROFILE\.kilo\bin\kilo.exe") {
    $kilo = "$env:USERPROFILE\.kilo\bin\kilo.exe"
}

if ($kilo) {
    Write-Ok "Kilo: $kilo"
} else {
    Write-Info "Kilo not found - installing..."
    if (Get-Command npm -ErrorAction SilentlyContinue) {
        npm install -g @kilocode/cli 2>&1 | Select-Object -Last 3
    } else {
        Write-Warn "npm not found. Install Kilo manually:"
        Write-Host "  npm install -g @kilocode/cli"
        Write-Host "  Then re-run this installer."
    }

    if (Get-Command kilo -ErrorAction SilentlyContinue) {
        Write-Ok "Kilo installed"
    } elseif (Test-Path "$env:USERPROFILE\.kilo\bin\kilo.exe") {
        Write-Ok "Kilo installed: $env:USERPROFILE\.kilo\bin\kilo.exe"
    } else {
        Write-Warn "Kilo installation may need a new terminal to take effect."
    }
}

# --- Download / update ksc ---
if (Test-Path "$InstallDir\.git") {
    Write-Info "Updating ksc..."
    git -C $InstallDir pull --quiet 2>$null
    Write-Ok "Updated $InstallDir"
} else {
    Write-Info "Downloading ksc..."
    if (Get-Command git -ErrorAction SilentlyContinue) {
        git clone --quiet $Repo $InstallDir
    } else {
        # Fallback: download zip
        $zipUrl = "https://github.com/sfc-gh-kkeller/kilo-snowflake-cortex/archive/refs/heads/main.zip"
        $zipFile = "$env:TEMP\ksc-install.zip"
        $extractDir = "$env:TEMP\ksc-extract"
        Invoke-WebRequest -Uri $zipUrl -OutFile $zipFile -UseBasicParsing
        Expand-Archive -Path $zipFile -DestinationPath $extractDir -Force
        if (-not (Test-Path $InstallDir)) { New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null }
        Copy-Item -Path "$extractDir\kilo-snowflake-cortex-main\*" -Destination $InstallDir -Recurse -Force
        Remove-Item $zipFile -Force -ErrorAction SilentlyContinue
        Remove-Item $extractDir -Recurse -Force -ErrorAction SilentlyContinue
    }
    Write-Ok "Installed to $InstallDir"
}

# --- Create ksc.cmd wrapper ---
$kscPy = "$InstallDir\proxy\ksc.py"
$kscCmd = "$BinDir\ksc.cmd"

if (-not (Test-Path $BinDir)) { New-Item -ItemType Directory -Path $BinDir -Force | Out-Null }

@"
@echo off
$py "$kscPy" %*
"@ | Set-Content -Path $kscCmd -Encoding ASCII

Write-Ok "ksc -> $kscCmd"

# --- Write model catalog ---
Write-Info "Configuring Snowflake Cortex models in kilo.json..."
& $py $kscPy setup

# --- Done ---
Write-Host ""
Write-Host "  ================================================" -ForegroundColor DarkGray
Write-Ok "Installation complete"
Write-Host "  ================================================" -ForegroundColor DarkGray
Write-Host ""
Write-Host "  Next steps:"
Write-Host "    ksc add myaccount     # add your Snowflake auth profile"
Write-Host "    ksc myaccount         # start proxy + launch Kilo"
Write-Host ""
