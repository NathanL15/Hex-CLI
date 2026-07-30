#!/usr/bin/env python3
"""evals/test_loop_v2.py — Offline integration tests for the v2 agent loop.

Drives hexcli.loop_v2 through the production entry point (run_autopilot with
config protocol="v2") against the mock backend, via the eval-v2 instrument.
Real tools execute in real sandboxes; only the LLM is scripted.

Usage:
    python evals/test_loop_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from evals import checks as ck  # noqa: E402
from evals.runner import Case, run_case_once  # noqa: E402
from hexcli.protocol_v2 import SYSTEM_PROMPT_V2  # noqa: E402

V2_CONFIG: dict[str, Any] = {
    **sa.DEFAULT_CONFIG,
    "backend": "mock",
    "model": "mock",
    "protocol": "v2",
    "memory_enabled": False,
    "telemetry_enabled": False,
}


def _tc(name: str, **args: Any) -> str:
    import json
    return f'<tool_call>\n{json.dumps({"name": name, "arguments": args})}\n</tool_call>'


# ---------------------------------------------------------------------------

def test_plain_text_finishes() -> None:
    sa.set_mock_responses(["The answer is 42."])
    case = Case("v2-final", "factual", "What is six times seven, please answer directly?",
                verify=ck.all_of(ck.no_tool_calls(), ck.message_contains("42")))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail
    assert out.trace.end_kind == "finish"
    assert out.trace.system_prompt.startswith(SYSTEM_PROMPT_V2), \
        "v2 system prompt must start with the byte-stable core"


def test_write_fence_payload_creates_file() -> None:
    sa.set_mock_responses([
        _tc("write", path="notes.txt") + "\n```\nhello world\nline two\n```",
        "Created notes.txt with the requested content.",
    ])
    case = Case("v2-write", "agentic", "Create the notes file please.",
                verify=ck.all_of(ck.file_contains("notes.txt", "hello world", "line two"),
                                 ck.tools_called("write")))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_edit_search_replace_applies() -> None:
    sa.set_mock_responses([
        _tc("edit", path="app.py")
        + "\n<<<<<<< SEARCH\n    return conut\n=======\n    return count\n>>>>>>> REPLACE",
        "Fixed the typo.",
    ])
    case = Case("v2-edit", "agentic", "Fix the typo in app dot py now.",
                setup={"app.py": "def f(count):\n    return conut\n"},
                verify=ck.all_of(ck.file_contains("app.py", "return count", ci=False),
                                 ck.tools_called("edit")))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail
    assert "conut" not in (out.trace.tool_calls[0].output or ""), "edit output should not error"


def test_malformed_gets_precise_feedback_and_recovers() -> None:
    sa.set_mock_responses([
        '<tool_call>{"name": "read", "arguments": {"path": "a.txt"}}',  # missing close tag
        _tc("read", path="a.txt"),
        "The file says: content here.",
    ])
    case = Case("v2-retry", "agentic", "Read the a file for me now.",
                setup={"a.txt": "content here"},
                verify=ck.all_of(ck.tools_called("read"), ck.message_contains("content")))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail
    assert out.trace.retries == 1, f"expected 1 format retry, got {out.trace.retries}"
    feedback = [m for m in out.trace.llm_calls if m.attempt == 1]
    assert feedback, "second attempt must exist"


def test_edit_no_match_error_feeds_back_and_model_corrects() -> None:
    sa.set_mock_responses([
        _tc("edit", path="app.py")
        + "\n<<<<<<< SEARCH\n    return WRONG\n=======\n    return count\n>>>>>>> REPLACE",
        _tc("edit", path="app.py")
        + "\n<<<<<<< SEARCH\n    return conut\n=======\n    return count\n>>>>>>> REPLACE",
        "Fixed on second attempt.",
    ])
    case = Case("v2-edit-recover", "agentic", "Fix the typo in app dot py now.",
                setup={"app.py": "def f(count):\n    return conut\n"},
                verify=ck.file_contains("app.py", "return count", ci=False))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail
    assert out.trace.tool_calls[0].status == "error"
    assert "closest region" in out.trace.tool_calls[0].output
    assert out.trace.tool_calls[1].status == "ok"


def test_unknown_tool_feedback_names_real_tools() -> None:
    sa.set_mock_responses([
        _tc("run_command", command="Get-ChildItem"),   # v1 tool name — must be rejected
        _tc("shell", command="Get-ChildItem"),
        "Listed the directory.",
    ])
    case = Case("v2-unknown-tool", "agentic", "List this directory contents now.",
                verify=ck.tools_called("shell"))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail
    assert out.trace.retries == 1


def test_shell_state_persists_across_steps() -> None:
    sa.set_mock_responses([
        _tc("shell", command='$env:HEX_LOOP_TEST = "carried"'),
        _tc("shell", command="Write-Output $env:HEX_LOOP_TEST"),
        "The variable carried over.",
    ])
    case = Case("v2-shell-persist", "agentic", "Set an env var then echo it back.",
                verify=lambda s, t: ("carried" in t.tool_calls[1].output,
                                     f"env var did not persist: {t.tool_calls[1].output!r}"))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_loop_detector_trips_on_repeated_failing_edit() -> None:
    bad = (_tc("edit", path="f.txt")
           + "\n<<<<<<< SEARCH\nNOPE\n=======\nx\n>>>>>>> REPLACE")
    sa.set_mock_responses([bad, bad, bad, "never reached"])
    case = Case("v2-loop", "agentic", "Edit the f file contents now.",
                setup={"f.txt": "content\n"},
                verify=lambda s, t: (t.end_kind == "loop_stop", f"end_kind={t.end_kind}"),
                max_steps=8)
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail
    assert len(out.trace.tool_calls) == 3


def test_repeated_successful_read_does_not_trip_detector() -> None:
    ok_read = _tc("read", path="a.txt")
    sa.set_mock_responses([ok_read, ok_read, ok_read, "Done reading."])
    case = Case("v2-no-false-loop", "agentic", "Read the a file three times please.",
                setup={"a.txt": "data"},
                verify=lambda s, t: (t.end_kind == "finish" and len(t.tool_calls) == 3,
                                     f"end={t.end_kind}, tools={len(t.tool_calls)}"))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_malformed_exhaustion_is_truthful() -> None:
    sa.set_mock_responses(["", "", ""])  # three empty responses
    case = Case("v2-malformed-exhaust", "agentic", "Do the thing with the file now.",
                verify=lambda s, t: (t.end_kind == "malformed" and "could not produce" in t.final_message,
                                     f"end={t.end_kind}, msg={t.final_message[:80]!r}"))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_tool_result_rendered_in_tool_response_tags() -> None:
    sa.set_mock_responses([
        _tc("read", path="a.txt"),
        "It says data.",
    ])
    captured: dict[str, Any] = {}
    orig = sa.call_llm

    def spy(config: dict[str, Any], messages: list[dict[str, str]], *a: Any, **k: Any):
        captured["messages"] = [dict(m) for m in messages]
        return orig(config, messages, *a, **k)

    sa.call_llm = spy
    try:
        case = Case("v2-render", "agentic", "Read the a file for me now.",
                    setup={"a.txt": "data"},
                    verify=lambda s, t: (True, ""))
        out = run_case_once(V2_CONFIG, case)
        assert out.ok, out.detail
        last_user = [m for m in captured["messages"] if m["role"] == "user"][-1]
        assert last_user["content"].startswith("<tool_response>"), last_user["content"][:80]
    finally:
        sa.call_llm = orig


def test_read_paging() -> None:
    big = "\n".join(f"line{i}" for i in range(1, 1001))
    sa.set_mock_responses([
        _tc("read", path="big.txt", offset=500, limit=3),
        "Read the middle.",
    ])
    case = Case("v2-read-page", "agentic", "Read the middle of the big file now.",
                setup={"big.txt": big},
                verify=lambda s, t: (
                    "line500" in t.tool_calls[0].output
                    and "line503" not in t.tool_calls[0].output
                    and "lines 500-502 of 1000" in t.tool_calls[0].output,
                    t.tool_calls[0].output[:120],
                ))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_verification_gate_nudges_after_unverified_edit() -> None:
    sa.set_mock_responses([
        _tc("edit", path="app.py")
        + "\n<<<<<<< SEARCH\n    return conut\n=======\n    return count\n>>>>>>> REPLACE",
        "Fixed the typo, all done!",                 # premature — gate must nudge
        _tc("shell", command="python app.py"),        # verification after the nudge
        "Verified: the script runs and prints 3.",
    ])
    case = Case("v2-verify-gate", "agentic", "Fix the typo in app dot py now.",
                setup={"app.py": "def f(count):\n    return conut\nprint(f(3) or 3)\n"},
                verify=lambda s, t: (
                    t.tools_used == ["edit", "shell"] and "Verified" in t.final_message,
                    f"tools={t.tools_used}, msg={t.final_message[:80]!r}",
                ))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_verification_gate_accepts_verified_finish() -> None:
    sa.set_mock_responses([
        _tc("edit", path="app.py")
        + "\n<<<<<<< SEARCH\n    return conut\n=======\n    return count\n>>>>>>> REPLACE",
        _tc("read", path="app.py"),
        "Fixed and confirmed by reading the file.",
    ])
    case = Case("v2-verify-pass", "agentic", "Fix the typo in app dot py now.",
                setup={"app.py": "def f(count):\n    return conut\n"},
                verify=lambda s, t: (
                    len(t.llm_calls) == 3 and t.end_kind == "finish",
                    f"calls={len(t.llm_calls)}, end={t.end_kind}",
                ))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


def test_verification_gate_nudges_only_once() -> None:
    # The model ignores the nudge and finishes again — the gate must accept
    # rather than loop forever.
    sa.set_mock_responses([
        _tc("write", path="new.txt") + "\n```\ncontent\n```",
        "Done!",
        "Really done!",
    ])
    case = Case("v2-verify-once", "agentic", "Create the new file for me now.",
                verify=lambda s, t: (
                    t.end_kind == "finish" and t.final_message == "Really done!",
                    f"end={t.end_kind}, msg={t.final_message!r}",
                ))
    out = run_case_once(V2_CONFIG, case)
    assert out.ok, out.detail


TESTS = [
    test_plain_text_finishes,
    test_write_fence_payload_creates_file,
    test_edit_search_replace_applies,
    test_malformed_gets_precise_feedback_and_recovers,
    test_edit_no_match_error_feeds_back_and_model_corrects,
    test_unknown_tool_feedback_names_real_tools,
    test_shell_state_persists_across_steps,
    test_loop_detector_trips_on_repeated_failing_edit,
    test_repeated_successful_read_does_not_trip_detector,
    test_malformed_exhaustion_is_truthful,
    test_tool_result_rendered_in_tool_response_tags,
    test_read_paging,
    test_verification_gate_nudges_after_unverified_edit,
    test_verification_gate_accepts_verified_finish,
    test_verification_gate_nudges_only_once,
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
        print(f"  ERROR {fn.__name__}: {exc!r}")
        return False


def main() -> int:
    print(f"\nevals/test_loop_v2.py — {len(TESTS)} integration tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
