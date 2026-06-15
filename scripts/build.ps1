# Build the agent-chrome-wrapper MCP server into a single-file binary.
#
# Cross-platform: works under both Windows PowerShell 5.1 / PowerShell 7 on
# Windows AND `pwsh` on Linux. Output extension is determined by the host
# (.exe on Windows, no extension on Linux).
#
# Default OS_TARGETS = [windows, linux]. plugin.json points at the
# extensionless `bin/chrome-wrapper`; each per-OS build job emits its own
# native binary, and release.yml's assembly job merges both into a single
# release zip so the host OS picks its native file at install time.
#
# Usage (from plugin root):
#   pwsh -File scripts/build.ps1
#   pwsh -File scripts/build.ps1 -Clean      # remove dist/ build/ first
#   pwsh -File scripts/build.ps1 -Package    # also stage build/stage/agent-chrome-wrapper/
#
# Requires: Python 3.11+ on PATH.

[CmdletBinding()]
param(
    [switch]$Clean,
    [switch]$Package
)

$root = (Resolve-Path "$PSScriptRoot/..").Path
Set-Location $root

# Note: do NOT set $ErrorActionPreference = "Stop" globally. PowerShell 5.1
# wraps native-command stderr as ErrorRecord, which trips Stop semantics for
# tools like PyInstaller that log heavily to stderr. We check $LASTEXITCODE
# after each native call instead.

# PowerShell 5.1 on Windows lacks the automatic $IsWindows / $IsLinux
# variables PowerShell 7+ provides. Derive them ourselves.
if ($null -eq (Get-Variable -Name IsWindows -ErrorAction SilentlyContinue)) {
    $script:IsWindows = ($env:OS -eq "Windows_NT")
    $script:IsLinux   = -not $script:IsWindows
}

$ExeExt = if ($IsWindows) { ".exe" } else { "" }
$ExeName = "chrome-wrapper$ExeExt"

function Write-Step($msg) {
    Write-Host "==> $msg" -ForegroundColor Cyan
}

function Fail($msg) {
    Write-Host "ERROR: $msg" -ForegroundColor Red
    exit 1
}

# 1. Verify Python.
# In CI ($env:CI = "true") prefer `python` on PATH so we get the version that
# actions/setup-python installed. On Windows locally, prefer py.exe -3.
# On Linux, only `python` / `python3` exists.
Write-Step "Checking Python"
$script:PyCmd = $null
$script:PyArgs = @()

$preferPython = ($env:CI -eq "true")

if ($IsWindows -and -not $preferPython -and (Get-Command py.exe -ErrorAction SilentlyContinue)) {
    $verRaw = & py.exe -3 --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "py.exe"
        $script:PyArgs = @("-3")
        Write-Host "    $verRaw (via py.exe)"
    }
}
if (-not $script:PyCmd -and (Get-Command python -ErrorAction SilentlyContinue)) {
    $verRaw = & python --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "python"
        Write-Host "    $verRaw (via python)"
    }
}
if (-not $script:PyCmd -and (Get-Command python3 -ErrorAction SilentlyContinue)) {
    $verRaw = & python3 --version 2>&1
    if ($LASTEXITCODE -eq 0) {
        $script:PyCmd = "python3"
        Write-Host "    $verRaw (via python3)"
    }
}
if (-not $script:PyCmd) {
    Fail "No usable Python found. Install Python 3.11+."
}

function Invoke-Py {
    & $script:PyCmd @script:PyArgs @args
}

# 1b. Isolate plugin + build deps in a project-local virtualenv.
# Modern Linux distros (Ubuntu 23.04+, Debian 12+, Fedora 38+) mark the
# system Python as PEP 668 externally-managed, which blocks `pip install`
# against it; a venv sidesteps that without the --break-system-packages
# override. On Windows the marker doesn't exist, but a venv keeps the
# build hermetic anyway. CI's actions/setup-python interpreter has no
# PEP 668 marker either, so the extra venv-create step there is cheap.
$venvDir = Join-Path $root ".venv"
if ($IsWindows) {
    $venvPy = Join-Path $venvDir "Scripts/python.exe"
} else {
    $venvPy = Join-Path $venvDir "bin/python"
}

# Detect cross-platform .venv contamination: a venv created under WSL/Linux
# and then "touched" by a Windows build (or vice versa) leaves pyvenv.cfg
# pointing at a foreign-OS interpreter while both Scripts/ and bin/ end up
# coexisting. ensurepip then fails with a nonsense mixed path like
# `/usr/bin\python.exe`. Purge the dir and rebuild fresh.
$venvCfg = Join-Path $venvDir "pyvenv.cfg"
if ((Test-Path $venvPy) -and (Test-Path $venvCfg)) {
    $cfgExec = (Select-String -Path $venvCfg -Pattern '^executable\s*=\s*(.+)$' `
                              -ErrorAction SilentlyContinue).Matches[0].Groups[1].Value
    if ($cfgExec) {
        $cfgExec = $cfgExec.Trim()
        $cfgIsWindowsPath = $cfgExec -match '^[A-Za-z]:[\\/]' -or $cfgExec -like '*\*'
        $cfgIsPosixPath   = $cfgExec.StartsWith('/')
        if ( ($IsWindows -and $cfgIsPosixPath) -or
             (-not $IsWindows -and $cfgIsWindowsPath) ) {
            Write-Step "Existing .venv is from a foreign OS ($cfgExec) -- purging"
            Remove-Item -Recurse -Force $venvDir -ErrorAction SilentlyContinue
        }
    }
}

if (-not (Test-Path $venvPy)) {
    Write-Step "Creating virtualenv at .venv/"
    Invoke-Py -m venv $venvDir
    if ($LASTEXITCODE -ne 0) {
        if (-not $IsWindows) {
            Write-Host "    On Debian/Ubuntu, ensure python3-venv is installed:" -ForegroundColor Yellow
            Write-Host "      sudo apt install python3-venv" -ForegroundColor Yellow
        }
        Fail "Failed to create virtualenv at $venvDir."
    }
    if (-not (Test-Path $venvPy)) {
        Fail "venv was created but $venvPy is missing."
    }
}

# Rebind Python launcher to the venv. All subsequent Invoke-Py calls
# (pip install, PyInstaller) now run inside the venv.
$script:PyCmd = $venvPy
$script:PyArgs = @()
Write-Host "    Using $venvPy"

# Verify pip is present. On Ubuntu 24.04 without `python3.12-venv`
# installed, `python3 -m venv` succeeds but ensurepip can't find its
# bundled wheels -- the resulting venv has no pip. Bootstrap it; if
# that also fails, surface the exact apt package to install.
Invoke-Py -m pip --version > $null 2>&1
if ($LASTEXITCODE -ne 0) {
    Write-Step "Bootstrapping pip in venv (ensurepip)"
    Invoke-Py -m ensurepip --upgrade --default-pip
    if ($LASTEXITCODE -ne 0) {
        if (-not $IsWindows) {
            Write-Host "    The venv has no pip and ensurepip cannot bootstrap it." -ForegroundColor Yellow
            Write-Host "    On Debian/Ubuntu, install the per-version venv package, e.g.:" -ForegroundColor Yellow
            Write-Host "      sudo apt install python3.12-venv python3-pip" -ForegroundColor Yellow
            Write-Host "    Then remove the broken .venv/ and re-run:" -ForegroundColor Yellow
            Write-Host "      rm -rf .venv && pwsh scripts/build.ps1" -ForegroundColor Yellow
        }
        Fail "venv has no pip and ensurepip failed."
    }
}

# 2. Ensure plugin + build deps are installed.
Write-Step "Ensuring dependencies (plugin + pyinstaller)"
Invoke-Py -m pip install --quiet --disable-pip-version-check -e ".[build]"
if ($LASTEXITCODE -ne 0) {
    Fail "pip install failed."
}

# 3. Clean previous build artifacts if requested.
if ($Clean) {
    Write-Step "Cleaning dist/ and build/"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue dist, build
}

# 4. Run PyInstaller.
Write-Step "Running PyInstaller"
Invoke-Py -m PyInstaller chrome-wrapper.spec --clean --noconfirm
if ($LASTEXITCODE -ne 0) {
    Fail "PyInstaller build failed."
}

$exe = Join-Path $root "dist/$ExeName"
if (-not (Test-Path $exe)) {
    Fail "Expected dist/$ExeName not produced."
}
$exeSize = [math]::Round((Get-Item $exe).Length / 1MB, 1)
Write-Host "    dist/$ExeName (${exeSize} MB)"

# 5. Copy into bin/ where plugin.json expects it.
Write-Step "Copying to bin/$ExeName"
New-Item -ItemType Directory -Force -Path "bin" | Out-Null

if ($IsWindows) {
    # Retries the copy because Defender briefly locks freshly-emitted .exe files.
    # If the lock turns out to be a running chrome-wrapper.exe (i.e. the dev's
    # own Claude Code session has the plugin loaded), surface that clearly.
    $copied = $false
    for ($i = 0; $i -lt 5; $i++) {
        try {
            Copy-Item -Force $exe "bin/$ExeName" -ErrorAction Stop
            $copied = $true
            break
        } catch [System.IO.IOException] {
            Write-Host "    file locked (try $($i+1)/5), retrying..." -ForegroundColor Yellow
            Start-Sleep -Milliseconds 800
        }
    }
    if (-not $copied) {
        $running = @(Get-Process -Name chrome-wrapper -ErrorAction SilentlyContinue)
        if ($running.Count -gt 0) {
            $procPids = ($running | ForEach-Object { $_.Id }) -join ", "
            Write-Host "    chrome-wrapper.exe is still running (PID: $procPids)." -ForegroundColor Yellow
            Write-Host "    A Claude Code session likely has the plugin's MCP server loaded."
            Write-Host "    Close it (or run '/mcp' and disconnect 'chrome-wrapper') and re-run the build."
            Write-Host "    To kill it now without that:   Stop-Process -Name chrome-wrapper -Force"
        }
        Fail "Could not copy dist/$ExeName to bin/ -- file remained locked."
    }
} else {
    Copy-Item -Force $exe "bin/$ExeName"
    # Linux binary needs the exec bit. PyInstaller already sets it on dist/,
    # but be explicit so a later `cp` without -p doesn't drop it.
    chmod +x "bin/$ExeName"
}

# 6. Smoke-test: MCP initialize handshake.
# PowerShell 5.1's Process StreamWriter prepends a UTF-8 BOM that MCP rejects.
# Work around it by staging the request in a temp file and using
# Start-Process -RedirectStandardInput, which pipes raw OS bytes.
Write-Step "Smoke-testing the binary (MCP initialize)"
$initMsg = '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2024-11-05","capabilities":{},"clientInfo":{"name":"build-smoke","version":"1"}}}'
$inFile = [System.IO.Path]::GetTempFileName()
$outFile = [System.IO.Path]::GetTempFileName()
$errFile = [System.IO.Path]::GetTempFileName()
[System.IO.File]::WriteAllBytes($inFile, [System.Text.Encoding]::UTF8.GetBytes($initMsg + "`n"))
$proc = Start-Process -FilePath "bin/$ExeName" `
    -RedirectStandardInput $inFile `
    -RedirectStandardOutput $outFile `
    -RedirectStandardError $errFile `
    -NoNewWindow -PassThru
if (-not $proc.WaitForExit(8000)) { $proc.Kill(); Start-Sleep -Milliseconds 200 }
$stdout = (Get-Content -Raw -ErrorAction SilentlyContinue $outFile)
$stderrText = (Get-Content -Raw -ErrorAction SilentlyContinue $errFile)
Remove-Item -ErrorAction SilentlyContinue $inFile, $outFile, $errFile
if ($stdout -match '"result"' -and $stdout -match '"protocolVersion"') {
    Write-Host "    handshake OK" -ForegroundColor Green
} else {
    Write-Host "    stdout: $stdout" -ForegroundColor Yellow
    Write-Host "    stderr: $stderrText" -ForegroundColor Yellow
    Fail "Handshake failed -- see output above."
}

# 7. Optional: stage build/stage/agent-chrome-wrapper/ for the assembly step in
# release.yml. NOTE: -Package on its own emits a *partial* stage tree
# containing only the binary for this OS -- release.yml's assembly job merges
# the per-OS stages and writes the final zip.
if ($Package) {
    Write-Step "Staging install-ready files"
    $stage = Join-Path $root "build/stage/agent-chrome-wrapper"
    Remove-Item -Recurse -Force -ErrorAction SilentlyContinue (Join-Path $root "build/stage")
    New-Item -ItemType Directory -Force -Path $stage | Out-Null
    Copy-Item -Recurse -Force ".claude-plugin" $stage
    Copy-Item -Recurse -Force "bin" $stage
    if (Test-Path "skills") {
        Copy-Item -Recurse -Force "skills" $stage
    }
    Copy-Item -Force "README.md" $stage -ErrorAction SilentlyContinue
    Copy-Item -Force "LICENSE" $stage -ErrorAction SilentlyContinue
    Write-Host "    build/stage/agent-chrome-wrapper (this-OS payload only)"
}

Write-Step "Done."
Write-Host "bin/$ExeName is ready. plugin.json points at 'bin/chrome-wrapper.exe' for this Windows-only plugin."
