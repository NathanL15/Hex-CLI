#!/usr/bin/env python3
"""evals/test_v12.py — Unit tests for v1.2 features.

Tests: safety classifier, audit log, error-loop detection logic.
All offline — no LLM endpoint required.

Usage:
    python evals/test_v12.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hexcli.safety as sf

# ---------------------------------------------------------------------------
# Safety classifier — destructive
# ---------------------------------------------------------------------------

def test_classify_remove_item_destructive() -> None:
    assert sf.classify_command("Remove-Item -Path ./dist -Recurse") == "destructive"


def test_classify_rm_destructive() -> None:
    assert sf.classify_command("rm -rf ./build") == "destructive"


def test_classify_del_destructive() -> None:
    assert sf.classify_command("del /Q /F temp.txt") == "destructive"


def test_classify_force_recurse_destructive() -> None:
    assert sf.classify_command("Remove-Item C:\\Temp -Force -Recurse") == "destructive"


def test_classify_recurse_force_order_destructive() -> None:
    assert sf.classify_command("Remove-Item C:\\Temp -Recurse -Force") == "destructive"


def test_classify_git_reset_hard_destructive() -> None:
    assert sf.classify_command("git reset --hard HEAD~1") == "destructive"


def test_classify_git_push_force_destructive() -> None:
    assert sf.classify_command("git push --force origin main") == "destructive"


def test_classify_git_push_f_destructive() -> None:
    assert sf.classify_command("git push -f") == "destructive"


def test_classify_format_volume_destructive() -> None:
    assert sf.classify_command("Format-Volume -DriveLetter D -FileSystem NTFS") == "destructive"


def test_classify_reg_delete_destructive() -> None:
    assert sf.classify_command("reg delete HKCU\\Software\\Test /f") == "destructive"


# ---------------------------------------------------------------------------
# Safety classifier — safe
# ---------------------------------------------------------------------------

def test_classify_get_process_safe() -> None:
    assert sf.classify_command("Get-Process | Sort-Object CPU -Descending") == "safe"


def test_classify_git_status_safe() -> None:
    assert sf.classify_command("git status") == "safe"


def test_classify_git_log_safe() -> None:
    assert sf.classify_command("git log --oneline -10") == "safe"


def test_classify_pip_list_safe() -> None:
    assert sf.classify_command("pip list") == "safe"


def test_classify_python_version_safe() -> None:
    assert sf.classify_command("python --version") == "safe"


def test_classify_ls_safe() -> None:
    assert sf.classify_command("ls .") == "safe"


# ---------------------------------------------------------------------------
# Safety classifier — caution (neither safe nor destructive)
# ---------------------------------------------------------------------------

def test_classify_pip_install_caution() -> None:
    assert sf.classify_command("pip install requests") == "caution"


def test_classify_git_commit_caution() -> None:
    assert sf.classify_command("git commit -m 'fix: update config'") == "caution"


def test_classify_new_item_caution() -> None:
    assert sf.classify_command("New-Item -ItemType Directory -Path ./output") == "caution"


def test_classify_git_push_no_force_caution() -> None:
    assert sf.classify_command("git push origin main") == "caution"


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

def test_audit_log_creates_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig = Path.cwd()
        try:
            import os
            os.chdir(tmp)
            sf.append_audit_log("sess-1", "safe", "git status", 0)
            log = Path(tmp) / ".shellai" / "audit.log"
            assert log.exists(), "audit.log must be created"
        finally:
            os.chdir(orig)


def test_audit_log_fields() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        import os
        orig = Path.cwd()
        try:
            os.chdir(tmp)
            sf.append_audit_log("sess-abc", "destructive", "rm -rf ./dist", "blocked")
            log = Path(tmp) / ".shellai" / "audit.log"
            entry = json.loads(log.read_text(encoding="utf-8").strip())
            assert entry["session"] == "sess-abc"
            assert entry["classification"] == "destructive"
            assert entry["cmd"] == "rm -rf ./dist"
            assert entry["exit_code"] == "blocked"
            assert "ts" in entry
        finally:
            os.chdir(orig)


def test_audit_log_appends() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        import os
        orig = Path.cwd()
        try:
            os.chdir(tmp)
            sf.append_audit_log(None, "safe", "git status", 0)
            sf.append_audit_log(None, "caution", "git commit -m x", 0)
            log = Path(tmp) / ".shellai" / "audit.log"
            lines = [ln for ln in log.read_text(encoding="utf-8").splitlines() if ln.strip()]
            assert len(lines) == 2, "both entries must be present"
            cmds = [json.loads(ln)["cmd"] for ln in lines]
            assert "git status" in cmds and "git commit -m x" in cmds
        finally:
            os.chdir(orig)


def test_audit_log_survives_bad_cwd() -> None:
    # Even if the path is weird, append_audit_log must never raise.
    sf.append_audit_log(None, "safe", "ls", None)


# ---------------------------------------------------------------------------
# Error-loop detection logic (pure unit test — no LLM)
# ---------------------------------------------------------------------------

def test_error_loop_detects_three_identical() -> None:
    tracker: list[tuple[str, str]] = []
    output = "Error: File not found"
    tool = "read_file"
    found_loop = False
    for _ in range(3):
        tracker.append((tool, output))
        if len(tracker) > 3:
            tracker.pop(0)
        if len(tracker) == 3 and len(set(tracker)) == 1:
            found_loop = True
    assert found_loop, "loop must be detected after 3 identical results"


def test_error_loop_no_false_positive_different_outputs() -> None:
    tracker: list[tuple[str, str]] = []
    outputs = ["Error: not found", "Exit code: 1\nPermission denied", "Error: not found"]
    for i, out in enumerate(outputs):
        tracker.append(("run_command", out))
        if len(tracker) > 3:
            tracker.pop(0)
    assert len(set(tracker)) != 1, "three different outputs must not trigger loop detection"


def test_error_loop_no_false_positive_different_tools() -> None:
    tracker: list[tuple[str, str]] = []
    same_out = "same output"
    for tool in ["read_file", "list_directory", "search_files"]:
        tracker.append((tool, same_out))
        if len(tracker) > 3:
            tracker.pop(0)
    assert len(set(tracker)) != 1, "same output from different tools must not trigger loop"


def test_error_loop_window_slides() -> None:
    tracker: list[tuple[str, str]] = []
    # 2 identical then 1 different then 2 more identical = no loop window of 3
    pairs = [("t", "a"), ("t", "a"), ("t", "b"), ("t", "a"), ("t", "a")]
    loop_fired = False
    for pair in pairs:
        tracker.append(pair)
        if len(tracker) > 3:
            tracker.pop(0)
        if len(tracker) == 3 and len(set(tracker)) == 1:
            loop_fired = True
    assert not loop_fired, "loop must not fire when window has varied results"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(fn: Any) -> bool:
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as exc:
        print(f"  FAIL  {fn.__name__}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {fn.__name__}: {exc}")
        return False


TESTS = [
    test_classify_remove_item_destructive,
    test_classify_rm_destructive,
    test_classify_del_destructive,
    test_classify_force_recurse_destructive,
    test_classify_recurse_force_order_destructive,
    test_classify_git_reset_hard_destructive,
    test_classify_git_push_force_destructive,
    test_classify_git_push_f_destructive,
    test_classify_format_volume_destructive,
    test_classify_reg_delete_destructive,
    test_classify_get_process_safe,
    test_classify_git_status_safe,
    test_classify_git_log_safe,
    test_classify_pip_list_safe,
    test_classify_python_version_safe,
    test_classify_ls_safe,
    test_classify_pip_install_caution,
    test_classify_git_commit_caution,
    test_classify_new_item_caution,
    test_classify_git_push_no_force_caution,
    test_audit_log_creates_file,
    test_audit_log_fields,
    test_audit_log_appends,
    test_audit_log_survives_bad_cwd,
    test_error_loop_detects_three_identical,
    test_error_loop_no_false_positive_different_outputs,
    test_error_loop_no_false_positive_different_tools,
    test_error_loop_window_slides,
]


def main() -> int:
    print(f"\nevals/test_v12.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
