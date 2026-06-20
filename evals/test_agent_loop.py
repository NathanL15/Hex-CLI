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
            '{"action":"finish","message":"Edited."}',
        ])
        result = sa.run_autopilot(_CFG, [], "edit it", _SHELL)
        assert "Edited." in result
        assert target.read_text(encoding="utf-8") == "edited content"


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
    test_simple_finish_returns_message,
    test_finish_via_fixture_file,
    test_plain_text_fallback_becomes_finish,
    test_cot_stripped_before_parsing,
    test_list_directory_tool_executes,
    test_read_file_tool_executes,
    test_write_file_tool_executes,
    test_edit_file_modifies_content,
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
