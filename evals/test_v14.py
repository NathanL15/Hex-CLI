#!/usr/bin/env python3
"""evals/test_v14.py — Unit tests for v1.4 features.

Tests: NPU process lock, /config helpers, /memory helpers,
delegate sub-agent (no-recursion guard, session-ID restore).
All offline — no LLM endpoint required.

Usage:
    python evals/test_v14.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest.mock
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hexcli.agent as sa
import hexcli.lockfile as lf

# ---------------------------------------------------------------------------
# Process lock — hexcli.lockfile
# ---------------------------------------------------------------------------

def test_lock_acquire_writes_pid() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lf._LOCK_PATH = None
        lf.acquire(Path(tmp))
        lock = Path(tmp) / "shellai.lock"
        assert lock.exists(), "lock file must be created"
        assert int(lock.read_text().strip()) == os.getpid()
        lf._release()
        lf._LOCK_PATH = None


def test_lock_acquire_clean_returns_none() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lf._LOCK_PATH = None
        warning = lf.acquire(Path(tmp))
        assert warning is None, "clean acquire must return None"
        lf._release()
        lf._LOCK_PATH = None


def test_lock_acquire_stale_pid_no_warning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Write a PID that no process is using (a very large number)
        lock = Path(tmp) / "shellai.lock"
        lock.write_text("99999999", encoding="utf-8")
        lf._LOCK_PATH = None
        with unittest.mock.patch("hexcli.lockfile._pid_alive", return_value=False):
            warning = lf.acquire(Path(tmp))
        assert warning is None, "stale (dead) PID must not produce a warning"
        lf._release()
        lf._LOCK_PATH = None


def test_lock_acquire_live_pid_returns_warning() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        # Write a fake PID that appears alive
        lock = Path(tmp) / "shellai.lock"
        lock.write_text("12345", encoding="utf-8")
        lf._LOCK_PATH = None
        with unittest.mock.patch("hexcli.lockfile._pid_alive", return_value=True):
            warning = lf.acquire(Path(tmp))
        assert warning is not None, "live foreign PID must produce a warning"
        assert "12345" in warning
        lf._release()
        lf._LOCK_PATH = None


def test_lock_release_removes_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lf._LOCK_PATH = None
        lf.acquire(Path(tmp))
        lock = Path(tmp) / "shellai.lock"
        assert lock.exists()
        lf._release()
        assert not lock.exists(), "lock file must be removed after release"
        lf._LOCK_PATH = None


def test_lock_release_does_not_remove_foreign_lock() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        lock = Path(tmp) / "shellai.lock"
        lock.write_text("9999", encoding="utf-8")  # different PID
        lf._LOCK_PATH = lock
        lf._release()
        # The file should still exist — we didn't own it
        assert lock.exists(), "release must not delete a lock owned by another PID"
        lf._LOCK_PATH = None


def test_lock_acquire_survives_readonly_parent() -> None:
    # Even if writing fails, acquire must not raise
    with tempfile.TemporaryDirectory() as tmp:
        lf._LOCK_PATH = None
        with unittest.mock.patch("pathlib.Path.write_text", side_effect=PermissionError("read-only")):
            try:
                lf.acquire(Path(tmp))
            except Exception:
                assert False, "acquire must not raise on PermissionError"
        lf._LOCK_PATH = None


# ---------------------------------------------------------------------------
# /config helpers — _handle_config_cmd, _coerce_config_value
# ---------------------------------------------------------------------------

def test_coerce_bool_true() -> None:
    for v in ("true", "True", "1", "yes", "on"):
        assert sa._coerce_config_value(v, "bool") is True, f"expected True for {v!r}"


def test_coerce_bool_false() -> None:
    for v in ("false", "False", "0", "no", "off"):
        assert sa._coerce_config_value(v, "bool") is False, f"expected False for {v!r}"


def test_coerce_int() -> None:
    assert sa._coerce_config_value("42", "int") == 42
    assert sa._coerce_config_value("0", "int") == 0


def test_coerce_float() -> None:
    result = sa._coerce_config_value("0.3", "float")
    assert abs(result - 0.3) < 1e-9


def test_coerce_str_passthrough() -> None:
    assert sa._coerce_config_value("qwen3:4b", "str") == "qwen3:4b"


def test_handle_config_set_bool() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    sa._handle_config_cmd("/config memory_enabled false", cfg)
    assert cfg["memory_enabled"] is False


def test_handle_config_set_int() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    sa._handle_config_cmd("/config max_agent_steps 20", cfg)
    assert cfg["max_agent_steps"] == 20


def test_handle_config_set_float() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    sa._handle_config_cmd("/config temperature 0.7", cfg)
    assert abs(cfg["temperature"] - 0.7) < 1e-9


def test_handle_config_set_str() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    sa._handle_config_cmd("/config model llama3:8b", cfg)
    assert cfg["model"] == "llama3:8b"


def test_handle_config_invalid_key_no_raise() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    try:
        sa._handle_config_cmd("/config nonexistent_key 123", cfg)
    except Exception:
        assert False, "_handle_config_cmd must not raise on unknown key"


def test_all_config_settable_keys_in_default() -> None:
    for key in sa._CONFIG_SETTABLE:
        if key == "model":
            continue  # model's default is present under "model"
        assert key in sa.DEFAULT_CONFIG, f"settable key {key!r} must exist in DEFAULT_CONFIG"


# ---------------------------------------------------------------------------
# /memory helpers — _handle_memory_cmd
# ---------------------------------------------------------------------------

def test_memory_status_disabled() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    cfg["memory_enabled"] = False
    try:
        sa._handle_memory_cmd("/memory status", cfg)
    except Exception as exc:
        assert False, f"_handle_memory_cmd raised: {exc}"


def test_memory_status_empty_store() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            sa._handle_memory_cmd("/memory status", cfg)
        except Exception as exc:
            assert False, f"raised: {exc}"
        finally:
            os.chdir(orig)


def test_memory_list_with_entries() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            store_dir = Path(tmp) / ".shellai" / "vector_store"
            store_dir.mkdir(parents=True)
            entries = [
                {"created_at": "2026-06-01T10:00:00+00:00", "text": "First task", "tool_sequence": ["read_file"]},
                {"created_at": "2026-06-02T11:00:00+00:00", "text": "Second task", "tool_sequence": ["edit_file"]},
            ]
            (store_dir / "metadata.json").write_text(json.dumps(entries), encoding="utf-8")
            sa._handle_memory_cmd("/memory list 5", cfg)
        except Exception as exc:
            assert False, f"raised: {exc}"
        finally:
            os.chdir(orig)


def test_memory_clear_aborted() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        store_dir = Path(tmp) / ".shellai" / "vector_store"
        store_dir.mkdir(parents=True)
        meta = store_dir / "metadata.json"
        meta.write_text("[]", encoding="utf-8")
        try:
            with unittest.mock.patch("builtins.input", return_value="n"):
                sa._handle_memory_cmd("/memory clear", cfg)
            assert meta.exists(), "aborted clear must not delete the file"
        finally:
            os.chdir(orig)


def test_memory_clear_confirmed() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        store_dir = Path(tmp) / ".shellai" / "vector_store"
        store_dir.mkdir(parents=True)
        meta = store_dir / "metadata.json"
        meta.write_text("[]", encoding="utf-8")
        try:
            with unittest.mock.patch("builtins.input", return_value="y"):
                sa._handle_memory_cmd("/memory clear", cfg)
            assert not meta.exists(), "confirmed clear must delete metadata.json"
        finally:
            os.chdir(orig)


def test_memory_unknown_subcommand_no_raise() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    try:
        sa._handle_memory_cmd("/memory bogus", cfg)
    except Exception as exc:
        assert False, f"raised: {exc}"


# ---------------------------------------------------------------------------
# delegate tool — _run_delegate + _in_delegate flag
# ---------------------------------------------------------------------------

def test_delegate_no_recursion_guard() -> None:
    sa._in_delegate = True
    cfg = dict(sa.DEFAULT_CONFIG)
    try:
        sa._run_delegate(cfg, "list the directory", "powershell.exe")
        assert False, "should have raised RuntimeError"
    except RuntimeError as exc:
        assert "no recursion" in str(exc).lower()
    finally:
        sa._in_delegate = False


def test_delegate_restores_session_id_on_success() -> None:
    original_sid = "parent-session-abc"
    sa._CURRENT_SESSION_ID = original_sid
    sa._in_delegate = False
    cfg = dict(sa.DEFAULT_CONFIG)
    cfg["max_agent_steps"] = 1

    def fake_autopilot(config, history, query, shell_exe, session=None, turn=None):
        return "delegate done"

    with unittest.mock.patch("hexcli.agent.run_autopilot", side_effect=fake_autopilot):
        result = sa._run_delegate(cfg, "test task", "powershell.exe")

    assert sa._CURRENT_SESSION_ID == original_sid, "session ID must be restored after delegate"
    assert sa._in_delegate is False, "_in_delegate must be False after delegate completes"
    assert result == "delegate done"


def test_delegate_restores_session_id_on_failure() -> None:
    original_sid = "parent-session-xyz"
    sa._CURRENT_SESSION_ID = original_sid
    sa._in_delegate = False
    cfg = dict(sa.DEFAULT_CONFIG)

    def fake_autopilot(*args, **kwargs):
        raise RuntimeError("simulated backend error")

    with unittest.mock.patch("hexcli.agent.run_autopilot", side_effect=fake_autopilot):
        try:
            sa._run_delegate(cfg, "test task", "powershell.exe")
        except RuntimeError:
            pass

    assert sa._CURRENT_SESSION_ID == original_sid, "session ID must be restored even after delegate failure"
    assert sa._in_delegate is False, "_in_delegate must be False after delegate failure"


def test_delegate_truncates_long_output() -> None:
    sa._in_delegate = False
    cfg = dict(sa.DEFAULT_CONFIG)
    long_output = "x" * 3000

    def fake_autopilot(*args, **kwargs):
        return long_output

    with unittest.mock.patch("hexcli.agent.run_autopilot", side_effect=fake_autopilot):
        result = sa._run_delegate(cfg, "task", "powershell.exe")

    assert len(result) <= 1500 + 60, "delegate output must be capped near 1500 chars"
    assert "truncated" in result


def test_delegate_in_delegate_flag_cleared_after_success() -> None:
    sa._in_delegate = False
    cfg = dict(sa.DEFAULT_CONFIG)

    def fake_autopilot(*args, **kwargs):
        assert sa._in_delegate is True, "_in_delegate must be True during delegate run"
        return "ok"

    with unittest.mock.patch("hexcli.agent.run_autopilot", side_effect=fake_autopilot):
        sa._run_delegate(cfg, "task", "powershell.exe")

    assert sa._in_delegate is False


def test_delegate_schema_absent_in_delegate_prompt() -> None:
    sa._in_delegate = True
    try:
        prompt = sa.build_autopilot_prompt(".", 5, query="list the files")
        assert "delegate" not in prompt, "delegate schema must not appear in delegate prompts"
    finally:
        sa._in_delegate = False


def test_delegate_schema_present_in_outer_prompt() -> None:
    sa._in_delegate = False
    prompt = sa.build_autopilot_prompt(".", 15, query="list the files")
    assert "delegate" in prompt, "delegate schema must appear in outer-loop prompts"


def test_delegate_in_tool_names() -> None:
    assert "delegate" in sa.TOOL_NAMES


# ---------------------------------------------------------------------------
# Runner — shared utility
# ---------------------------------------------------------------------------

def capsys_compat(fn: Any) -> Any:
    return fn  # no-op: pytest fixture not used here


def _run(fn: Any) -> bool:
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
    test_lock_acquire_writes_pid,
    test_lock_acquire_clean_returns_none,
    test_lock_acquire_stale_pid_no_warning,
    test_lock_acquire_live_pid_returns_warning,
    test_lock_release_removes_file,
    test_lock_release_does_not_remove_foreign_lock,
    test_lock_acquire_survives_readonly_parent,
    test_coerce_bool_true,
    test_coerce_bool_false,
    test_coerce_int,
    test_coerce_float,
    test_coerce_str_passthrough,
    test_handle_config_set_bool,
    test_handle_config_set_int,
    test_handle_config_set_float,
    test_handle_config_set_str,
    test_handle_config_invalid_key_no_raise,
    test_all_config_settable_keys_in_default,
    test_memory_status_disabled,
    test_memory_status_empty_store,
    test_memory_list_with_entries,
    test_memory_clear_aborted,
    test_memory_clear_confirmed,
    test_memory_unknown_subcommand_no_raise,
    test_delegate_no_recursion_guard,
    test_delegate_restores_session_id_on_success,
    test_delegate_restores_session_id_on_failure,
    test_delegate_truncates_long_output,
    test_delegate_in_delegate_flag_cleared_after_success,
    test_delegate_schema_absent_in_delegate_prompt,
    test_delegate_schema_present_in_outer_prompt,
    test_delegate_in_tool_names,
]


def main() -> int:
    print(f"\nevals/test_v14.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
