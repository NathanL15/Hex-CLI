#Requires -Version 5.1
<#
.SYNOPSIS
    One-shot installer for Hex CLI on Snapdragon X Elite ARM64 Windows.

.DESCRIPTION
    Walks the full setup: ARM64 + Python 3.11+ checks, pip dependencies,
    QAIRT SDK discovery (with guided download instructions if absent — the
    SDK cannot be redistributed), the prebuilt npurun ARM64 binary from the
    fork's GitHub Releases (github.com/NathanL15/npurun), the Qwen3-4B model bundle pull (~2.5 GB), config
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
    Override the npurun release tag to download (e.g. "v0.2.3") from
    github.com/NathanL15/npurun/releases. Default: "latest".

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
# The machine's architecture, not the shell's: an x64 PowerShell (a Git Bash
# or an x64 VS Code terminal on an ARM64 laptop) would otherwise warn that
# the NPU path will not work on a machine where it does.
$isArm64 = $false
$archText = ""
try {
    $cpuArch = (Get-CimInstance Win32_Processor -ErrorAction Stop | Select-Object -First 1).Architecture
    $isArm64 = ($cpuArch -eq 12)   # 12 = ARM64 in Win32_Processor
    $archText = "Win32_Processor.Architecture=$cpuArch"
} catch {
    $arch = [System.Runtime.InteropServices.RuntimeInformation]::OSArchitecture
    $isArm64 = ($arch -eq [System.Runtime.InteropServices.Architecture]::Arm64)
    $archText = "$arch"
}
if ($isArm64) {
    Write-Ok "ARM64 confirmed."
} else {
    Write-Warn "This machine reports architecture '$archText'."
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

# Same rule as the launcher: the newest valid install under the stack folder
# wins, so an old QNN_SDK_ROOT (pinned before 2.50 shipped) does not hide a
# newer SDK. QNN_SDK_ROOT is used only when nothing newer is installed.
$qairtRoot = $null
$newest = $null
$stack = "C:\Qualcomm\AIStack"
if (Test-Path $stack) {
    $cands = Get-ChildItem $stack -Directory -Filter "QAIRT_*" -ErrorAction SilentlyContinue |
             Sort-Object { Get-QairtVersionKey $_.Name } -Descending
    foreach ($c in $cands) {
        if (Test-QairtRoot $c.FullName) { $newest = $c.FullName; break }
    }
}
if ($env:QNN_SDK_ROOT -and (Test-QairtRoot $env:QNN_SDK_ROOT)) {
    $envKey = Get-QairtVersionKey (Split-Path -Leaf $env:QNN_SDK_ROOT)
    if ($newest -and ((Get-QairtVersionKey (Split-Path -Leaf $newest)) -gt $envKey)) {
        $qairtRoot = $newest
        Write-Warn "QNN_SDK_ROOT points at '$env:QNN_SDK_ROOT'; the launcher uses the newer $(Split-Path -Leaf $newest)."
    } else {
        $qairtRoot = $env:QNN_SDK_ROOT
    }
} else {
    if ($env:QNN_SDK_ROOT) {
        Write-Warn "QNN_SDK_ROOT is set to '$env:QNN_SDK_ROOT' but lacks the expected"
        Write-Warn "lib/bin aarch64-windows-msvc and lib/hexagon-v73/unsigned layout - ignoring it."
    }
    $qairtRoot = $newest
}

if ($qairtRoot) {
    Write-Ok "QAIRT SDK: $qairtRoot"
} else {
    Write-Warn "QAIRT SDK not found. Qualcomm does not allow redistributing it, so this is"
    Write-Warn "the one manual step. It is a single download + extract:"
    Write-Host ""
    Write-Host "      1. Sign in at https://qpm.qualcomm.com (free Qualcomm account)."
    Write-Host "      2. Download 'Qualcomm AI Runtime (QAIRT) SDK' for Windows ARM64"
    Write-Host "         (2.50 or newer; 2.47 works without KV prefix reuse)."
    Write-Host "      3. Extract so that a folder like C:\Qualcomm\AIStack\QAIRT_2.50.0"
    Write-Host "         contains lib\aarch64-windows-msvc, bin\aarch64-windows-msvc,"
    Write-Host "         and lib\hexagon-v73\unsigned."
    Write-Host "      4. Re-run this installer - it will pick the SDK up automatically."
    Write-Host ""
}

# ---------------------------------------------------------------------------
# 5. npurun binary (prebuilt, from the fork's GitHub Releases)
# ---------------------------------------------------------------------------
Write-Step "Looking for npurun ..."

function Get-NpurunVersion {
    # Same 5.1 trap as the Python probe: native stderr under 2>&1 can throw.
    param([string]$Exe)
    $out = try { & $Exe --version 2>&1 | Out-String } catch { "" }
    if ($out -match "(\d+)\.(\d+)\.(\d+)") {
        return [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
    }
    return $null
}

# The build this Hex CLI is written for lives in launcher.py (REQUIRED_NPURUN);
# read it there rather than keeping a second copy that drifts.
$requiredNpurun = $null
$launcherText = Get-Content (Join-Path $InstallDir "launcher.py") -Raw
if ($launcherText -match "REQUIRED_NPURUN\s*=\s*\((\d+),\s*(\d+),\s*(\d+)\)") {
    $requiredNpurun = [version]::new([int]$Matches[1], [int]$Matches[2], [int]$Matches[3])
}

$npurunExe = $null
$npurunOld = $null
$userProfile = [Environment]::GetFolderPath("UserProfile")
$npurunCandidates = @(
    (Join-Path $userProfile ".cargo\bin\npurun.exe"),
    (Join-Path $InstallDir "npurun-arm64.exe")
)
$onPath = Get-Command npurun -ErrorAction SilentlyContinue
if ($onPath) { $npurunCandidates += $onPath.Source }
foreach ($c in $npurunCandidates) {
    if (-not (Test-Path $c)) { continue }
    $v = Get-NpurunVersion $c
    if ((-not $requiredNpurun) -or (-not $v) -or ($v -ge $requiredNpurun)) { $npurunExe = $c; break }
    if (-not $npurunOld) { $npurunOld = "$v at $c" }
}

if ($npurunExe) {
    Write-Ok "npurun: $npurunExe"
} else {
    if ($npurunOld) {
        Write-Warn "npurun $npurunOld is older than the $requiredNpurun this Hex CLI is written for."
    }
    Write-Step "Downloading prebuilt npurun (ARM64, MIT/Apache-2.0) ..."
    $npurunDest = Join-Path $InstallDir "npurun-arm64.exe"
    $apiUrl = if ($NpurunVersion -eq "latest") {
        "https://api.github.com/repos/NathanL15/npurun/releases/latest"
    } else {
        "https://api.github.com/repos/NathanL15/npurun/releases/tags/$NpurunVersion"
    }
    try {
        $headers = @{ "User-Agent" = "hexcli-installer"; "Accept" = "application/vnd.github+json" }
        $release  = Invoke-RestMethod -Uri $apiUrl -Headers $headers -TimeoutSec 15
        $asset    = $release.assets | Where-Object { $_.name -eq "npurun-arm64.exe" } | Select-Object -First 1
        if ($asset) {
            Invoke-WebRequest -Uri $asset.browser_download_url -OutFile $npurunDest -Headers $headers
            $npurunExe = $npurunDest
            Write-Ok "Downloaded npurun-arm64.exe from npurun release $($release.tag_name)."
        } else {
            Write-Warn "No 'npurun-arm64.exe' asset in release $($release.tag_name)."
        }
    } catch {
        Write-Warn "Could not download npurun: $_"
        Write-Warn "Build from source instead: github.com/NathanL15/npurun, branch hexcli-fork (cargo install, MSVC ARM64)."
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
# 7b. Embedding model for semantic memory (MiniLM, ~23 MB). Without these two
# files memory is silently off; the doctor used to be the only thing that
# said so, after the fact.
# ---------------------------------------------------------------------------
Write-Step "Checking the embedding model for memory ..."
$onnxDir   = Join-Path $InstallDir "onnx"
$embedBase = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
$embedFiles = @(
    @{ name = "model_qint8_arm64.onnx"; url = "$embedBase/onnx/model_qint8_arm64.onnx"; min = 1000000 },
    @{ name = "tokenizer.json";         url = "$embedBase/tokenizer.json";             min = 1000 }
)
if (-not (Test-Path $onnxDir)) { New-Item -ItemType Directory -Path $onnxDir | Out-Null }
$embedOk = $true
foreach ($f in $embedFiles) {
    $dest = Join-Path $onnxDir $f.name
    if ((Test-Path $dest) -and ((Get-Item $dest).Length -ge $f.min)) { continue }
    try {
        Invoke-WebRequest -Uri $f.url -OutFile $dest -UseBasicParsing
        if ((Get-Item $dest).Length -lt $f.min) { throw "download too small" }
    } catch {
        $embedOk = $false
        Write-Warn "Could not download $($f.name): $_"
    }
}
if ($embedOk) {
    Write-Ok "Embedding model ready (onnx/)."
} else {
    Write-Warn "Semantic memory stays off until both files are in onnx/; --doctor prints the download commands."
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
            $icon    = Join-Path $InstallDir "assets\hexcli.ico"
            $iconPng = Join-Path $InstallDir "assets\hexcli.png"
            # Windows Terminal when it is installed: text selection works
            # there whatever the console mode (Hex turns QuickEdit off, which
            # kills drag-select in the classic console), the status bar's
            # gauge glyphs render, and redraws are smoother. The profile is
            # registered as a fragment (the supported way for an app to add
            # one) unless the user already has a "Hex CLI" profile of their
            # own. Classic conhost is the fallback; it keeps the taskbar icon
            # launcher.py stamps on the window.
            $wt = Join-Path $env:LOCALAPPDATA "Microsoft\WindowsApps\wt.exe"
            $useTerminal = Test-Path $wt
            if ($useTerminal) {
                $wtSettings = Join-Path $env:LOCALAPPDATA "Packages\Microsoft.WindowsTerminal_8wekyb3d8bbwe\LocalState\settings.json"
                $hasOwnProfile = (Test-Path $wtSettings) -and ((Get-Content $wtSettings -Raw) -match '"name"\s*:\s*"Hex CLI"')
                if (-not $hasOwnProfile) {
                    $fragmentDir = Join-Path $env:LOCALAPPDATA "Microsoft\Windows Terminal\Fragments\Hex CLI"
                    New-Item -ItemType Directory -Force -Path $fragmentDir | Out-Null
                    $fragment = @{
                        profiles = @(@{
                            name                     = "Hex CLI"
                            commandline              = "cmd.exe /c `"$targetCmd`""
                            # Start where a shell would, not inside the Hex CLI
                            # checkout: its AGENTS.md would otherwise ride along
                            # into every casual session's prompt.
                            startingDirectory        = "%USERPROFILE%"
                            icon                     = $(if (Test-Path $iconPng) { $iconPng } else { $null })
                            tabTitle                 = "Hex CLI"
                            suppressApplicationTitle = $true
                            colorScheme              = "One Half Dark"
                            font                     = @{ face = "Cascadia Mono"; size = 11 }
                            padding                  = "10"
                            opacity                  = 96
                            cursorShape              = "bar"
                        })
                    }
                    $fragment | ConvertTo-Json -Depth 4 | Set-Content (Join-Path $fragmentDir "hexcli.json") -Encoding UTF8
                    Write-Ok "Windows Terminal profile registered: Hex CLI"
                }
            }
            $shell    = New-Object -ComObject WScript.Shell
            $shortcut = $shell.CreateShortcut($lnkPath)
            if ($useTerminal) {
                # A compact window: 92 columns fits the 76-column help with
                # the side padding; 28 rows leaves room above the input box.
                $shortcut.TargetPath = $wt
                $shortcut.Arguments  = '--size 92,28 -p "Hex CLI"'
            } else {
                $shortcut.TargetPath = "$env:SystemRoot\System32\conhost.exe"
                $shortcut.Arguments  = "cmd.exe /c `"$targetCmd`""
            }
            $shortcut.WorkingDirectory = $env:USERPROFILE   # a shell's start, not the checkout
            $shortcut.Description      = "Hex CLI - local NPU terminal agent"
            $shortcut.IconLocation = if (Test-Path $icon) { "$icon,0" } else { "powershell.exe,0" }
            $shortcut.Save()
            Write-Ok "Shortcut created: $lnkPath $(if ($useTerminal) { '(Windows Terminal)' } else { '(classic console)' })"
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
    if ($NoStartMenu) {
        Write-Host "  Everything is in place. Run:" -ForegroundColor White
    } else {
        Write-Host "  Everything is in place. Start Hex CLI from the Start Menu, or run:" -ForegroundColor White
    }
    Write-Host "    python launcher.py"
    if (-not $embedOk) {
        Write-Host "  Semantic memory is off until the embedding model is in onnx/ (see above)." -ForegroundColor Yellow
    }
}
Write-Host ""
