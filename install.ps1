#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot installer for Hex CLI on Snapdragon X Elite ARM64 Windows.

.DESCRIPTION
    Checks ARM64 + Python 3.11+, creates .shellai/ scaffold, copies the
    default config, downloads the pre-built npurun ARM64 binary from the
    latest GitHub release, and creates a Start Menu shortcut.

.PARAMETER InstallDir
    Where Hex CLI lives. Default: current directory (assumes you already
    cloned the repo here).  When called by Scoop, pass $dir.

.PARAMETER ScoopInstall
    Skip the git clone step (Scoop already extracted the zip).

.PARAMETER NoStartMenu
    Skip creating the Start Menu shortcut.

.PARAMETER NpurunVersion
    Override the npurun release tag to download (e.g. "v1.7.0").
    Default: "latest".

.EXAMPLE
    # Run directly after cloning:
    Set-Location Hex-CLI
    .\install.ps1

.EXAMPLE
    # Remote one-liner:
    irm https://raw.githubusercontent.com/NathanL15/Hex-CLI/main/install.ps1 | iex
#>
[CmdletBinding()]
param(
    [string]  $InstallDir    = $PSScriptRoot,
    [switch]  $ScoopInstall,
    [switch]  $NoStartMenu,
    [string]  $NpurunVersion = "latest"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

function Write-Step  { param([string]$Msg) Write-Host "  ► $Msg" -ForegroundColor Cyan   }
function Write-Ok    { param([string]$Msg) Write-Host "  ✓ $Msg" -ForegroundColor Green  }
function Write-Warn  { param([string]$Msg) Write-Host "  ⚠ $Msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$Msg) Write-Host "  ✗ $Msg" -ForegroundColor Red    }

Write-Host ""
Write-Host "  Hex CLI — installer" -ForegroundColor White
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Architecture check
# ---------------------------------------------------------------------------
Write-Step "Checking CPU architecture …"
$arch = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture
if ($arch -ne [System.Runtime.InteropServices.Architecture]::Arm64) {
    Write-Warn "This machine reports architecture '$arch'."
    Write-Warn "Hex CLI is optimised for Snapdragon X Elite ARM64; it may still work but the NPU path will be disabled."
} else {
    Write-Ok "ARM64 confirmed."
}

# ---------------------------------------------------------------------------
# 2. Python check (3.11+)
# ---------------------------------------------------------------------------
Write-Step "Checking Python …"
$pythonExe = $null
foreach ($candidate in @("py", "python3", "python")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        $verStr = & $candidate --version 2>&1
        if ($verStr -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -ge 3 -and $minor -ge 11) {
                $pythonExe = $candidate
                Write-Ok "Found: $verStr"
                break
            }
        }
    }
}
if (-not $pythonExe) {
    Write-Fail "Python 3.11+ not found. Install from https://python.org and re-run this script."
    exit 1
}

# ---------------------------------------------------------------------------
# 3. pip dependencies
# ---------------------------------------------------------------------------
Write-Step "Installing Python dependencies (numpy, onnxruntime) …"
try {
    & $pythonExe -m pip install --quiet numpy onnxruntime
    Write-Ok "Dependencies installed."
} catch {
    Write-Warn "pip install failed: $_"
    Write-Warn "Run manually: $pythonExe -m pip install numpy onnxruntime"
}

# ---------------------------------------------------------------------------
# 4. .shellai/ scaffold
# ---------------------------------------------------------------------------
Write-Step "Creating .shellai/ scaffold …"
$shellaiDir = Join-Path $InstallDir ".shellai"
foreach ($sub in @("", "logs", "checkpoints")) {
    $p = Join-Path $shellaiDir $sub
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}
Write-Ok ".shellai/ ready."

# ---------------------------------------------------------------------------
# 5. Default config
# ---------------------------------------------------------------------------
$configSrc  = Join-Path $InstallDir "shellai.example.json"
$configDest = Join-Path $InstallDir "shellai.json"
if (Test-Path $configSrc) {
    if (-not (Test-Path $configDest)) {
        Copy-Item $configSrc $configDest
        Write-Ok "Created shellai.json from template."
    } else {
        Write-Ok "shellai.json already exists — not overwritten."
    }
}

# ---------------------------------------------------------------------------
# 6. Download npurun ARM64 binary from GitHub Releases
# ---------------------------------------------------------------------------
Write-Step "Fetching latest npurun binary …"
$npurunDest = Join-Path $InstallDir "npurun-arm64.exe"

$apiUrl = if ($NpurunVersion -eq "latest") {
    "https://api.github.com/repos/NathanL15/Hex-CLI/releases/latest"
} else {
    "https://api.github.com/repos/NathanL15/Hex-CLI/releases/tags/$NpurunVersion"
}

$downloadOk = $false
try {
    $headers = @{ "User-Agent" = "hexcli-installer"; "Accept" = "application/vnd.github+json" }
    $release  = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 15
    $asset    = $release.assets | Where-Object { $_.name -eq "npurun-arm64.exe" } | Select-Object -First 1
    if ($asset) {
        Write-Step "Downloading $($asset.name) from $($release.tag_name) …"
        Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $npurunDest -Headers $headers
        Write-Ok "npurun-arm64.exe downloaded."
        $downloadOk = $true
    } else {
        Write-Warn "No 'npurun-arm64.exe' asset in release $($release.tag_name)."
        Write-Warn "Build it manually: see README.md (Snapdragon X Elite NPU path)."
    }
} catch {
    Write-Warn "Could not download npurun: $_"
    Write-Warn "Run 'hexcli --update' once you have network access."
}

# ---------------------------------------------------------------------------
# 7. Start Menu shortcut
# ---------------------------------------------------------------------------
if (-not $NoStartMenu) {
    Write-Step "Creating Start Menu shortcut …"
    $startMenu  = [System.Environment]::GetFolderPath("Programs")
    $lnkPath    = Join-Path $startMenu "Hex CLI.lnk"
    $targetCmd  = Join-Path $InstallDir "Hex CLI.cmd"

    if (Test-Path $targetCmd) {
        try {
            $shell    = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($lnkPath)
            $shortcut.TargetPath      = "cmd.exe"
            $shortcut.Arguments       = "/c `"$targetCmd`""
            $shortcut.WorkingDirectory = $InstallDir
            $shortcut.Description     = "Hex CLI — local NPU terminal agent"
            $shortcut.IconLocation    = "powershell.exe,0"
            $shortcut.Save()
            Write-Ok "Shortcut created: $lnkPath"
        } catch {
            Write-Warn "Could not create shortcut: $_"
        }
    } else {
        Write-Warn "'Hex CLI.cmd' not found at $InstallDir — shortcut skipped."
    }
}

# ---------------------------------------------------------------------------
# Done
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  Installation complete." -ForegroundColor Green
Write-Host ""
Write-Host "  Next steps:" -ForegroundColor White
if (-not $downloadOk) {
Write-Host "    1. Set up QAIRT SDK + npurun (see README.md)"
Write-Host "    2. Run: hexcli --update     (to download the npurun binary once available)"
} else {
Write-Host "    1. Set QNN_SDK_ROOT and ADSP_LIBRARY_PATH (see README.md)"
}
Write-Host "    Open 'Hex CLI' from Start Menu, or run: python `"$InstallDir\shellai.py`""
Write-Host ""
