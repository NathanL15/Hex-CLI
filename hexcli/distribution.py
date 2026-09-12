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

# The npurun binary comes from the fork's own releases, not Hex CLI's.
_GITHUB_API = "https://api.github.com/repos/NathanL15/npurun/releases/latest"
_NPURUN_ASSET = "npurun-arm64.exe"
_SHORTCUT_NAME = "Hex CLI.lnk"
_START_MENU = Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs"
# The Windows Terminal profile install.ps1 registers (a fragment, so it never
# edits the user's settings.json). Removed on uninstall.
_WT_FRAGMENT = Path.home() / "AppData" / "Local" / "Microsoft" / "Windows Terminal" / "Fragments" / "Hex CLI" / "hexcli.json"


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


def _launcher():
    try:
        from . import launcher
        return launcher
    except Exception:
        return None


def _parse_version(tag: str) -> tuple[int, ...]:
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", tag or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def _git_pull(install_dir: Path) -> bool:
    """Return True if git pull succeeds, False on any failure."""
    git = shutil.which("git")
    if not git:
        _print("git not found; source update skipped.")
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
        _print("git pull timed out; source update skipped.")
        return False
    if result.returncode == 0:
        _print(result.stdout.strip() or "Already up to date.")
        return True
    _print(f"git pull failed: {result.stderr.strip()}")
    return False


# ---------------------------------------------------------------------------
# Public entry points
# ---------------------------------------------------------------------------

def update(install_dir: Path | None = None) -> int:
    """Pull the latest source and refresh the npurun binary.

    Returns an exit code (0 = success, 1 = partial failure, 2 = hard failure).
    """
    from . import paths
    print("\n  Hex CLI update\n")

    # 1. Update the Python source: git in a checkout, pip for an installed package.
    install_dir = install_dir or paths.CHECKOUT_DIR
    if install_dir is not None and (install_dir / ".git").exists():
        _print("Pulling source...")
        _git_pull(install_dir)
    else:
        _print("Installed as a package; update the code with:\n"
               "    pip install --upgrade git+https://github.com/NathanL15/Hex-CLI")

    # 2. Fetch the fork's latest release metadata from GitHub.
    _print("Checking the latest npurun release...")
    try:
        release = _fetch_latest_release()
    except Exception as exc:
        _print(f"GitHub API error: {exc}")
        _print("Source updated. npurun not checked: no network.")
        return 1

    tag = release.get("tag_name", "unknown")
    _print(f"Latest npurun: {tag}")

    # Nothing to do when the binary the launcher will run is already there.
    ln = _launcher()
    if ln is not None:
        have = ln.find_npurun_exe()
        have_version = ln._npurun_version(have) if have else ()
        want = _parse_version(tag)
        if have_version and want and tuple(have_version) >= want:
            _print(f"npurun {ln.version_str(have_version)} at {have} is current.")
            return 0

    # 3. Download the npurun binary if a matching asset exists.
    url = _find_asset_url(release, _NPURUN_ASSET)
    if not url:
        _print(f"{tag} has no {_NPURUN_ASSET} asset; binary update skipped.")
        return 0

    bin_dir = paths.npurun_bin_dir()
    bin_dir.mkdir(parents=True, exist_ok=True)
    existing = bin_dir / _NPURUN_ASSET
    dest_tmp = bin_dir / f"{_NPURUN_ASSET}.tmp"
    _print(f"Downloading {_NPURUN_ASSET}...")
    try:
        _download(url, dest_tmp)
        dest_tmp.replace(existing)
    except Exception as exc:
        dest_tmp.unlink(missing_ok=True)
        _print(f"Download failed: {exc}")
        return 1

    _print(f"npurun updated: {existing}")
    _print("Update complete.")
    return 0


def uninstall(install_dir: Path | None = None) -> int:
    """Remove the Start Menu shortcut and optionally purge user data."""
    from . import paths
    print("\n  Hex CLI uninstall\n")

    # 1. Remove Start Menu shortcut.
    shortcut = _START_MENU / _SHORTCUT_NAME
    if shortcut.exists():
        try:
            shortcut.unlink()
            _print(f"Removed shortcut: {shortcut}")
        except OSError as exc:
            _print(f"Could not remove shortcut: {exc}")
    else:
        _print("Start Menu shortcut not found.")
    if _WT_FRAGMENT.exists():
        try:
            _WT_FRAGMENT.unlink()
            try:
                _WT_FRAGMENT.parent.rmdir()   # the fragment folder, if nothing else is in it
            except OSError:
                pass
            _print("Removed Windows Terminal profile: Hex CLI")
        except OSError as exc:
            _print(f"Could not remove the Windows Terminal profile: {exc}")

    # 2. Ask whether to purge per-user data (~/.shellai: sessions, memory,
    #    chat logs, the runtime config, the embedding model).
    shellai_dir = paths.data_dir(create=False)
    if shellai_dir.exists():
        try:
            answer = input(
                f"\n  Remove {shellai_dir} with its sessions and memory? [y/N] "
            ).strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = "n"
        if answer in ("y", "yes"):
            try:
                shutil.rmtree(shellai_dir)
                _print(f"Removed: {shellai_dir}")
            except OSError as exc:
                _print(f"Could not remove {shellai_dir}: {exc}")
        else:
            _print(f"{shellai_dir} kept.")

    # 3. The code itself.
    checkout = install_dir or paths.CHECKOUT_DIR
    _print("\nTo complete uninstall:  pip uninstall hexcli")
    if checkout is not None:
        _print(f"and delete the checkout:  Remove-Item -Recurse \"{checkout}\"")

    return 0


def first_run_check(install_dir: Path | None = None) -> None:
    """Print first-run setup hints when critical dependencies are missing.

    Runs once per process on every `hexcli` invocation, but prints nothing
    when everything looks healthy — zero noise for existing installs.
    """
    from . import paths
    hints: list[str] = []

    # npurun: the launcher's own search (source build, downloaded binary, PATH).
    ln = _launcher()
    found = ln.find_npurun_exe() if ln is not None else None
    if found is None and not (shutil.which("npurun") or shutil.which("npurun.exe")):
        hints.append("  npurun not found. Run:  hexcli --update")

    # ONNX embedding model for memory.
    if not paths.embedding_model_path().exists():
        hints.append("  Embedding model missing; memory is off. hexcli --doctor prints the download commands.")

    if hints:
        print("\n  First-run setup", flush=True)
        for h in hints:
            print(h, flush=True)
        print(flush=True)
