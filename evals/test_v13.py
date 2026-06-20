#!/usr/bin/env python3
"""evals/test_v13.py — Unit tests for v1.3 features.

Tests: dynamic tool injection, workspace snapshot, URL security,
fetch_url offline behaviour, batch partial failure, history tool extraction.
All offline — no LLM endpoint required.

Usage:
    python evals/test_v13.py
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
import hexcli.network as net


# ---------------------------------------------------------------------------
# Dynamic tool injection — build_autopilot_prompt
# ---------------------------------------------------------------------------

def test_search_memory_injected_for_past_reference() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="what did we do earlier?")
    assert "search_memory" in prompt, "search_memory must be injected for 'earlier'"


def test_search_memory_not_injected_for_generic_query() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="list all Python files")
    assert "search_memory" not in prompt, "search_memory must not appear for generic queries"


def test_search_memory_injected_for_last_time() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="fix the bug from last time")
    assert "search_memory" in prompt


def test_search_memory_injected_for_previously() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="previously you said the test was failing")
    assert "search_memory" in prompt


def test_lint_not_injected_for_generic_query() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="show me the directory tree")
    assert "lint_code" not in prompt, "lint_code must not appear for generic queries"


def test_lint_injected_for_lint_query_when_ruff_present() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="lint the Python file")
    if sa._RUFF:
        assert "lint_code" in prompt
    else:
        assert "lint_code" not in prompt


def test_lint_injected_when_recent_tool_edit() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="fix the import", recent_tools=["edit_file"])
    if sa._RUFF:
        assert "lint_code" in prompt, "lint_code must appear after recent edit_file"


def test_fetch_url_not_injected_for_generic_query() -> None:
    # Mock offline so fetch_url can't fire from online check either.
    net._online_result = False
    net._online_ts = float("inf")
    try:
        prompt = sa.build_autopilot_prompt(".", 15, query="read the config file")
        assert "fetch_url" not in prompt
    finally:
        net._online_result = None
        net._online_ts = 0.0


def test_fetch_url_not_injected_when_offline() -> None:
    net._online_result = False
    net._online_ts = float("inf")
    try:
        prompt = sa.build_autopilot_prompt(".", 15, query="look up the latest docs")
        assert "fetch_url" not in prompt, "fetch_url must not appear when offline"
    finally:
        net._online_result = None
        net._online_ts = 0.0


def test_fetch_url_injected_when_online_and_url_in_query() -> None:
    net._online_result = True
    net._online_ts = float("inf")
    try:
        prompt = sa.build_autopilot_prompt(".", 15, query="fetch https://docs.python.org/3/")
        assert "fetch_url" in prompt, "fetch_url must appear when online and URL in query"
    finally:
        net._online_result = None
        net._online_ts = 0.0


def test_fetch_url_injected_when_online_and_docs_keyword() -> None:
    net._online_result = True
    net._online_ts = float("inf")
    try:
        prompt = sa.build_autopilot_prompt(".", 15, query="look up the documentation for requests")
        assert "fetch_url" in prompt
    finally:
        net._online_result = None
        net._online_ts = 0.0


def test_batch_injected_for_multiple_files_keyword() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="read all files in the src directory")
    assert "batch" in prompt, "batch must be injected for 'all files' keyword"


def test_batch_injected_for_multiple_py_extensions() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="compare agent.py and memory.py")
    assert "batch" in prompt, "batch must be injected when query mentions 2+ .py files"


def test_batch_not_injected_for_single_file_query() -> None:
    prompt = sa.build_autopilot_prompt(".", 15, query="read agent.py and show me the imports")
    # Only one .py mention — batch should not fire
    assert "batch" not in prompt


# ---------------------------------------------------------------------------
# Token budget sanity check
# ---------------------------------------------------------------------------

def test_base_prompt_smaller_than_full_prompt() -> None:
    # Base prompt (offline, generic query) must be smaller than the full prompt
    # (online, query with memory/lint/batch triggers) — verifying conditional injection works.
    net._online_result = False
    net._online_ts = float("inf")
    try:
        base = sa.build_autopilot_prompt(".", 15, query="list the directory")
    finally:
        net._online_result = None
        net._online_ts = 0.0

    net._online_result = True
    net._online_ts = float("inf")
    try:
        full = sa.build_autopilot_prompt(
            ".", 15,
            query="earlier I read multiple files — look up the docs and lint my code",
            recent_tools=["edit_file"],
        )
    finally:
        net._online_result = None
        net._online_ts = 0.0

    assert len(full) > len(base), "conditional injection must expand the prompt"
    # Sanity: base prompt must not be absurdly large (12 KB is a hard ceiling).
    assert len(base) < 12_000, f"base prompt is {len(base)} chars — unreasonably large"


# ---------------------------------------------------------------------------
# Workspace snapshot
# ---------------------------------------------------------------------------

def test_workspace_snapshot_contains_workspace_tag() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap = sa.workspace_snapshot(tmp)
        first_line = snap.split("\n")[0]
        assert first_line.startswith("[workspace:"), f"unexpected snapshot: {snap}"
        assert first_line.endswith("]")


def test_workspace_snapshot_detects_python() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "requirements.txt").write_text("requests\n")
        snap = sa.workspace_snapshot(tmp)
        assert "workspace:python" in snap


def test_workspace_snapshot_detects_node() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "package.json").write_text('{"name":"test"}')
        snap = sa.workspace_snapshot(tmp)
        assert "workspace:node" in snap


def test_workspace_snapshot_detects_rust() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "Cargo.toml").write_text('[package]\nname = "test"')
        snap = sa.workspace_snapshot(tmp)
        assert "workspace:rust" in snap


def test_workspace_snapshot_detects_entry_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "main.py").write_text("# entry\n")
        snap = sa.workspace_snapshot(tmp)
        assert "entry:main.py" in snap


def test_workspace_snapshot_detects_test_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "tests").mkdir()
        snap = sa.workspace_snapshot(tmp)
        assert "tests:tests/" in snap


def test_workspace_snapshot_bare_dir() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        snap = sa.workspace_snapshot(tmp)
        assert "workspace:dir" in snap


def test_workspace_snapshot_never_raises() -> None:
    # Even on a path with no git / no marker files, must not raise.
    result = sa.workspace_snapshot("C:\\")
    assert isinstance(result, str) and len(result) > 0


# ---------------------------------------------------------------------------
# URL security (network.py)
# ---------------------------------------------------------------------------

def test_block_file_scheme() -> None:
    result = net._is_blocked_url("file:///etc/passwd")
    assert result is not None and "http" in result.lower()


def test_block_localhost() -> None:
    result = net._is_blocked_url("http://localhost/admin")
    assert result is not None and "private" in result.lower()


def test_block_loopback_ip() -> None:
    result = net._is_blocked_url("http://127.0.0.1/secret")
    assert result is not None


def test_block_private_192() -> None:
    result = net._is_blocked_url("http://192.168.1.1/router")
    assert result is not None


def test_block_private_10() -> None:
    result = net._is_blocked_url("http://10.0.0.1/internal")
    assert result is not None


def test_allow_public_https() -> None:
    result = net._is_blocked_url("https://docs.python.org/3/")
    assert result is None, f"public URL should not be blocked, got: {result}"


def test_allow_public_http() -> None:
    result = net._is_blocked_url("http://example.com/page")
    assert result is None


# ---------------------------------------------------------------------------
# fetch_url offline behaviour
# ---------------------------------------------------------------------------

def test_fetch_url_returns_error_when_offline() -> None:
    net._online_result = False
    net._online_ts = float("inf")
    try:
        result = net.fetch_url("https://example.com")
        assert "no network" in result.lower() or "offline" in result.lower() or "connection" in result.lower()
    finally:
        net._online_result = None
        net._online_ts = 0.0


def test_fetch_url_blocks_private_ip_offline() -> None:
    # Block check happens before the online check — always fires.
    net._online_result = False
    net._online_ts = float("inf")
    try:
        result = net.fetch_url("http://127.0.0.1/secret")
        assert "blocked" in result.lower() or "private" in result.lower()
    finally:
        net._online_result = None
        net._online_ts = 0.0


# ---------------------------------------------------------------------------
# _extract_tools_from_history
# ---------------------------------------------------------------------------

def test_extract_tools_from_history_finds_edit_file() -> None:
    history = [
        {"role": "user", "content": "fix the bug"},
        {"role": "assistant", "content": '{"action":"edit_file","args":{"path":"a.py","old_string":"x","new_string":"y"}}'},
        {"role": "user", "content": "Tool output:\nEdited a.py"},
        {"role": "assistant", "content": '{"action":"finish","message":"Done."}'},
    ]
    tools = sa._extract_tools_from_history(history)
    assert "edit_file" in tools


def test_extract_tools_from_empty_history() -> None:
    tools = sa._extract_tools_from_history([])
    assert tools == []


def test_extract_tools_respects_last_n() -> None:
    history = [
        {"role": "assistant", "content": '{"action":"run_command","args":{"command":"git status"}}'},
        {"role": "assistant", "content": '{"action":"edit_file","args":{}}'},
    ]
    # With last_n=1, only the second message is considered
    tools = sa._extract_tools_from_history(history, last_n=1)
    assert "edit_file" in tools
    assert "run_command" not in tools


# ---------------------------------------------------------------------------
# Batch tool — partial failure
# ---------------------------------------------------------------------------

def test_batch_partial_failure_all_results_returned() -> None:
    # Use a real (offline-safe) config and a temp directory.
    with tempfile.TemporaryDirectory() as tmp:
        Path(tmp, "exists.txt").write_text("hello")
        cfg = dict(sa.DEFAULT_CONFIG)
        cfg["tool_output_limit"] = 4000
        orig = os.getcwd()
        os.chdir(tmp)
        try:
            actions = [
                {"tool": "read_file", "args": {"path": "exists.txt"}},
                {"tool": "read_file", "args": {"path": "does_not_exist.txt"}},
                {"tool": "list_directory", "args": {"path": "."}},
            ]
            result = sa._run_batch(cfg, actions, sa.detect_shell(""))
            assert "[0]" in result, "first result must be present"
            assert "[1]" in result, "second result must be present (even if error)"
            assert "[2]" in result, "third result must be present"
            assert "ERROR" in result, "the missing file must produce an ERROR entry"
            assert "hello" in result, "the successful read must appear in results"
        finally:
            os.chdir(orig)


def test_batch_rejects_mutation_tools() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    actions = [{"tool": "edit_file", "args": {"path": "x.py", "old_string": "a", "new_string": "b"}}]
    result = sa._run_batch(cfg, actions, sa.detect_shell(""))
    assert "ERROR" in result and "not allowed" in result


def test_batch_rejects_too_many_actions() -> None:
    cfg = dict(sa.DEFAULT_CONFIG)
    actions = [{"tool": "list_directory", "args": {"path": "."}}] * (sa._BATCH_MAX + 1)
    try:
        sa._run_batch(cfg, actions, sa.detect_shell(""))
        assert False, "should have raised RuntimeError"
    except RuntimeError as exc:
        assert "max" in str(exc).lower()


# ---------------------------------------------------------------------------
# is_online cache behaviour
# ---------------------------------------------------------------------------

def test_is_online_caches_result() -> None:
    net._online_result = True
    net._online_ts = float("inf")
    try:
        # Should return cached True without probing
        result = net.is_online()
        assert result is True
    finally:
        net._online_result = None
        net._online_ts = 0.0


def test_is_online_returns_false_when_probe_fails() -> None:
    net._online_result = None
    net._online_ts = 0.0
    with unittest.mock.patch("hexcli.network.socket.create_connection", side_effect=OSError("no route")):
        result = net.is_online()
    assert result is False
    net._online_result = None
    net._online_ts = 0.0


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
        print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False


TESTS = [
    test_search_memory_injected_for_past_reference,
    test_search_memory_not_injected_for_generic_query,
    test_search_memory_injected_for_last_time,
    test_search_memory_injected_for_previously,
    test_lint_not_injected_for_generic_query,
    test_lint_injected_for_lint_query_when_ruff_present,
    test_lint_injected_when_recent_tool_edit,
    test_fetch_url_not_injected_for_generic_query,
    test_fetch_url_not_injected_when_offline,
    test_fetch_url_injected_when_online_and_url_in_query,
    test_fetch_url_injected_when_online_and_docs_keyword,
    test_batch_injected_for_multiple_files_keyword,
    test_batch_injected_for_multiple_py_extensions,
    test_batch_not_injected_for_single_file_query,
    test_base_prompt_smaller_than_full_prompt,
    test_workspace_snapshot_contains_workspace_tag,
    test_workspace_snapshot_detects_python,
    test_workspace_snapshot_detects_node,
    test_workspace_snapshot_detects_rust,
    test_workspace_snapshot_detects_entry_file,
    test_workspace_snapshot_detects_test_dir,
    test_workspace_snapshot_bare_dir,
    test_workspace_snapshot_never_raises,
    test_block_file_scheme,
    test_block_localhost,
    test_block_loopback_ip,
    test_block_private_192,
    test_block_private_10,
    test_allow_public_https,
    test_allow_public_http,
    test_fetch_url_returns_error_when_offline,
    test_fetch_url_blocks_private_ip_offline,
    test_extract_tools_from_history_finds_edit_file,
    test_extract_tools_from_empty_history,
    test_extract_tools_respects_last_n,
    test_batch_partial_failure_all_results_returned,
    test_batch_rejects_mutation_tools,
    test_batch_rejects_too_many_actions,
    test_is_online_caches_result,
    test_is_online_returns_false_when_probe_fails,
]


def main() -> int:
    print(f"\nevals/test_v13.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
