"""hexcli.distribution — Self-update and uninstall helpers for Hex CLI.

Called from hexcli.agent via --update and --uninstall flags.
All logic is stdlib only.
"""
from __future__ import annotations

import json
import shutil
import subprocess
import urllib.request
from pathlib import Path
from typing import Any

_GITHUB_API = "https://api.github.com/repos/NathanL15/Hex-CLI/releases/latest"
_NPURUN_ASSET = "npurun-arm64.exe"
_SHORTCUT_NAME = "Hex CLI.lnk"
_START_MENU = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _print(msg: str) -> None:
    print(f"  {msg}", flush=True)


def _fetch_latest_release() -> dict[str, Any]:
    req = urllib.request.Request(
        _GITHUB_API,
        headers={"Accept": "application/vnd.github+json", "User-Agent": "hexcli"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode())


def _find_asset_url(release: dict[str, Any], asset_name: str) -> str | None:
    for asset in release.get("assets", []):
        if asset.get("name") == asset_name:
            return str(asset["browser_download_url"])
    return None


def _download(url: str, dest: Path) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "hexcli"})
    with urllib.request.urlopen(req, timeout=120) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh)


def _git_pull(install_dir: Path) -> bool:
    """Return True if git pull succeeds, False on any failure."""
    git = shutil.which("git")
    if not git:
        _print("git not found on PATH — skipping source update.")
        return False
    try:
        result = subprocess.run(
            [git, "pull", "--ff-only"],
            cwd=str(install_dir),
            capture_output=True,
            text=True,
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        _print("git pull timed out after 120 s — skipping source update.")
        return False
    if result.returncode == 0:
        _print(result.stdout.strip() or "Already up to date.")
        return True
    _print(f"git pull failed: {result.stderr.strip()}")
    return False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def update(install_dir: Path) -> int:
    """Pull the latest source and refresh the npurun binary.

    Returns an exit code (0 = success, 1 = partial failure, 2 = hard failure).
    """
    print("\n  Hex CLI — self-update\n")

    # 1. Update Python source via git.
    _print("Pulling latest source …")
    _git_pull(install_dir)

    # 2. Fetch latest release metadata from GitHub.
    _print("Checking latest release …")
    try:
        release = _fetch_latest_release()
    except Exception as exc:
        _print(f"GitHub API error: {exc}")
        _print("Source update complete; binary update skipped (no network).")
        return 1

    tag = release.get("tag_name", "unknown")
    _print(f"Latest release: {tag}")

    # 3. Download the npurun binary if a matching asset exists.
    url = _find_asset_url(release, _NPURUN_ASSET)
    if not url:
        _print(f"No '{_NPURUN_ASSET}' asset in {tag} — binary update skipped.")
        return 0

    existing = install_dir / _NPURUN_ASSET
    dest_tmp = install_dir / f"{_NPURUN_ASSET}.tmp"
    _print(f"Downloading {_NPURUN_ASSET} …")
    try:
        _download(url, dest_tmp)
        dest_tmp.replace(existing)
    except Exception as exc:
        dest_tmp.unlink(missing_ok=True)
        _print(f"Download failed: {exc}")
        return 1

    _print(f"npurun updated → {existing}")
    _print("Update complete.")
    return 0


def uninstall(install_dir: Path) -> int:
    """Remove the Start Menu shortcut and optionally purge user data."""
    print("\n  Hex CLI — uninstall\n")

    # 1. Remove Start Menu shortcut.
    shortcut = _START_MENU / _SHORTCUT_NAME
    if shortcut.exists():
        try:
            shortcut.unlink()
            _print(f"Removed shortcut: {shortcut}")
        except OSError as exc:
            _print(f"Could not remove shortcut: {exc}")
    else:
        _print("Start Menu shortcut not found (already removed).")

    # 2. Ask whether to purge per-user data.
    shellai_dir = install_dir / ".shellai"
    if shellai_dir.exists():
        try:
            answer = input(
                "\n  Remove .shellai/ (sessions, memory, telemetry, checkpoints)? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("y", "yes"):
            try:
                shutil.rmtree(shellai_dir)
                _print(f"Removed: {shellai_dir}")
            except OSError as exc:
                _print(f"Could not remove .shellai/: {exc}")
        else:
            _print(".shellai/ kept (run 'Remove-Item -Recurse .shellai' to remove manually).")

    # 3. Remind user to remove the clone / pip package.
    if (install_dir / "pyproject.toml").exists():
        _print("\nTo complete uninstall:  pip uninstall hexcli")
    else:
        _print(f"\nTo complete uninstall, delete the install directory:\n    Remove-Item -Recurse \"{install_dir}\"")

    return 0


def first_run_check(install_dir: Path) -> None:
    """Print first-run setup hints when critical dependencies are missing.

    Runs once per process on every `hexcli` invocation, but prints nothing
    when everything looks healthy — zero noise for existing installs.
    """
    hints: list[str] = []

    # npurun on PATH or in install_dir.
    npurun_on_path = shutil.which("npurun") or shutil.which("npurun.exe")
    npurun_local = (install_dir / "npurun-arm64.exe").exists()
    if not npurun_on_path and not npurun_local:
        hints.append(
            "  npurun not found. Run:  hexcli --update\n"
            "  (or install QAIRT SDK + build from https://github.com/bpbonker/npurun)"
        )

    # ONNX embedding model for memory.
    onnx_model = install_dir / "onnx" / "model_qint8_arm64.onnx"
    if not onnx_model.exists():
        hints.append(
            "  Embedding model missing — semantic memory will be disabled.\n"
            "  Download onnx/model_qint8_arm64.onnx from the release page or README."
        )

    if hints:
        print("\n  ── First-run setup ───────────────────────────────────", flush=True)
        for h in hints:
            print(h, flush=True)
        print(flush=True)
