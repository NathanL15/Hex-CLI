#!/usr/bin/env python3
"""evals/test_escalation.py — Unit tests for the local escalation ladder.

The escalation model itself is mocked (LocalEscalator.consult patched);
everything else — trigger detection, advice injection, loop continuation, and
every degrade-gracefully path — runs through the production run_autopilot with
the mock LLM backend.

Triggers under test (docs/V2_PLAN.md §4 / §14.7):
  A. loop-detector trip  → consult before giving up
  B. verification nudge ignored → consult before accepting the finish
  C. prose-instead-of-action on an edit request → consult (with the
     clarifying-question exemption so ambiguous requests still just ask)

Usage:
    python evals/test_escalation.py
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest.mock
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from hexcli import local_escalation  # noqa: E402

_CFG: dict[str, Any] = {
    **sa.DEFAULT_CONFIG,
    "backend": "mock",
    "max_agent_steps": 10,
    "autopilot_confirm_destructive": False,
    "memory_enabled": False,
    "telemetry_enabled": False,
    "anthropic_api_key": "",
    "escalation_local_model": "qwen3-8b",
    # Fixtures live at absolute temp paths; scoping is covered by
    # evals/test_write_scope.py (see note there).
    "workspace_write_scope": False,
}

_SHELL = "powershell.exe"


class _FakeEscalator:
    """Stands in for LocalEscalator: records consultations, returns advice."""

    def __init__(self, advice: str | None = "Try editing line 2 instead.") -> None:
        self.advice = advice
        self.calls: list[str] = []
        self.enabled = True

    def consult(self, situation: str) -> str | None:
        self.calls.append(situation)
        return self.advice


def _with_escalator(esc: _FakeEscalator):
    return unittest.mock.patch.object(sa, "_get_escalator", return_value=esc)


def _tool(tool: str, **args: Any) -> str:
    return json.dumps({"action": tool, "args": args})


def _finish(message: str) -> str:
    return json.dumps({"action": "finish", "message": message})


# ---------------------------------------------------------------------------
# Trigger A — loop detector
# ---------------------------------------------------------------------------

def test_loop_trip_consults_and_recovers() -> None:
    esc = _FakeEscalator()
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "f.txt"
        f.write_text("real content\n", encoding="utf-8")
        bad = _tool("edit_file", path=str(f), old_string="TOTALLY WRONG TEXT THAT IS NOWHERE", new_string="x")
        good = _tool("edit_file", path=str(f), old_string="real content", new_string="fixed content")
        sa.set_mock_responses([
            bad, bad, bad,          # trips the loop detector
            good,                    # after advice, the model succeeds
            _tool("read_file", path=str(f)),
            _finish("Fixed after advice."),
        ])
        with _with_escalator(esc):
            result = sa.run_autopilot(_CFG, [], "fix the f file text", _SHELL)
        assert len(esc.calls) == 1, f"expected exactly one consult, got {len(esc.calls)}"
        assert "3 times" in esc.calls[0]
        assert "Fixed after advice." in result
        assert "fixed content" in f.read_text(encoding="utf-8")


def test_loop_trip_with_failed_consult_degrades_to_old_behavior() -> None:
    esc = _FakeEscalator(advice=None)  # consult fails
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "f.txt"
        f.write_text("real content\n", encoding="utf-8")
        bad = _tool("edit_file", path=str(f), old_string="TOTALLY WRONG TEXT THAT IS NOWHERE", new_string="x")
        sa.set_mock_responses([bad, bad, bad, _finish("never reached")])
        with _with_escalator(esc):
            result = sa.run_autopilot(_CFG, [], "fix the f file text", _SHELL)
        assert len(esc.calls) == 1
        assert "never reached" not in result, "loop must stop as before when consult fails"


def test_no_escalator_means_original_loop_stop() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "f.txt"
        f.write_text("real content\n", encoding="utf-8")
        bad = _tool("edit_file", path=str(f), old_string="TOTALLY WRONG TEXT THAT IS NOWHERE", new_string="x")
        sa.set_mock_responses([bad, bad, bad, _finish("never reached")])
        cfg = {**_CFG, "escalation_local_model": ""}
        result = sa.run_autopilot(cfg, [], "fix the f file text", _SHELL)
        assert "never reached" not in result


# ---------------------------------------------------------------------------
# Trigger B — verification nudge ignored
# ---------------------------------------------------------------------------

def test_ignored_verify_nudge_consults() -> None:
    esc = _FakeEscalator(advice="Read the file back before answering.")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "out.txt"
        sa.set_mock_responses([
            _tool("write_file", path=str(f), content="data"),
            _finish("Done!"),          # deflected by the verify nudge
            _finish("Really done!"),   # ignores the nudge → trigger B
            _tool("read_file", path=str(f)),
            _finish("Verified the write."),
        ])
        with _with_escalator(esc):
            result = sa.run_autopilot(_CFG, [], "write the out file", _SHELL)
        assert len(esc.calls) == 1
        assert "WITHOUT verifying" in esc.calls[0]
        assert "Verified the write." in result


# ---------------------------------------------------------------------------
# Trigger C — prose instead of action
# ---------------------------------------------------------------------------

def test_edit_request_finishing_without_mutation_consults() -> None:
    esc = _FakeEscalator(advice="Use edit_file on app.py replacing X with Y.")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "app.py"
        f.write_text("value = 1\n", encoding="utf-8")
        sa.set_mock_responses([
            _tool("read_file", path=str(f)),
            _finish("The file could be improved by changing value."),  # prose, no edit
            _tool("edit_file", path=str(f), old_string="value = 1", new_string="value = 2"),
            _tool("read_file", path=str(f)),
            _finish("Changed value to 2."),
        ])
        with _with_escalator(esc):
            result = sa.run_autopilot(_CFG, [], f"fix the value in {f}", _SHELL)
        assert len(esc.calls) == 1
        assert "without having modified any file" in esc.calls[0]
        assert "Changed value to 2." in result
        assert "value = 2" in f.read_text(encoding="utf-8")


def test_clarifying_question_is_exempt_from_trigger_c() -> None:
    esc = _FakeEscalator()
    sa.set_mock_responses([
        _finish("Which file would you like me to fix?"),
    ])
    with _with_escalator(esc):
        result = sa.run_autopilot(_CFG, [], "Fix my code.", _SHELL)
    assert esc.calls == [], "a clarifying question must NOT trigger escalation"
    assert result.endswith("?")


def test_non_edit_request_is_exempt_from_trigger_c() -> None:
    esc = _FakeEscalator()
    sa.set_mock_responses([
        _finish("Binary search is O(log n)."),
    ])
    with _with_escalator(esc):
        result = sa.run_autopilot(
            _CFG, [], "What is the time complexity of binary search?", _SHELL)
    assert esc.calls == [], "knowledge questions must never escalate"
    assert "O(log n)" in result


def test_at_most_one_consult_per_turn() -> None:
    # Trigger C fires; the model then STILL finishes without mutating —
    # no second consult may occur.
    esc = _FakeEscalator(advice="Edit the file.")
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "app.py"
        f.write_text("value = 1\n", encoding="utf-8")
        sa.set_mock_responses([
            _finish("I think the file is fine actually."),
            _finish("Still not editing anything."),
        ])
        cfg = {**_CFG}
        with _with_escalator(esc):
            result = sa.run_autopilot(cfg, [], f"fix the value in {f}", _SHELL)
        assert len(esc.calls) == 1, f"one consult max per turn, got {len(esc.calls)}"
        assert "Still not editing" in result


# ---------------------------------------------------------------------------
# Helper coverage
# ---------------------------------------------------------------------------

def test_edit_intent_detection() -> None:
    assert local_escalation.looks_like_edit_request("fix the bug in app.py")
    assert local_escalation.looks_like_edit_request("Add a median field")
    assert not local_escalation.looks_like_edit_request("what does git stash do?")
    assert not local_escalation.looks_like_edit_request("hey how's it going")


def test_situation_builder_compacts() -> None:
    s = local_escalation.build_situation(
        "fix it", [f"event {i}: " + "x" * 500 for i in range(20)], "stuck")
    assert len(s) <= 4000
    assert "event 19" in s, "most recent events must survive"
    assert "PROBLEM:" in s and "stuck" in s


def test_think_blocks_stripped_from_advice() -> None:
    esc = local_escalation.LocalEscalator({**_CFG})
    with unittest.mock.patch.object(esc, "ensure_server", return_value=True), \
         unittest.mock.patch.object(local_escalation.urllib.request, "urlopen") as mock_open:
        payload = {"choices": [{"message": {
            "content": "<think>long internal reasoning</think>Do X then Y."}}]}
        mock_resp = unittest.mock.MagicMock()
        mock_resp.read.return_value = json.dumps(payload).encode()
        mock_open.return_value.__enter__.return_value = mock_resp
        advice = esc.consult("situation")
    assert advice == "Do X then Y.", advice


TESTS = [
    test_loop_trip_consults_and_recovers,
    test_loop_trip_with_failed_consult_degrades_to_old_behavior,
    test_no_escalator_means_original_loop_stop,
    test_ignored_verify_nudge_consults,
    test_edit_request_finishing_without_mutation_consults,
    test_clarifying_question_is_exempt_from_trigger_c,
    test_non_edit_request_is_exempt_from_trigger_c,
    test_at_most_one_consult_per_turn,
    test_edit_intent_detection,
    test_situation_builder_compacts,
    test_think_blocks_stripped_from_advice,
]


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


def main() -> int:
    print(f"\nevals/test_escalation.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
