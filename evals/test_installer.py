#!/usr/bin/env python3
"""evals/test_installer.py — Offline tests for install.ps1 and launcher discovery.

The installer itself needs a network and a real machine; what CAN be tested
offline is (a) that install.ps1 parses as valid PowerShell, (b) that the
launcher's npurun/QAIRT discovery logic is correct, and (c) that the names
the installer and the launcher share (binary filename, referenced repo
files) cannot drift apart silently.

Usage:
    python evals/test_installer.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import unittest.mock
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
import launcher  # noqa: E402

INSTALL_PS1 = REPO / "install.ps1"


# ---------------------------------------------------------------------------
# install.ps1 static checks
# ---------------------------------------------------------------------------

def test_install_ps1_parses_as_valid_powershell() -> None:
    """A syntax error in install.ps1 must fail CI, not the first user."""
    ps = shutil.which("powershell") or shutil.which("pwsh")
    assert ps, "no PowerShell available to parse install.ps1"
    check = (
        "$errs = $null; "
        f"[System.Management.Automation.Language.Parser]::ParseFile('{INSTALL_PS1}', [ref]$null, [ref]$errs) | Out-Null; "
        "if ($errs -and $errs.Count -gt 0) { $errs | ForEach-Object { $_.Message }; exit 1 } else { exit 0 }"
    )
    r = subprocess.run(
        [ps, "-NoProfile", "-NonInteractive", "-Command", check],
        capture_output=True, text=True, timeout=60,
    )
    assert r.returncode == 0, f"install.ps1 has parse errors:\n{r.stdout}{r.stderr}"


def test_install_ps1_references_only_existing_repo_files() -> None:
    """Files the installer expects next to itself must actually be in the repo."""
    for name in ("Hex CLI.cmd", "shellai.example.json", "hex-cli.ico", "launcher.py"):
        assert (REPO / name).exists(), f"install.ps1 relies on missing repo file: {name}"


def test_installer_and_launcher_agree_on_binary_name() -> None:
    """install.ps1 downloads npurun-arm64.exe; the launcher must look for the
    same filename, or the installed binary is invisible at launch."""
    ps_text = INSTALL_PS1.read_text(encoding="utf-8")
    py_text = (REPO / "launcher.py").read_text(encoding="utf-8")
    assert "npurun-arm64.exe" in ps_text
    assert "npurun-arm64.exe" in py_text


def test_installer_and_launcher_agree_on_qairt_layout() -> None:
    """Both sides must validate the same three QAIRT subdirectories — the
    exact trio whose absence causes the silent DLL/stack-overrun crashes."""
    ps_text = INSTALL_PS1.read_text(encoding="utf-8")
    py_text = (REPO / "launcher.py").read_text(encoding="utf-8")
    for marker in ("aarch64-windows-msvc", "hexagon-v73"):
        assert marker in ps_text, f"install.ps1 no longer checks {marker}"
        assert marker in py_text, f"launcher.py no longer checks {marker}"


# ---------------------------------------------------------------------------
# launcher.find_npurun_exe
# ---------------------------------------------------------------------------

def test_find_npurun_prefers_cargo_build() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        app = Path(tmp) / "app"
        cargo_exe = home / ".cargo" / "bin" / "npurun.exe"
        cargo_exe.parent.mkdir(parents=True)
        cargo_exe.write_bytes(b"x")
        app.mkdir()
        (app / "npurun-arm64.exe").write_bytes(b"x")
        found = launcher.find_npurun_exe(home=home, app_dir=app)
        assert found == cargo_exe, f"cargo build must win, got {found}"


def test_find_npurun_falls_back_to_downloaded_binary() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        app = Path(tmp) / "app"
        home.mkdir()
        app.mkdir()
        downloaded = app / "npurun-arm64.exe"
        downloaded.write_bytes(b"x")
        found = launcher.find_npurun_exe(home=home, app_dir=app)
        assert found == downloaded


def test_find_npurun_returns_none_when_absent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        app = Path(tmp) / "app"
        home.mkdir()
        app.mkdir()
        with unittest.mock.patch("shutil.which", return_value=None):
            assert launcher.find_npurun_exe(home=home, app_dir=app) is None


# ---------------------------------------------------------------------------
# launcher.find_qairt_root
# ---------------------------------------------------------------------------

def _make_qairt(root: Path, valid: bool = True) -> None:
    (root / "lib" / "aarch64-windows-msvc").mkdir(parents=True)
    (root / "bin" / "aarch64-windows-msvc").mkdir(parents=True)
    if valid:
        (root / "lib" / "hexagon-v73" / "unsigned").mkdir(parents=True)


def test_find_qairt_env_override_wins_when_valid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_root = Path(tmp) / "custom_qairt"
        _make_qairt(env_root)
        stack = Path(tmp) / "stack"
        newer = stack / "QAIRT_9.99.0"
        _make_qairt(newer)
        found = launcher.find_qairt_root(env_value=str(env_root), stack_dir=stack)
        assert found == env_root


def test_find_qairt_invalid_env_falls_through_to_stack() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        env_root = Path(tmp) / "broken_qairt"
        _make_qairt(env_root, valid=False)  # missing hexagon skels
        stack = Path(tmp) / "stack"
        good = stack / "QAIRT_2.47.0"
        _make_qairt(good)
        found = launcher.find_qairt_root(env_value=str(env_root), stack_dir=stack)
        assert found == good


def test_find_qairt_picks_newest_valid_install() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stack = Path(tmp) / "stack"
        _make_qairt(stack / "QAIRT_2.40.0")
        _make_qairt(stack / "QAIRT_2.47.0")
        _make_qairt(stack / "QAIRT_2.50.0", valid=False)  # newest but broken
        found = launcher.find_qairt_root(env_value="", stack_dir=stack)
        assert found == stack / "QAIRT_2.47.0", f"got {found}"


def test_find_qairt_none_when_nothing_valid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        stack = Path(tmp) / "stack"
        _make_qairt(stack / "QAIRT_2.47.0", valid=False)
        assert launcher.find_qairt_root(env_value="", stack_dir=stack) is None


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(fn) -> bool:
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as exc:
        print(f"  FAIL  {fn.__name__}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False


TESTS = [
    test_install_ps1_parses_as_valid_powershell,
    test_install_ps1_references_only_existing_repo_files,
    test_installer_and_launcher_agree_on_binary_name,
    test_installer_and_launcher_agree_on_qairt_layout,
    test_find_npurun_prefers_cargo_build,
    test_find_npurun_falls_back_to_downloaded_binary,
    test_find_npurun_returns_none_when_absent,
    test_find_qairt_env_override_wins_when_valid,
    test_find_qairt_invalid_env_falls_through_to_stack,
    test_find_qairt_picks_newest_valid_install,
    test_find_qairt_none_when_nothing_valid,
]


def main() -> int:
    print(f"\nevals/test_installer.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
