#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot installer for Hex CLI on Snapdragon X Elite ARM64 Windows.

.DESCRIPTION
    Walks the full setup: ARM64 + Python 3.11+ checks, pip dependencies,
    QAIRT SDK discovery (with guided download instructions if absent — the
    SDK cannot be redistributed), the prebuilt npurun ARM64 binary from
    GitHub Releases, the Qwen3-4B model bundle pull (~2.5 GB), config
    scaffold, Start Menu shortcut, and a final `hexcli --doctor` check.

    Every step that finds its work already done skips it, so re-running
    after fixing one prerequisite is cheap and safe.

.PARAMETER InstallDir
    Where Hex CLI lives. Default: the directory containing this script
    (assumes you already cloned the repo here).

.PARAMETER NoStartMenu
    Skip creating the Start Menu shortcut.

.PARAMETER PullModel
    Pull the model bundle without asking (useful for unattended installs).

.PARAMETER SkipModel
    Never pull the model bundle, even interactively.

.PARAMETER NpurunVersion
    Override the release tag to download npurun from (e.g. "v2.0.0").
    Default: "latest".

.EXAMPLE
    Set-Location Hex-CLI
    .\install.ps1
#>
[CmdletBinding()]
param(
    [string]  $InstallDir    = $PSScriptRoot,
    [switch]  $NoStartMenu,
    [switch]  $PullModel,
    [switch]  $SkipModel,
    [string]  $NpurunVersion = "latest"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

# $PSScriptRoot is not reliably available in param() defaults on 5.1.
if (-not $InstallDir) {
    $InstallDir = Split-Path -Parent $MyInvocation.MyCommand.Path
}

function Write-Step  { param([string]$Msg) Write-Host "  > $Msg" -ForegroundColor Cyan   }
function Write-Ok    { param([string]$Msg) Write-Host "  + $Msg" -ForegroundColor Green  }
function Write-Warn  { param([string]$Msg) Write-Host "  ! $Msg" -ForegroundColor Yellow }
function Write-Fail  { param([string]$Msg) Write-Host "  x $Msg" -ForegroundColor Red    }

$script:CanPrompt = -not [Console]::IsInputRedirected

Write-Host ""
Write-Host "  Hex CLI - installer" -ForegroundColor White
Write-Host ""

# ---------------------------------------------------------------------------
# 1. Architecture
# ---------------------------------------------------------------------------
Write-Step "Checking CPU architecture ..."
$arch = [System.Runtime.InteropServices.RuntimeInformation]::ProcessArchitecture
$isArm64 = ($arch -eq [System.Runtime.InteropServices.Architecture]::Arm64)
if ($isArm64) {
    Write-Ok "ARM64 confirmed."
} else {
    Write-Warn "This machine reports architecture '$arch'."
    Write-Warn "Hex CLI targets Snapdragon X Elite ARM64; the NPU path will not work here."
}

# ---------------------------------------------------------------------------
# 2. Python 3.11+
# ---------------------------------------------------------------------------
Write-Step "Checking Python ..."
$pythonExe = $null
foreach ($candidate in @("py", "python3", "python")) {
    $found = Get-Command $candidate -ErrorAction SilentlyContinue
    if ($found) {
        # Windows PowerShell 5.1 turns native stderr under `2>&1` into a
        # TERMINATING RemoteException while $ErrorActionPreference is Stop.
        # The default WindowsApps python3.exe stub writes "Python was not
        # found..." to stderr, so probing it killed the whole installer
        # instead of falling through to the next candidate.
        $verStr = try { & $candidate --version 2>&1 | Out-String } catch { "" }
        if ($verStr -match "Python (\d+)\.(\d+)") {
            $major = [int]$Matches[1]; $minor = [int]$Matches[2]
            if ($major -eq 3 -and $minor -ge 11) {
                $pythonExe = $candidate
                Write-Ok "Found: $verStr"
                break
            }
        }
    }
}
if (-not $pythonExe) {
    Write-Fail "Python 3.11+ not found. Install from https://python.org (ARM64 build) and re-run."
    exit 1
}

# ---------------------------------------------------------------------------
# 3. pip dependencies (optional extras — the core agent is stdlib-only)
# ---------------------------------------------------------------------------
Write-Step "Installing optional Python dependencies (numpy, onnxruntime) ..."
# A native command's nonzero exit does NOT throw, even under
# $ErrorActionPreference = "Stop", so a try/catch here is dead code that
# reports a failed pip install as success. Check $LASTEXITCODE.
& $pythonExe -m pip install --quiet numpy onnxruntime
if ($LASTEXITCODE -eq 0) {
    Write-Ok "Dependencies installed."
} else {
    Write-Warn "pip install failed (exit $LASTEXITCODE)."
    Write-Warn "The agent still runs without them (semantic memory stays disabled)."
}

# ---------------------------------------------------------------------------
# 4. QAIRT SDK (cannot be redistributed — discover or guide)
# ---------------------------------------------------------------------------
Write-Step "Looking for the Qualcomm QAIRT SDK ..."

function Test-QairtRoot {
    param([string]$Root)
    if (-not $Root) { return $false }
    return (Test-Path (Join-Path $Root "lib\aarch64-windows-msvc")) -and
           (Test-Path (Join-Path $Root "bin\aarch64-windows-msvc")) -and
           (Test-Path (Join-Path $Root "lib\hexagon-v73\unsigned"))
}

function Get-QairtVersionKey {
    # Name sort is wrong and quietly so: "QAIRT_2.9.0" sorts ABOVE
    # "QAIRT_2.47.0", which would hand the launcher a stale SDK and cause the
    # very DLL/stack-overrun failures this discovery exists to prevent.
    param([string]$Name)
    $digits = ($Name -replace "^QAIRT_", "") -split "\." | ForEach-Object {
        $num = ($_ -replace "\D", "")
        if ($num) { [int]$num } else { 0 }
    }
    while ($digits.Count -lt 4) { $digits += 0 }
    return [version]::new($digits[0], $digits[1], $digits[2], $digits[3])
}

$qairtRoot = $null
if ($env:QNN_SDK_ROOT -and (Test-QairtRoot $env:QNN_SDK_ROOT)) {
    $qairtRoot = $env:QNN_SDK_ROOT
} else {
    if ($env:QNN_SDK_ROOT) {
        Write-Warn "QNN_SDK_ROOT is set to '$env:QNN_SDK_ROOT' but lacks the expected"
        Write-Warn "lib/bin aarch64-windows-msvc and lib/hexagon-v73/unsigned layout - ignoring it."
    }
    $stack = "C:\Qualcomm\AIStack"
    if (Test-Path $stack) {
        $cands = Get-ChildItem $stack -Directory -Filter "QAIRT_*" -ErrorAction SilentlyContinue |
                 Sort-Object { Get-QairtVersionKey $_.Name } -Descending
        foreach ($c in $cands) {
            if (Test-QairtRoot $c.FullName) { $qairtRoot = $c.FullName; break }
        }
    }
}

if ($qairtRoot) {
    Write-Ok "QAIRT SDK: $qairtRoot"
} else {
    Write-Warn "QAIRT SDK not found. Qualcomm does not allow redistributing it, so this is"
    Write-Warn "the one manual step. It is a single download + extract:"
    Write-Host ""
    Write-Host "      1. Sign in at https://qpm.qualcomm.com (free Qualcomm account)."
    Write-Host "      2. Download 'Qualcomm AI Runtime (QAIRT) SDK' for Windows ARM64"
    Write-Host "         (tested version: 2.47.x)."
    Write-Host "      3. Extract so that a folder like C:\Qualcomm\AIStack\QAIRT_2.47.0"
    Write-Host "         contains lib\aarch64-windows-msvc, bin\aarch64-windows-msvc,"
    Write-Host "         and lib\hexagon-v73\unsigned."
    Write-Host "      4. Re-run this installer - it will pick the SDK up automatically."
    Write-Host ""
}

# ---------------------------------------------------------------------------
# 5. npurun binary (prebuilt, from GitHub Releases)
# ---------------------------------------------------------------------------
Write-Step "Looking for npurun ..."
$npurunExe = $null
$userProfile = [Environment]::GetFolderPath("UserProfile")
$npurunCandidates = @(
    (Join-Path $userProfile ".cargo\bin\npurun.exe"),
    (Join-Path $InstallDir "npurun-arm64.exe")
)
foreach ($c in $npurunCandidates) {
    if (Test-Path $c) { $npurunExe = $c; break }
}
if (-not $npurunExe) {
    $onPath = Get-Command npurun -ErrorAction SilentlyContinue
    if ($onPath) { $npurunExe = $onPath.Source }
}

if ($npurunExe) {
    Write-Ok "npurun: $npurunExe"
} else {
    Write-Step "Downloading prebuilt npurun (ARM64, MIT/Apache-2.0) ..."
    $npurunDest = Join-Path $InstallDir "npurun-arm64.exe"
    $apiUrl = if ($NpurunVersion -eq "latest") {
        "https://api.github.com/repos/NathanL15/Hex-CLI/releases/latest"
    } else {
        "https://api.github.com/repos/NathanL15/Hex-CLI/releases/tags/$NpurunVersion"
    }
    try {
        $headers = @{ "User-Agent" = "hexcli-installer"; "Accept" = "application/vnd.github+json" }
        $release  = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 15
        $asset    = $release.assets | Where-Object { $_.name -eq "npurun-arm64.exe" } | Select-Object -First 1
        if ($asset) {
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $npurunDest -Headers $headers
            $npurunExe = $npurunDest
            Write-Ok "Downloaded npurun-arm64.exe from release $($release.tag_name)."
        } else {
            Write-Warn "No 'npurun-arm64.exe' asset in release $($release.tag_name)."
        }
    } catch {
        Write-Warn "Could not download npurun: $_"
        Write-Warn "Build from source instead: github.com/bpbonker/npurun (cargo install, MSVC ARM64)."
    }
}

# ---------------------------------------------------------------------------
# 6. Model bundle (~2.5 GB, needs npurun + QAIRT)
# ---------------------------------------------------------------------------
$modelName = "qwen3-4b-instruct-2507"
$modelDir  = Join-Path $env:LOCALAPPDATA "npurun\models\$modelName"

if (Test-Path $modelDir) {
    Write-Ok "Model bundle already present: $modelName"
} elseif (-not ($npurunExe -and $qairtRoot)) {
    Write-Warn "Model pull skipped - it needs both npurun and the QAIRT SDK (see above)."
} elseif ($SkipModel) {
    Write-Warn "Model pull skipped (-SkipModel)."
} else {
    $doPull = $PullModel
    if (-not $doPull -and $script:CanPrompt) {
        $answer = Read-Host "  Pull the $modelName bundle now? (~2.5 GB) [Y/n]"
        $doPull = ($answer -eq "" -or $answer -match "^[Yy]")
    }
    if ($doPull) {
        Write-Step "Pulling $modelName (this downloads ~2.5 GB) ..."
        $env:QNN_SDK_ROOT      = $qairtRoot
        $env:ADSP_LIBRARY_PATH = Join-Path $qairtRoot "lib\hexagon-v73\unsigned"
        $env:PATH = (Join-Path $qairtRoot "bin\aarch64-windows-msvc") + ";" +
                    (Join-Path $qairtRoot "lib\aarch64-windows-msvc") + ";" + $env:PATH
        & $npurunExe pull $modelName
        if ($LASTEXITCODE -eq 0) {
            Write-Ok "Model bundle ready."
        } else {
            Write-Warn "npurun pull failed (exit $LASTEXITCODE). Re-run the installer to retry."
        }
    } else {
        Write-Warn "Model pull deferred. The launcher will offer it on first run."
    }
}

# ---------------------------------------------------------------------------
# 7. Config scaffold
# ---------------------------------------------------------------------------
Write-Step "Creating .shellai/ scaffold ..."
$shellaiDir = Join-Path $InstallDir ".shellai"
foreach ($sub in @("", "logs", "checkpoints")) {
    $p = Join-Path $shellaiDir $sub
    if (-not (Test-Path $p)) { New-Item -ItemType Directory -Path $p | Out-Null }
}
Write-Ok ".shellai/ ready."

$configSrc  = Join-Path $InstallDir "shellai.example.json"
$configDest = Join-Path $InstallDir "shellai.json"
if ((Test-Path $configSrc) -and -not (Test-Path $configDest)) {
    Copy-Item $configSrc $configDest
    Write-Ok "Created shellai.json from the generated template."
}

# ---------------------------------------------------------------------------
# 8. Start Menu shortcut
# ---------------------------------------------------------------------------
if (-not $NoStartMenu) {
    Write-Step "Creating Start Menu shortcut ..."
    $startMenu  = [System.Environment]::GetFolderPath("Programs")
    $lnkPath    = Join-Path $startMenu "Hex CLI.lnk"
    $targetCmd  = Join-Path $InstallDir "Hex CLI.cmd"
    if (Test-Path $targetCmd) {
        try {
            $shell    = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($lnkPath)
            $shortcut.TargetPath       = "cmd.exe"
            $shortcut.Arguments        = "/c `"$targetCmd`""
            $shortcut.WorkingDirectory = $InstallDir
            $shortcut.Description      = "Hex CLI - local NPU terminal agent"
            $icon = Join-Path $InstallDir "assets\hexcli.ico"
            $shortcut.IconLocation = if (Test-Path $icon) { "$icon,0" } else { "powershell.exe,0" }
            $shortcut.Save()
            Write-Ok "Shortcut created: $lnkPath"
        } catch {
            Write-Warn "Could not create shortcut: $_"
        }
    } else {
        Write-Warn "'Hex CLI.cmd' not found at $InstallDir - shortcut skipped."
    }
}

# ---------------------------------------------------------------------------
# 9. Doctor
# ---------------------------------------------------------------------------
Write-Step "Running hexcli --doctor ..."
Push-Location $InstallDir
try {
    & $pythonExe -m hexcli.agent --doctor
    # Same dead-catch trap as the pip step: a nonzero native exit does not
    # throw. --doctor exits 1 when a required check fails.
    if ($LASTEXITCODE -ne 0) {
        Write-Warn "Doctor reported unmet requirements (exit $LASTEXITCODE) - see above."
    }
} catch {
    Write-Warn "Doctor run failed: $_"
} finally {
    Pop-Location
}

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
Write-Host ""
Write-Host "  Installation finished." -ForegroundColor Green
Write-Host ""
$remaining = @()
if (-not $qairtRoot)           { $remaining += "Install the QAIRT SDK (step 4 above), then re-run .\install.ps1" }
if (-not $npurunExe)           { $remaining += "Get npurun (re-run installer with network, or build from source)" }
if (-not (Test-Path $modelDir)) { $remaining += "Pull the model: re-run .\install.ps1 -PullModel" }
if ($remaining.Count -gt 0) {
    Write-Host "  Remaining steps:" -ForegroundColor White
    $i = 1
    foreach ($r in $remaining) { Write-Host "    $i. $r"; $i++ }
} else {
    Write-Host "  Everything is in place. Start Hex CLI from the Start Menu, or run:" -ForegroundColor White
    Write-Host "    python launcher.py"
}
Write-Host ""
