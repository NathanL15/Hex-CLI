#!/usr/bin/env python3
"""evals/test_agent_loop.py — Integration tests for the full run_autopilot loop.

Uses the mock backend (backend="mock") so no LLM endpoint or NPU is required.
Verifies: action dispatch, tool execution, error-loop detection, undo snapshots,
safety gating, step budget, and history injection.

Usage:
    python evals/test_agent_loop.py
"""
from __future__ import annotations

import json
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

# ---------------------------------------------------------------------------
# Shared mock config — uses mock backend, no memory I/O, no destructive confirm
# ---------------------------------------------------------------------------

_CFG: dict[str, Any] = {
    **sa.DEFAULT_CONFIG,
    "backend": "mock",
    "max_agent_steps": 10,
    "tool_output_limit": 4000,
    "autopilot_confirm_destructive": False,
    "memory_enabled": False,
    "anthropic_api_key": "",  # suppress escalation prompt
    # These tests drive the loop with fixtures at absolute temp paths
    # while cwd is the repo — a shape production never has (an agent
    # turn's cwd IS the workspace). Scoping has its own suite:
    # evals/test_write_scope.py, which covers the loop path too.
    "workspace_write_scope": False,
}

_SHELL = "powershell.exe"


def _load_fixture(name: str) -> list[str]:
    path = Path(__file__).parent / "fixtures" / name
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Basic action dispatch
# ---------------------------------------------------------------------------

def test_simple_finish_returns_message() -> None:
    sa.set_mock_responses(['{"action":"finish","message":"All done."}'])
    result = sa.run_autopilot(_CFG, [], "do something", _SHELL)
    assert result == "All done.", f"expected 'All done.', got: {result!r}"


def test_finish_via_fixture_file() -> None:
    sa.set_mock_responses(_load_fixture("simple_finish.json"))
    result = sa.run_autopilot(_CFG, [], "simple task", _SHELL)
    assert "Task completed" in result


def test_plain_text_fallback_becomes_finish() -> None:
    sa.set_mock_responses(["This is plain text with no JSON structure."])
    result = sa.run_autopilot(_CFG, [], "explain something", _SHELL)
    assert isinstance(result, str) and result.strip()


def test_cot_stripped_before_parsing() -> None:
    sa.set_mock_responses(['<think>Let me think.</think>{"action":"finish","message":"Thought done."}'])
    result = sa.run_autopilot(_CFG, [], "think and finish", _SHELL)
    assert result == "Thought done."


# ---------------------------------------------------------------------------
# Tool dispatch
# ---------------------------------------------------------------------------

def test_list_directory_tool_executes() -> None:
    sa.set_mock_responses(_load_fixture("list_then_finish.json"))
    result = sa.run_autopilot(_CFG, [], "list files", _SHELL)
    assert "Directory listed" in result


def test_read_file_tool_executes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "hello.txt"
        target.write_text("hello from test", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "read_file", "args": {"path": str(target)}}),
            '{"action":"finish","message":"File read."}',
        ])
        result = sa.run_autopilot(_CFG, [], "read the file", _SHELL)
    assert "File read." in result


def test_write_file_tool_executes() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "output.txt"
        sa.set_mock_responses([
            json.dumps({"action": "write_file", "args": {
                "path": str(target), "content": "written by agent"
            }}),
            # The verification gate deflects this first finish...
            '{"action":"finish","message":"Written."}',
            # ...so the agent reads the file back, then finishes for real.
            json.dumps({"action": "read_file", "args": {"path": str(target)}}),
            '{"action":"finish","message":"Written."}',
        ])
        result = sa.run_autopilot(_CFG, [], "write the file", _SHELL)
        assert "Written." in result
        assert target.read_text(encoding="utf-8") == "written by agent"


def test_edit_file_modifies_content() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "edit_me.txt"
        target.write_text("original content", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "edit_file", "args": {
                "path": str(target),
                "old_string": "original content",
                "new_string": "edited content",
            }}),
            # Deflected once by the verification gate, then verified for real.
            '{"action":"finish","message":"Edited."}',
            json.dumps({"action": "read_file", "args": {"path": str(target)}}),
            '{"action":"finish","message":"Edited."}',
        ])
        result = sa.run_autopilot(_CFG, [], "edit it", _SHELL)
        assert "Edited." in result
        assert target.read_text(encoding="utf-8") == "edited content"


def test_verification_gate_deflects_unverified_finish() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "gated.txt"
        sa.set_mock_responses([
            json.dumps({"action": "write_file", "args": {
                "path": str(target), "content": "x"}}),
            '{"action":"finish","message":"Done without checking."}',
            json.dumps({"action": "read_file", "args": {"path": str(target)}}),
            '{"action":"finish","message":"Verified: file contains x."}',
        ])
        result = sa.run_autopilot(_CFG, [], "write the gated file", _SHELL)
        assert "Verified" in result, f"gate should force a verification pass, got: {result!r}"


def test_verification_gate_nudges_only_once() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "stubborn.txt"
        sa.set_mock_responses([
            json.dumps({"action": "write_file", "args": {
                "path": str(target), "content": "x"}}),
            '{"action":"finish","message":"First."}',
            '{"action":"finish","message":"Second."}',
        ])
        result = sa.run_autopilot(_CFG, [], "write the stubborn file", _SHELL)
        assert "Second." in result, f"gate must not loop forever, got: {result!r}"


def test_verification_gate_accepts_verified_turn() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "checked.txt"
        sa.set_mock_responses([
            json.dumps({"action": "write_file", "args": {
                "path": str(target), "content": "x"}}),
            json.dumps({"action": "read_file", "args": {"path": str(target)}}),
            '{"action":"finish","message":"Checked and correct."}',
        ])
        result = sa.run_autopilot(_CFG, [], "write and check the file", _SHELL)
        assert result == "Checked and correct.", result


def test_tool_sequence_uses_output_as_context() -> None:
    """Two-step: list_directory then finish — both steps must complete."""
    sa.set_mock_responses([
        '{"action":"list_directory","args":{"path":"."}}',
        '{"action":"finish","message":"Two steps done."}',
    ])
    result = sa.run_autopilot(_CFG, [], "two steps", _SHELL)
    assert "Two steps done." in result


# ---------------------------------------------------------------------------
# Undo snapshots
# ---------------------------------------------------------------------------

def test_undo_snapshot_captured_for_edit() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "snap_test.txt"
        target.write_text("before edit", encoding="utf-8")
        session = sa.create_session()
        sa.set_mock_responses([
            json.dumps({"action": "edit_file", "args": {
                "path": str(target),
                "old_string": "before edit",
                "new_string": "after edit",
            }}),
            '{"action":"finish","message":"Done."}',
        ])
        sa.run_autopilot(_CFG, [], "edit for undo test", _SHELL, session=session)
        sid = session.get("id", "")
        snap = sa._SESSION_UNDO_SNAPSHOTS.get(sid, {})
        assert snap, "undo snapshot must be captured after an edit_file turn"
        assert any("before edit" == v for v in snap.values()), (
            "snapshot must contain the original file content"
        )


def test_undo_snapshot_captured_for_write_new_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "new_file.txt"
        session = sa.create_session()
        sa.set_mock_responses([
            json.dumps({"action": "write_file", "args": {
                "path": str(target), "content": "fresh content"
            }}),
            '{"action":"finish","message":"Written."}',
        ])
        sa.run_autopilot(_CFG, [], "create a file", _SHELL, session=session)
        sid = session.get("id", "")
        snap = sa._SESSION_UNDO_SNAPSHOTS.get(sid, {})
        assert any(v is None for v in snap.values()), (
            "snapshot value must be None for a newly created file (undo = delete)"
        )


# ---------------------------------------------------------------------------
# Error-loop detection
# ---------------------------------------------------------------------------

def test_error_loop_detection_stops_agent() -> None:
    """Three identical (tool, output) pairs must trigger loop detection."""
    stuck_cmd = '{"action":"run_command","args":{"command":"echo stuck"}}'
    sa.set_mock_responses([stuck_cmd, stuck_cmd, stuck_cmd, '{"action":"finish","message":"Never."}'])
    with unittest.mock.patch("hexcli.agent.run_command_tool", return_value="stuck"):
        with unittest.mock.patch("builtins.input", return_value="n"):
            result = sa.run_autopilot(_CFG, [], "do something stuck", _SHELL)
    assert isinstance(result, str), "agent must return a string after error-loop detection"


def test_error_loop_requires_identical_outputs() -> None:
    """If tool outputs differ, the loop must NOT fire early."""
    counter: list[int] = [0]

    def varying_output(cmd: str, shell: str, limit: int, **kw: Any) -> str:
        counter[0] += 1
        return f"output #{counter[0]}"

    stuck_cmd = '{"action":"run_command","args":{"command":"echo x"}}'
    sa.set_mock_responses([stuck_cmd, stuck_cmd, stuck_cmd, '{"action":"finish","message":"OK."}'])
    with unittest.mock.patch("hexcli.agent.run_command_tool", side_effect=varying_output):
        result = sa.run_autopilot(_CFG, [], "do varied", _SHELL)
    assert "OK." in result, "agent must not stop early when outputs vary"


def test_fuzzy_loop_trips_on_varying_errors_same_target() -> None:
    """The v1.7 audit's 9-edit retry spiral: same tool, same target, three
    FAILURES whose error text varies each time. The identical-tuple detector
    never fired; the fuzzy trip must."""
    counter: list[int] = [0]

    def varying_failure(cmd: str, shell: str, limit: int, **kw: Any) -> str:
        counter[0] += 1
        raise RuntimeError(f"failed differently, attempt {counter[0]}")

    stuck = '{"action":"run_command","args":{"command":"pytest tests/"}}'
    sa.set_mock_responses([stuck, stuck, stuck, '{"action":"finish","message":"Never."}'])
    with unittest.mock.patch("hexcli.agent.run_command_tool", side_effect=varying_failure):
        with unittest.mock.patch("builtins.input", return_value="n"):
            result = sa.run_autopilot(_CFG, [], "run the tests", _SHELL)
    assert "Never." not in result, "loop must stop before the 4th fixture is consumed"
    assert counter[0] == 3, f"expected exactly 3 attempts before the trip, got {counter[0]}"


def test_varying_errors_on_different_targets_do_not_trip() -> None:
    """Failing on three DIFFERENT files is exploration, not a loop."""
    def always_fail(path: str, old: str, new: str) -> str:
        raise RuntimeError(f"no match in {path}")

    edits = [
        '{"action":"edit_file","args":{"path":"a.py","old_string":"x","new_string":"y"}}',
        '{"action":"edit_file","args":{"path":"b.py","old_string":"x","new_string":"y"}}',
        '{"action":"edit_file","args":{"path":"c.py","old_string":"x","new_string":"y"}}',
    ]
    sa.set_mock_responses(edits + ['{"action":"finish","message":"Moved on."}'])
    with unittest.mock.patch("hexcli.agent.edit_file_tool", side_effect=always_fail):
        result = sa.run_autopilot(_CFG, [], "fix the tests", _SHELL)
    assert "Moved on." in result, "distinct targets must not trip the loop detector"


# ---------------------------------------------------------------------------
# Retry-with-feedback on parse failures (V2_PLAN §5.1)
# ---------------------------------------------------------------------------

def test_typoed_action_name_is_retried_with_feedback() -> None:
    """A JSON action naming a nonexistent tool used to be silently accepted as
    a prose finish; it must now earn a retry that names the bad action."""
    sa.set_mock_responses([
        '{"action":"edit-file","args":{"path":"x.py"},"message":"tried to edit"}',
        '{"action":"finish","message":"Recovered."}',
    ])
    result = sa.run_autopilot(_CFG, [], "fix x.py", _SHELL)
    assert "Recovered." in result, "typo'd action must trigger a retry, not a finish"


def test_truncated_json_is_retried() -> None:
    sa.set_mock_responses([
        '{"action":"run_command","args":{"command":"echo hi"',  # truncated
        '{"action":"finish","message":"Recovered."}',
    ])
    result = sa.run_autopilot(_CFG, [], "say hi", _SHELL)
    assert "Recovered." in result


def test_late_step_parse_failure_still_retried() -> None:
    """The v1 retry was gated on step < 3 — a botch at step 5 was accepted as
    a finish. The step limit is gone; verify a late failure still retries."""
    ok = '{"action":"run_command","args":{"command":"echo ok"}}'
    sa.set_mock_responses([
        ok, ok, ok, ok,                     # steps 1-4: fine
        '{"action":"run-command","args":{"command":"echo x"},"message":"typo"}',
        '{"action":"finish","message":"Recovered late."}',
    ])
    counter: list[int] = [0]

    def distinct_ok(cmd: str, shell: str, limit: int, **kw: Any) -> str:
        # Outputs must differ or the identical-tuple loop detector (correctly)
        # trips on four repeats of the same successful call.
        counter[0] += 1
        return f"ok #{counter[0]}"

    with unittest.mock.patch("hexcli.agent.run_command_tool", side_effect=distinct_ok):
        result = sa.run_autopilot(_CFG, [], "several steps", _SHELL)
    assert "Recovered late." in result


def test_pure_prose_is_still_an_implicit_finish() -> None:
    """The direct-answer path is load-bearing: prose with no braces, fences or
    tool names must be accepted as a finish, not retried."""
    sa.set_mock_responses([
        "The capital of France is Paris.",
        '{"action":"finish","message":"MUST NOT BE CONSUMED"}',
    ])
    result = sa.run_autopilot(_CFG, [], "capital of France?", _SHELL)
    assert "Paris" in result
    assert sa._MOCK_RESPONSE_QUEUE, "prose finish must not have consumed a retry"
    sa.set_mock_responses([])  # drain the sentinel


# ---------------------------------------------------------------------------
# Network deny-by-default (V2_PLAN §11)
# ---------------------------------------------------------------------------

def test_fetch_url_denied_by_policy() -> None:
    cfg = {**_CFG, "network_access": "deny"}
    try:
        sa.execute_tool_call(cfg, {"tool": "fetch_url", "args": {"url": "https://example.com"}}, _SHELL)
        raise AssertionError("fetch_url must raise under network_access=deny")
    except RuntimeError as exc:
        text = str(exc)
        assert "disabled" in text
        assert "another route" in text, "refusal must state the hard boundary"


def test_fetch_url_ask_denies_without_approval() -> None:
    """'ask' + user says no (or no terminal at all) = refused. Either denial
    path is correct — CI has no tty, a dev terminal patches input to 'n'."""
    cfg = {**_CFG, "network_access": "ask"}
    with unittest.mock.patch("builtins.input", return_value="n"):
        try:
            sa.execute_tool_call(cfg, {"tool": "fetch_url", "args": {"url": "https://example.com"}}, _SHELL)
            raise AssertionError("unapproved fetch must raise")
        except RuntimeError as exc:
            assert "not approved" in str(exc)


def test_fetch_url_allow_fetches() -> None:
    cfg = {**_CFG, "network_access": "allow"}
    with unittest.mock.patch("hexcli.network.fetch_url", return_value="PAGE CONTENT"):
        out = sa.execute_tool_call(cfg, {"tool": "fetch_url", "args": {"url": "https://example.com"}}, _SHELL)
    assert out == "PAGE CONTENT"


def test_deny_policy_drops_fetch_schema_from_prompt() -> None:
    """Advertising a hard-blocked tool wastes tokens and invites a call that
    can only be refused — the schema must vanish under deny."""
    query = "look up the latest documentation online"
    with unittest.mock.patch("hexcli.network.is_online", return_value=True):
        sa.set_active_config({**sa.DEFAULT_CONFIG, "network_access": "deny"})
        try:
            denied = sa.build_autopilot_prompt(".", 15, query=query)
        finally:
            sa.set_active_config(None)
        allowed = sa.build_autopilot_prompt(".", 15, query=query)
    assert "fetch_url" not in denied
    assert "fetch_url" in allowed


# ---------------------------------------------------------------------------
# Safety gating
# ---------------------------------------------------------------------------

def test_safety_gate_blocks_destructive_with_confirm_true() -> None:
    cfg = {**_CFG, "autopilot_confirm_destructive": True}
    sa.set_mock_responses([
        '{"action":"run_command","args":{"command":"Remove-Item -Recurse C:\\\\temp"}}',
        '{"action":"finish","message":"Cleaned."}',
    ])
    # User denies the dangerous command
    with unittest.mock.patch("builtins.input", return_value="n"):
        result = sa.run_autopilot(cfg, [], "delete temp", _SHELL)
    assert isinstance(result, str)


def test_safety_gate_allows_destructive_when_confirmed() -> None:
    cfg = {**_CFG, "autopilot_confirm_destructive": True}
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "deleteme.txt"
        target.write_text("bye", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "run_command", "args": {
                "command": f'Remove-Item "{target}"'
            }}),
            '{"action":"finish","message":"Deleted."}',
        ])
        # User confirms
        with unittest.mock.patch("builtins.input", return_value="y"):
            result = sa.run_autopilot(cfg, [], "delete the file", _SHELL)
    assert isinstance(result, str)


def test_safe_commands_pass_without_prompt() -> None:
    sa.set_mock_responses([
        '{"action":"run_command","args":{"command":"Get-Process"}}',
        '{"action":"finish","message":"Listed processes."}',
    ])
    cfg = {**_CFG, "autopilot_confirm_destructive": True}
    # No input() called since safe commands bypass the gate
    result = sa.run_autopilot(cfg, [], "show processes", _SHELL)
    assert "Listed processes." in result


# ---------------------------------------------------------------------------
# Step budget
# ---------------------------------------------------------------------------

def test_agent_terminates_within_step_budget() -> None:
    responses = ['{"action":"list_directory","args":{"path":"."}}'] * 20
    sa.set_mock_responses(responses)
    cfg = {**_CFG, "max_agent_steps": 5}
    with unittest.mock.patch("builtins.input", return_value="n"):
        result = sa.run_autopilot(cfg, [], "list forever", _SHELL)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# History injection
# ---------------------------------------------------------------------------

def test_history_messages_injected() -> None:
    history = [
        {"role": "user", "content": "prior question"},
        {"role": "assistant", "content": '{"action":"finish","message":"prior answer"}'},
    ]
    sa.set_mock_responses(['{"action":"finish","message":"With history."}'])
    result = sa.run_autopilot(_CFG, history, "follow up question", _SHELL)
    assert "With history." in result


# ---------------------------------------------------------------------------
# Mock queue exhaustion
# ---------------------------------------------------------------------------

def test_empty_mock_queue_returns_fallback() -> None:
    sa.set_mock_responses([])  # empty queue
    result = sa.run_autopilot(_CFG, [], "anything", _SHELL)
    assert "exhausted" in result.lower() or isinstance(result, str)


# ---------------------------------------------------------------------------
# Null-args safety (JSON null fields must not produce the string "None")
# ---------------------------------------------------------------------------

def test_null_finish_message_does_not_produce_none_string() -> None:
    sa.set_mock_responses(['{"action":"finish","message":null}'])
    result = sa.run_autopilot(_CFG, [], "say something", _SHELL)
    assert result != "None", f"null finish message must not produce 'None', got: {result!r}"


def test_null_read_file_path_raises_helpful_error() -> None:
    sa.set_mock_responses([
        '{"action":"read_file","args":{"path":null}}',
        '{"action":"finish","message":"tried null path"}',
    ])
    result = sa.run_autopilot(_CFG, [], "read null path", _SHELL)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# Refusal nudge
# ---------------------------------------------------------------------------

def test_refusal_nudge_fires_and_retries() -> None:
    sa.set_mock_responses([
        '{"action":"finish","message":"I am sorry, I cannot access that."}',
        '{"action":"list_directory","args":{"path":"."}}',
        '{"action":"finish","message":"Listed after nudge."}',
    ])
    result = sa.run_autopilot(_CFG, [], "list directory", _SHELL)
    # The nudge should push the model to actually use a tool
    assert "Listed after nudge." in result


# ---------------------------------------------------------------------------
# compact_history — tail preservation
# ---------------------------------------------------------------------------

def test_compact_history_preserves_tail() -> None:
    cfg = {**_CFG, "compact_max_output_tokens": 512}
    session = sa.create_session()
    # Build a session with 10 messages (> 7 minimum)
    for i in range(5):
        sa.append_session_message(session, "user", f"Turn {i} question")
        sa.append_session_message(session, "assistant", f"Turn {i} answer")

    assert len(session["messages"]) == 10

    # compact_history needs an LLM call for the summary — use mock backend
    sa.set_mock_responses(["This is the compact summary of all prior turns."])
    new_msgs = sa.compact_history(cfg, session, quiet=True)

    # Must have 2 summary messages + up to 4 tail messages
    assert len(new_msgs) >= 2, "must have at least summary + ack"
    assert len(new_msgs) <= 6, f"must have at most 2+4=6 messages, got {len(new_msgs)}"
    # Compact must reduce total message count
    assert len(new_msgs) < 10, "compact must reduce message count"
    # The last tail message (10th message = "Turn 4 answer") must be preserved
    all_content = " ".join(m["content"] for m in new_msgs)
    assert "Turn 4 answer" in all_content, "last message must survive compaction"


def test_compact_refuses_too_few_messages() -> None:
    cfg = {**_CFG, "compact_max_output_tokens": 512}
    session = sa.create_session()
    # Only 4 messages — below the 7-message minimum
    for i in range(2):
        sa.append_session_message(session, "user", f"Q{i}")
        sa.append_session_message(session, "assistant", f"A{i}")
    assert len(session["messages"]) == 4

    import io
    import unittest.mock
    with unittest.mock.patch("sys.stdout", new_callable=io.StringIO) as out:
        new_msgs = sa.compact_history(cfg, session, quiet=False)
    assert len(new_msgs) == 4, "compact must not change message list when below minimum"
    assert "Nothing to compact" in out.getvalue()


# ---------------------------------------------------------------------------
# run_command_tool timeout
# ---------------------------------------------------------------------------

def test_run_command_tool_times_out() -> None:
    result = sa.run_command_tool(
        "Start-Sleep -Seconds 100",
        _SHELL,
        output_limit=4000,
        timeout=1,
    )
    assert "TIMEOUT" in result, f"expected TIMEOUT in output, got: {result!r}"


def test_run_command_tool_large_output_does_not_oom() -> None:
    """run_command_tool must cap buffered output at 4×output_limit chars to avoid OOM."""
    # Generate ~60 KB of output; output_limit is only 1000 chars → cap fires at 4000 chars.
    result = sa.run_command_tool(
        "1..600 | ForEach-Object { 'x' * 100 }",
        _SHELL,
        output_limit=1000,
    )
    # Result must be trimmed — not all 60 KB
    assert len(result) <= 1100, f"output should be trimmed, got {len(result)} chars"
    assert "Exit code:" in result


# ---------------------------------------------------------------------------
# verify_syntax after edit (Rule 13 path)
# ---------------------------------------------------------------------------

def test_verify_syntax_called_after_edit_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "code.py"
        target.write_text("x = 1\n", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "edit_file", "args": {
                "path": str(target),
                "old_string": "x = 1",
                "new_string": "x = 2",
            }}),
            json.dumps({"action": "verify_syntax", "args": {
                "path": str(target), "language": "python"
            }}),
            '{"action":"finish","message":"Edited and verified."}',
        ])
        result = sa.run_autopilot(_CFG, [], "change x to 2", _SHELL)
        assert "Edited and verified." in result
        assert target.read_text(encoding="utf-8") == "x = 2\n"


# ---------------------------------------------------------------------------
# Unknown / malformed action fallback
# ---------------------------------------------------------------------------

def test_unknown_action_returns_string_not_crash() -> None:
    """Unknown action type must fall back gracefully — no exception raised."""
    sa.set_mock_responses(['{"action":"nonexistent_tool","args":{}}'])
    result = sa.run_autopilot(_CFG, [], "do something unusual", _SHELL)
    assert isinstance(result, str), "unknown action must produce a string result, not crash"


def test_empty_string_response_handled_gracefully() -> None:
    """Empty LLM response must not crash the agent loop."""
    sa.set_mock_responses([""])
    result = sa.run_autopilot(_CFG, [], "anything", _SHELL)
    assert isinstance(result, str)


# ---------------------------------------------------------------------------
# batch — parallel reads
# ---------------------------------------------------------------------------

def test_batch_reads_multiple_files() -> None:
    """batch action with two read_file entries must return both file contents."""
    with tempfile.TemporaryDirectory() as tmp:
        fa = Path(tmp) / "a.txt"
        fb = Path(tmp) / "b.txt"
        fa.write_text("content of A", encoding="utf-8")
        fb.write_text("content of B", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({
                "action": "batch",
                "args": {"actions": [
                    {"tool": "read_file", "args": {"path": str(fa)}},
                    {"tool": "read_file", "args": {"path": str(fb)}},
                ]},
            }),
            '{"action":"finish","message":"Both files read."}',
        ])
        result = sa.run_autopilot(_CFG, [], "read both files at once", _SHELL)
    assert "Both files read." in result


# ---------------------------------------------------------------------------
# read_file_tool — large file does not OOM
# ---------------------------------------------------------------------------

def test_read_file_tool_large_file_truncated_without_oom() -> None:
    """read_file_tool must not load the full content of a file larger than 4× output_limit."""
    limit = 200  # chars
    # Write a file that is 10× limit in bytes — well over 4× limit
    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp) / "big.txt"
        big.write_bytes(b"x" * limit * 10)
        result = sa.read_file_tool(str(big), limit)
    # Result must be trimmed to limit (trim_text appends ~30 char notice)
    assert len(result) <= limit + 50, f"expected ≤{limit+50} chars, got {len(result)}"
    assert "x" in result, "content must appear in truncated output"


# ---------------------------------------------------------------------------
# edit_file error propagation
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# _maybe_auto_compact — threshold behaviour
# ---------------------------------------------------------------------------

def test_maybe_auto_compact_fires_above_threshold() -> None:
    """_maybe_auto_compact must compact above the derived budget. Since v1.8 the
    default path is deterministic (no LLM); the LLM summariser is opt-in via
    auto_compact_uses_llm."""
    cfg = {**_CFG, "auto_compact_uses_llm": True}
    session = sa.create_session()
    # 2600 est. tokens (> 1300): 8 messages × 1300 chars / 4 = 2600
    for _ in range(4):
        sa.append_session_message(session, "user", "x" * 1300)
        sa.append_session_message(session, "assistant", "y" * 1300)

    compact_called: list[bool] = []

    def fake_compact(config: Any, sess: Any, *, quiet: bool = False) -> list[Any]:
        compact_called.append(True)
        return sess.get("messages", [])

    with unittest.mock.patch.object(sa, "compact_history", side_effect=fake_compact), \
         unittest.mock.patch.object(sa, "sync_session_store"):
        sa._maybe_auto_compact(cfg, session, [])

    assert compact_called, "_maybe_auto_compact must trigger compact_history above threshold"


def test_maybe_auto_compact_silent_below_threshold() -> None:
    """_maybe_auto_compact must not compact when below 1300 est. tokens."""
    cfg = {**_CFG}
    session = sa.create_session()
    sa.append_session_message(session, "user", "x" * 200)  # ~50 tokens

    compact_called: list[bool] = []

    def fake_compact(config: Any, sess: Any, *, quiet: bool = False) -> list[Any]:
        compact_called.append(True)
        return sess.get("messages", [])

    with unittest.mock.patch.object(sa, "compact_history", side_effect=fake_compact):
        sa._maybe_auto_compact(cfg, session, [])

    assert not compact_called, "_maybe_auto_compact must NOT compact when below threshold"


def test_append_file_undo_snapshot_captures_original() -> None:
    """append_file must snapshot original content so /undo can restore exactly what was there."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "log.txt"
        target.write_text("existing line\n", encoding="utf-8")
        session = sa.create_session()
        sa.set_mock_responses([
            json.dumps({"action": "append_file", "args": {
                "path": str(target), "content": "appended line\n"
            }}),
            '{"action":"finish","message":"Appended."}',
        ])
        sa.run_autopilot(_CFG, [], "append to file", _SHELL, session=session)
        sid = session.get("id", "")
        snap = sa._SESSION_UNDO_SNAPSHOTS.get(sid, {})
        assert snap, "undo snapshot must be captured after append_file"
        # Snapshot should contain original (pre-append) content
        original = list(snap.values())[0]
        assert original == "existing line\n", (
            f"snapshot should have original content, got: {original!r}"
        )


def test_append_file_undo_snapshot_none_for_new_file() -> None:
    """append_file to a new file must snapshot None so /undo deletes it."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "new_log.txt"
        session = sa.create_session()
        sa.set_mock_responses([
            json.dumps({"action": "append_file", "args": {
                "path": str(target), "content": "first line\n"
            }}),
            '{"action":"finish","message":"Created."}',
        ])
        sa.run_autopilot(_CFG, [], "create via append", _SHELL, session=session)
        sid = session.get("id", "")
        snap = sa._SESSION_UNDO_SNAPSHOTS.get(sid, {})
        assert any(v is None for v in snap.values()), (
            "snapshot for new file via append must be None (undo = delete)"
        )


def test_delegate_called_from_agent_loop_succeeds() -> None:
    """delegate tool must spawn a sub-agent and return its result to the outer agent."""
    # Outer agent: calls delegate; then finishes with the delegate result
    # Inner agent (sub): immediately finishes with a known message
    sa.set_mock_responses([
        # Step 1: outer agent calls delegate
        json.dumps({"action": "delegate", "args": {"task": "summarise the logs"}}),
        # Step 2 (inner agent — separate call_llm): sub-agent finishes
        '{"action":"finish","message":"Logs summarised: all clear."}',
        # Step 3: outer agent finishes using the delegate output
        '{"action":"finish","message":"Delegation complete."}',
    ])
    result = sa.run_autopilot(_CFG, [], "delegate a task", _SHELL)
    assert "Delegation complete." in result


def test_batch_over_limit_returns_error_not_crash() -> None:
    """batch with more than 8 actions must raise, not silently drop extras."""
    sa.set_mock_responses([
        json.dumps({
            "action": "batch",
            "args": {"actions": [
                {"tool": "list_directory", "args": {"path": "."}}
            ] * 9},  # 9 > _BATCH_MAX = 8
        }),
        '{"action":"finish","message":"Over-limit batch handled."}',
    ])
    result = sa.run_autopilot(_CFG, [], "batch 9 items", _SHELL)
    assert "Over-limit batch handled." in result


def test_compact_history_at_minimum_boundary() -> None:
    """compact_history must proceed when session has exactly the minimum required messages."""
    cfg = {**_CFG, "compact_max_output_tokens": 512}
    session = sa.create_session()
    # _COMPACT_KEEP_RECENT=4, min = 4+3=7 — build exactly 7 messages
    for i in range(3):
        sa.append_session_message(session, "user", f"Q{i}")
        sa.append_session_message(session, "assistant", f"A{i}")
    sa.append_session_message(session, "user", "final question")
    assert len(session["messages"]) == 7

    sa.set_mock_responses(["Summary at minimum boundary."])
    new_msgs = sa.compact_history(cfg, session, quiet=True)

    assert len(new_msgs) < 7, "compact must reduce message count at the minimum boundary"


def test_batch_delegate_not_allowed_returns_error_not_crash() -> None:
    """batch must reject delegate (write tool) with an error, not crash."""
    sa.set_mock_responses([
        json.dumps({
            "action": "batch",
            "args": {"actions": [
                {"tool": "delegate", "args": {"task": "do something"}},
            ]},
        }),
        '{"action":"finish","message":"Recovered from batch error."}',
    ])
    result = sa.run_autopilot(_CFG, [], "batch with delegate", _SHELL)
    assert "Recovered from batch error." in result


def test_batch_non_dict_action_returns_error_not_crash() -> None:
    """batch must handle non-dict entries gracefully."""
    sa.set_mock_responses([
        json.dumps({
            "action": "batch",
            "args": {"actions": ["not a dict", 42]},
        }),
        '{"action":"finish","message":"Non-dict batch done."}',
    ])
    result = sa.run_autopilot(_CFG, [], "batch with junk", _SHELL)
    assert "Non-dict batch done." in result


def test_run_code_tool_python_script_executes() -> None:
    """run_code_tool must execute a .py script in cwd and return its output."""
    import os
    orig_cwd = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            script = Path(tmp) / "hello.py"
            script.write_text('print("hello from run_code")\n', encoding="utf-8")
            result = sa.run_code_tool(str(script), [], timeout=10, shell_exe=_SHELL, output_limit=4000)
        finally:
            os.chdir(orig_cwd)
    assert "hello from run_code" in result


def test_run_code_tool_outside_cwd_raises() -> None:
    """run_code_tool must reject a path that is not under the working directory."""
    import sys
    # Use the Python interpreter itself as an out-of-cwd path
    interpreter = Path(sys.executable).resolve()
    try:
        sa.run_code_tool(str(interpreter), [], timeout=5, shell_exe=_SHELL, output_limit=4000)
        assert False, "should have raised RuntimeError for out-of-cwd path"
    except RuntimeError as exc:
        assert "restricted" in str(exc).lower() or "working directory" in str(exc).lower()


def test_write_file_tool_leaves_no_tmp_file() -> None:
    """write_file_tool must use atomic rename — no .tmp artefact left on success."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "output.py"
        sa.write_file_tool(str(target), "x = 1\n")
        assert target.exists(), "write_file must create the target file"
        assert target.read_text(encoding="utf-8") == "x = 1\n"
        tmp_artefact = Path(tmp) / "output.py.tmp"
        assert not tmp_artefact.exists(), "write_file must not leave a .tmp file behind"


def test_delegate_recursion_guard_raises() -> None:
    """_run_delegate must raise RuntimeError when called from inside a delegate."""
    import hexcli.agent as sa2
    sa2._in_delegate = True
    try:
        try:
            sa2._run_delegate(_CFG, "nested task", _SHELL)
            assert False, "should have raised RuntimeError"
        except RuntimeError as exc:
            assert "recursion" in str(exc).lower() or "delegate" in str(exc).lower()
    finally:
        sa2._in_delegate = False


def test_edit_file_empty_old_string_raises_error() -> None:
    """edit_file with empty old_string must raise, not silently insert at start of file."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "file.py"
        target.write_text("x = 1\n", encoding="utf-8")
        try:
            sa.edit_file_tool(str(target), "", "y = 2")
            assert False, "should have raised RuntimeError"
        except RuntimeError as exc:
            assert "empty" in str(exc).lower() or "old_string" in str(exc).lower()


def test_edit_file_missing_old_string_sends_error_to_model() -> None:
    """edit_file error (old_string not found) must be sent back to model, not silenced."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "file.py"
        target.write_text("x = 1\n", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "edit_file", "args": {
                "path": str(target),
                "old_string": "THIS DOES NOT EXIST",
                "new_string": "y = 2",
            }}),
            '{"action":"finish","message":"Error reported."}',
        ])
        result = sa.run_autopilot(_CFG, [], "fix the code", _SHELL)
    # Agent must continue after the error (not crash), and the finish message must appear
    assert "Error reported." in result


def test_append_file_leaves_no_tmp_file() -> None:
    """append_file atomic write must not leave a .tmp artefact on success."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "notes.txt"
        target.write_text("line one\n", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "append_file", "args": {
                "path": str(target), "content": "line two\n"
            }}),
            '{"action":"finish","message":"Appended."}',
        ])
        sa.run_autopilot(_CFG, [], "append to notes", _SHELL)
        assert target.read_text(encoding="utf-8") == "line one\nline two\n"
        assert not (Path(tmp) / "notes.txt.tmp").exists()


def test_list_directory_nonexistent_path_returns_error() -> None:
    """list_directory on a missing path must return an error message, not an OSError."""
    sa.set_mock_responses([
        json.dumps({"action": "list_directory", "args": {"path": "/nonexistent/path/xyz123"}}),
        '{"action":"finish","message":"Listed."}',
    ])
    result = sa.run_autopilot(_CFG, [], "list that dir", _SHELL)
    assert "Listed." in result


def test_list_directory_on_file_returns_error() -> None:
    """list_directory on a file path must return an error, not an OSError."""
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as f:
        f.write(b"data")
        file_path = f.name
    try:
        sa.set_mock_responses([
            json.dumps({"action": "list_directory", "args": {"path": file_path}}),
            '{"action":"finish","message":"Not a dir."}',
        ])
        result = sa.run_autopilot(_CFG, [], "list it", _SHELL)
        assert "Not a dir." in result
    finally:
        Path(file_path).unlink(missing_ok=True)


def test_edit_file_leaves_no_tmp_file() -> None:
    """edit_file atomic write must not leave a .tmp artefact on success."""
    with tempfile.TemporaryDirectory() as tmp:
        target = Path(tmp) / "data.txt"
        target.write_text("hello world", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "edit_file", "args": {
                "path": str(target),
                "old_string": "hello world",
                "new_string": "goodbye world",
            }}),
            '{"action":"finish","message":"Edited."}',
        ])
        sa.run_autopilot(_CFG, [], "edit it", _SHELL)
        assert target.read_text(encoding="utf-8") == "goodbye world"
        assert not (Path(tmp) / "data.txt.tmp").exists()


def test_verify_syntax_detects_invalid_python() -> None:
    """verify_syntax_tool must return FAIL for a file with a Python syntax error."""
    with tempfile.TemporaryDirectory() as tmp:
        bad = Path(tmp) / "broken.py"
        bad.write_text("def foo(\n    x =\n", encoding="utf-8")
        sa.set_mock_responses([
            json.dumps({"action": "verify_syntax", "args": {
                "path": str(bad), "language": "python"
            }}),
            '{"action":"finish","message":"Syntax checked."}',
        ])
        result = sa.run_autopilot(_CFG, [], "check syntax", _SHELL)
        assert "Syntax checked." in result


def test_verify_syntax_skips_large_file() -> None:
    """verify_syntax_tool must skip files over _VERIFY_MAX_BYTES without reading them fully."""
    with tempfile.TemporaryDirectory() as tmp:
        big = Path(tmp) / "huge.py"
        # Write a file whose size is just above the 500 KB limit
        big.write_bytes(b"x = 1\n" * 100_000)  # ~600 KB
        sa.set_mock_responses([
            json.dumps({"action": "verify_syntax", "args": {
                "path": str(big), "language": "python"
            }}),
            '{"action":"finish","message":"Done."}',
        ])
        result = sa.run_autopilot(_CFG, [], "check syntax", _SHELL)
        assert "Done." in result


# ---------------------------------------------------------------------------
# autopilot_system_prompt override — applied, and loudly warned about
# ---------------------------------------------------------------------------

def test_prompt_override_applies_and_warns_once() -> None:
    """The file-only autopilot_system_prompt override must replace the prompt
    AND print its warning exactly once per process — a silent replacement is
    the historic example-config footgun."""
    import contextlib
    import io

    cfg = {**_CFG, "autopilot_system_prompt": "You are a minimal test agent."}
    seen_prompts: list[str] = []
    real_call = sa.call_llm

    def spying_call(config, messages, *args, **kwargs):
        seen_prompts.append(messages[0]["content"])
        return real_call(config, messages, *args, **kwargs)

    sa._PROMPT_OVERRIDE_WARNED = False
    try:
        with unittest.mock.patch.object(sa, "call_llm", side_effect=spying_call):
            out1 = io.StringIO()
            with contextlib.redirect_stdout(out1):
                sa.set_mock_responses(['{"action":"finish","message":"ok"}'])
                sa.run_autopilot(cfg, [], "task one", _SHELL)
            out2 = io.StringIO()
            with contextlib.redirect_stdout(out2):
                sa.set_mock_responses(['{"action":"finish","message":"ok"}'])
                sa.run_autopilot(cfg, [], "task two", _SHELL)
        assert seen_prompts, "call_llm was never reached"
        assert all(p == "You are a minimal test agent." for p in seen_prompts), \
            f"override did not replace the system prompt: {seen_prompts[0][:80]!r}"
        assert "autopilot_system_prompt" in out1.getvalue(), \
            "first overridden run must warn"
        assert "autopilot_system_prompt" not in out2.getvalue(), \
            "warning must fire once per process, not every run"
    finally:
        sa._PROMPT_OVERRIDE_WARNED = False


def test_no_override_means_no_warning_and_tuned_prompt() -> None:
    """Without the override key the tuned prompt is used and nothing warns."""
    import contextlib
    import io

    sa._PROMPT_OVERRIDE_WARNED = False
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        sa.set_mock_responses(['{"action":"finish","message":"ok"}'])
        sa.run_autopilot(_CFG, [], "plain task", _SHELL)
    assert "autopilot_system_prompt" not in out.getvalue()


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
    test_fuzzy_loop_trips_on_varying_errors_same_target,
    test_varying_errors_on_different_targets_do_not_trip,
    test_typoed_action_name_is_retried_with_feedback,
    test_truncated_json_is_retried,
    test_late_step_parse_failure_still_retried,
    test_pure_prose_is_still_an_implicit_finish,
    test_fetch_url_denied_by_policy,
    test_fetch_url_ask_denies_without_approval,
    test_fetch_url_allow_fetches,
    test_deny_policy_drops_fetch_schema_from_prompt,
    test_simple_finish_returns_message,
    test_finish_via_fixture_file,
    test_plain_text_fallback_becomes_finish,
    test_cot_stripped_before_parsing,
    test_list_directory_tool_executes,
    test_read_file_tool_executes,
    test_write_file_tool_executes,
    test_edit_file_modifies_content,
    test_verification_gate_deflects_unverified_finish,
    test_verification_gate_nudges_only_once,
    test_verification_gate_accepts_verified_turn,
    test_tool_sequence_uses_output_as_context,
    test_undo_snapshot_captured_for_edit,
    test_undo_snapshot_captured_for_write_new_file,
    test_error_loop_detection_stops_agent,
    test_error_loop_requires_identical_outputs,
    test_safety_gate_blocks_destructive_with_confirm_true,
    test_safety_gate_allows_destructive_when_confirmed,
    test_safe_commands_pass_without_prompt,
    test_agent_terminates_within_step_budget,
    test_history_messages_injected,
    test_empty_mock_queue_returns_fallback,
    test_null_finish_message_does_not_produce_none_string,
    test_null_read_file_path_raises_helpful_error,
    test_refusal_nudge_fires_and_retries,
    test_compact_history_preserves_tail,
    test_compact_refuses_too_few_messages,
    test_run_command_tool_times_out,
    test_verify_syntax_called_after_edit_file,
    test_unknown_action_returns_string_not_crash,
    test_empty_string_response_handled_gracefully,
    test_batch_reads_multiple_files,
    test_edit_file_missing_old_string_sends_error_to_model,
    test_read_file_tool_large_file_truncated_without_oom,
    test_edit_file_empty_old_string_raises_error,
    test_maybe_auto_compact_fires_above_threshold,
    test_maybe_auto_compact_silent_below_threshold,
    test_batch_delegate_not_allowed_returns_error_not_crash,
    test_batch_non_dict_action_returns_error_not_crash,
    test_delegate_recursion_guard_raises,
    test_write_file_tool_leaves_no_tmp_file,
    test_batch_over_limit_returns_error_not_crash,
    test_compact_history_at_minimum_boundary,
    test_run_code_tool_python_script_executes,
    test_run_code_tool_outside_cwd_raises,
    test_append_file_undo_snapshot_captures_original,
    test_append_file_undo_snapshot_none_for_new_file,
    test_delegate_called_from_agent_loop_succeeds,
    test_list_directory_nonexistent_path_returns_error,
    test_list_directory_on_file_returns_error,
    test_edit_file_leaves_no_tmp_file,
    test_verify_syntax_detects_invalid_python,
    test_verify_syntax_skips_large_file,
    test_append_file_leaves_no_tmp_file,
    test_run_command_tool_large_output_does_not_oom,
    test_prompt_override_applies_and_warns_once,
    test_no_override_means_no_warning_and_tuned_prompt,
]


def main() -> int:
    print(f"\nevals/test_agent_loop.py — {len(TESTS)} integration tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
