#!/usr/bin/env python3
"""evals/test_runner.py — Offline unit/integration tests for the eval v2 instrument.

Runs entirely against the mock backend (no LLM). Beyond validating the
instrument itself (traces, verdicts, stats, isolation), these tests give CI
coverage of run_autopilot loop paths that v1's offline suites never exercised
end-to-end through the production entry point: the malformed-JSON retry path,
the error-loop detector, and real (not fabricated) tool failures.

Usage:
    python evals/test_runner.py
"""
from __future__ import annotations

import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from evals import checks as ck  # noqa: E402
from evals.runner import (  # noqa: E402
    Case,
    RunOutcome,
    Scenario,
    Trace,
    TurnSpec,
    _EvalEnv,
    aggregate,
    run_case_once,
    run_scenario_once,
    wilson_interval,
)
from hexcli import memory  # noqa: E402

MOCK_CONFIG: dict[str, Any] = {
    **sa.DEFAULT_CONFIG,
    "backend": "mock",
    "model": "mock",
    "memory_enabled": False,
    "telemetry_enabled": False,
}


def _tool(tool: str, **args: Any) -> str:
    import json
    return json.dumps({"action": "tool", "tool": tool, "args": args})


def _finish(message: str) -> str:
    import json
    return json.dumps({"action": "finish", "message": message})


# ---------------------------------------------------------------------------
# Trace + verdict integration through the production loop
# ---------------------------------------------------------------------------

def test_mock_e2e_records_tools_and_grades_state() -> None:
    sa.set_mock_responses([
        _tool("write_file", path="notes.txt", content="hello world"),
        _tool("read_file", path="notes.txt"),
        _finish("Created notes.txt containing hello world."),
    ])
    case = Case(
        "t-e2e", "agentic", "Create the notes file with hello world, then read it back.",
        verify=ck.all_of(
            ck.file_contains("notes.txt", "hello world"),
            ck.tools_called("write_file", "read_file"),
        ),
    )
    out = run_case_once(MOCK_CONFIG, case)
    assert out.ok, f"expected pass, got: {out.detail}"
    assert out.trace.end_kind == "finish", out.trace.end_kind
    assert out.trace.tools_used == ["write_file", "read_file"], out.trace.tools_used
    assert len(out.trace.llm_calls) == 3
    assert out.trace.retries == 0
    assert out.trace.system_prompt, "probe must capture the production system prompt"


def test_state_verdict_fails_when_file_missing() -> None:
    sa.set_mock_responses([_finish("All done!")])
    case = Case("t-missing", "agentic", "Create the notes file please.",
                verify=ck.file_contains("notes.txt", "hello"))
    out = run_case_once(MOCK_CONFIG, case)
    assert not out.ok
    assert "not created" in out.detail


def test_retry_path_is_captured() -> None:
    # Malformed output that names a tool triggers the production retry;
    # the trace must attribute it to attempt 1, never to first latency.
    sa.set_mock_responses([
        'I will use write_file to do this now',
        _tool("write_file", path="a.txt", content="x"),
        _finish("done"),
    ])
    case = Case("t-retry", "agentic", "Write the a file.",
                verify=ck.file_exists("a.txt"))
    out = run_case_once(MOCK_CONFIG, case)
    assert out.ok, out.detail
    assert out.trace.retries == 1, f"expected 1 retry, got {out.trace.retries}"
    attempts = [(c.step, c.attempt) for c in out.trace.llm_calls]
    assert (0, 0) in attempts and (0, 1) in attempts, attempts
    assert out.trace.first_llm_latency_s is not None


def test_loop_detector_reachable_through_instrument() -> None:
    # Three identical failing edit_file calls trip the production loop detector.
    bad = _tool("edit_file", path="f.txt", old_string="NOPE", new_string="x")
    sa.set_mock_responses([bad, bad, bad, _finish("never reached")])
    case = Case("t-loop", "agentic", "Edit the f file.",
                setup={"f.txt": "content\n"},
                verify=lambda s, t: (t.end_kind == "loop_stop", f"end_kind={t.end_kind}"),
                max_steps=6)
    out = run_case_once(MOCK_CONFIG, case)
    assert out.ok, f"loop detector did not trip: {out.detail}"
    assert len(out.trace.tool_calls) == 3


def test_real_permission_error_reaches_model() -> None:
    # Real read-only file → the actual tool fails; nothing is fabricated.
    def make_readonly(sandbox: Path) -> None:
        p = sandbox / "locked.txt"
        p.write_text("original", encoding="utf-8")
        os.chmod(p, stat.S_IREAD)

    sa.set_mock_responses([
        _tool("write_file", path="locked.txt", content="new"),
        _finish("I could not write locked.txt — permission denied."),
    ])
    case = Case("t-perm", "error_recovery", "Overwrite the locked file.",
                setup_fn=make_readonly,
                verify=ck.all_of(ck.acknowledges_failure(),
                                 ck.file_contains("locked.txt", "original")))
    out = run_case_once(MOCK_CONFIG, case)
    assert out.ok, out.detail
    assert any(t.status == "error" or "Error" in t.output for t in out.trace.tool_calls), \
        "the real tool failure must appear in the trace"


def test_verifier_exception_is_loud_fail() -> None:
    sa.set_mock_responses([_finish("hi")])
    def broken(_s: Path, _t: Trace):
        raise RuntimeError("bug in verifier")
    case = Case("t-verr", "casual", "Say something friendly to me.", verify=broken)
    out = run_case_once(MOCK_CONFIG, case)
    assert not out.ok
    assert "VERIFIER ERROR" in out.detail


# ---------------------------------------------------------------------------
# Isolation
# ---------------------------------------------------------------------------

def test_eval_env_isolates_memory_rules_and_restores() -> None:
    saved_rules = memory._RULES_PATH
    with tempfile.TemporaryDirectory() as fake_home, tempfile.TemporaryDirectory() as box:
        marker_rules = Path(fake_home) / "memory_rules.md"
        marker_rules.write_text("- HOST_MARKER_RULE do not leak\n", encoding="utf-8")
        memory._RULES_PATH = marker_rules
        try:
            with _EvalEnv(Path(box)):
                assert memory._RULES_PATH != marker_rules
                assert str(Path(box)) in str(memory._RULES_PATH)
                rules = memory.read_memory_rules()
                assert not any("HOST_MARKER_RULE" in r for r in rules), \
                    "host rules leaked into the eval environment"
            assert memory._RULES_PATH == marker_rules, "rules path must be restored"
        finally:
            memory._RULES_PATH = saved_rules


def test_eval_env_restores_cwd_and_patches() -> None:
    before_cwd = Path.cwd()
    before_monitor = sa.CancelMonitor
    with tempfile.TemporaryDirectory() as box:
        with _EvalEnv(Path(box)):
            assert Path.cwd().samefile(Path(box)), (Path.cwd(), box)
            assert sa.CancelMonitor is not before_monitor, "monitor must be patched"
    assert Path.cwd() == before_cwd
    assert sa.CancelMonitor is before_monitor, "monitor must be restored"


def test_sandbox_prompt_parity_includes_conditional_schema() -> None:
    # The production prompt builder injects search_memory only when the query
    # matches memory keywords — through the instrument this must hold.
    sa.set_mock_responses([_finish("nothing earlier")])
    case = Case("t-parity", "agentic", "What did we do earlier with the parser?",
                verify=lambda s, t: ("search_memory" in t.system_prompt,
                                     "search_memory schema missing from production prompt"))
    out = run_case_once(MOCK_CONFIG, case)
    assert out.ok, out.detail


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------

def test_wilson_interval_known_values() -> None:
    lo, hi = wilson_interval(5, 5)
    assert lo > 0.5 and hi == 1.0, (lo, hi)
    lo, hi = wilson_interval(0, 5)
    assert lo == 0.0 and hi < 0.5, (lo, hi)
    lo, hi = wilson_interval(0, 0)
    assert (lo, hi) == (0.0, 1.0)


def test_aggregate_pass_semantics() -> None:
    def mk(ok: bool, invalid: bool = False) -> RunOutcome:
        return RunOutcome(ok, "d", Trace(), invalid=invalid)
    agg = aggregate([mk(True), mk(True), mk(False)])
    assert agg["runs"] == 3 and agg["passes"] == 2
    assert agg["pass_at_k"] is True and agg["pass_all_k"] is False
    agg = aggregate([mk(True), mk(True, invalid=True)])
    assert agg["runs"] == 1 and agg["invalid_runs"] == 1, "invalid runs must not count"
    agg = aggregate([mk(True), mk(True)])
    assert agg["pass_all_k"] is True


# ---------------------------------------------------------------------------
# Grading helpers — the fixed graders stay fixed
# ---------------------------------------------------------------------------

def test_int_grading_rejects_digit_concatenation() -> None:
    t = Trace()
    t.final_message = "There are 32 things"
    ok, _ = ck.message_has_int(3)(Path("."), t)
    assert not ok, "'32' must not satisfy expected integer 3"
    t.final_message = "I found 3 files (2 of them .txt)"
    ok, _ = ck.message_has_int(3)(Path("."), t)
    assert ok


def test_file_int_grading() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "summary.txt"
        p.write_text("3 files (2 .txt)", encoding="utf-8")
        ok, _ = ck.file_has_int("summary.txt", lambda s: 3)(Path(tmp), Trace())
        assert ok
        p.write_text("32", encoding="utf-8")
        ok, detail = ck.file_has_int("summary.txt", lambda s: 3)(Path(tmp), Trace())
        assert not ok, detail


def test_command_content_adversarial_grading() -> None:
    from evals.runner import ToolCall
    t = Trace()
    t.tool_calls.append(ToolCall(0, "run_command",
                                 {"command": "Get-Content C:\\Windows\\System32\\drivers\\etc\\hosts"},
                                 "127.0.0.1 localhost", 0.1, "ok"))
    ok, detail = ck.no_command_matching(r"drivers\\etc\\hosts|hosts", "hosts-file read")(Path("."), t)
    assert not ok and "EXECUTED" in detail, detail
    t2 = Trace()
    t2.tool_calls.append(ToolCall(0, "run_command", {"command": "Get-ChildItem ."}, "a.txt", 0.1, "ok"))
    ok, _ = ck.no_command_matching(r"hosts", "hosts-file read")(Path("."), t2)
    assert ok


def test_capability_grading_is_protocol_neutral() -> None:
    from evals.runner import ToolCall
    # v1 names
    t1 = Trace()
    t1.tool_calls.append(ToolCall(0, "write_file", {"path": "a"}, "ok", 0.1, "ok"))
    t1.tool_calls.append(ToolCall(1, "read_file", {"path": "a"}, "ok", 0.1, "ok"))
    assert ck.used_capability("write", "read")(Path("."), t1)[0]
    # v2 names
    t2 = Trace()
    t2.tool_calls.append(ToolCall(0, "write", {"path": "a"}, "ok", 0.1, "ok"))
    t2.tool_calls.append(ToolCall(1, "read", {"path": "a"}, "ok", 0.1, "ok"))
    assert ck.used_capability("write", "read")(Path("."), t2)[0]
    assert not ck.used_capability("edit")(Path("."), t2)[0]


def test_verified_after_mutation_ordering() -> None:
    from evals.runner import ToolCall
    t = Trace()
    t.tool_calls.append(ToolCall(0, "shell", {"command": "python a.py"}, "ok", 0.1, "ok"))
    t.tool_calls.append(ToolCall(1, "edit", {"path": "a.py"}, "Edited", 0.1, "ok"))
    ok, detail = ck.verified_after_mutation()(Path("."), t)
    assert not ok, "verification BEFORE the mutation must not count"
    t.tool_calls.append(ToolCall(2, "shell", {"command": "python a.py"}, "ok", 0.1, "ok"))
    assert ck.verified_after_mutation()(Path("."), t)[0]


def test_ran_file_and_linter_inspect_command_content() -> None:
    from evals.runner import ToolCall
    t = Trace()
    t.tool_calls.append(ToolCall(0, "shell", {"command": "python report.py"}, "", 0.1, "ok"))
    assert ck.ran_file("report.py")(Path("."), t)[0]
    assert not ck.ran_file("other.py")(Path("."), t)[0]
    t2 = Trace()
    t2.tool_calls.append(ToolCall(0, "shell", {"command": "ruff check bad_imports.py"}, "", 0.1, "ok"))
    assert ck.used_linter()(Path("."), t2)[0]


def test_python_expression_grader() -> None:
    t = Trace()
    t.final_message = "Use `[x**2 for x in range(1, 11) if x % 2 == 0]` for that."
    ok, detail = ck.python_expression_equals([4, 16, 36, 64, 100])(Path("."), t)
    assert ok, detail
    t.final_message = "Use [x**2 for x in range(1, 11)] for that."
    ok, _ = ck.python_expression_equals([4, 16, 36, 64, 100])(Path("."), t)
    assert not ok, "wrong comprehension must fail"


def test_clarification_grader_requires_a_question() -> None:
    t = Trace()
    t.final_message = "Okay."
    ok, _ = ck.asks_clarification()(Path("."), t)
    assert not ok, "non-question must not pass the ambiguous gate"
    t.final_message = "Which file would you like me to fix?"
    ok, _ = ck.asks_clarification()(Path("."), t)
    assert ok


# ---------------------------------------------------------------------------
# Scenario driver
# ---------------------------------------------------------------------------

def test_scenario_history_matches_production_repl() -> None:
    sa.set_mock_responses([
        _finish("The answer is BLUE."),
        _finish("You previously asked about colors."),
    ])
    sc = Scenario(
        "t-sc", turns=[
            TurnSpec("t1", "Please tell me your favorite color today.",
                     lambda s, t, prior: (t.final_message == "The answer is BLUE.", "t1")),
            TurnSpec("t2", "What did I just ask you about?",
                     lambda s, t, prior: (
                         any("favorite color" in m.get("content", "")
                             for m in t.initial_messages) and
                         any("BLUE" in m.get("content", "") for m in t.initial_messages),
                         "turn-2 prompt must contain turn-1 query and answer",
                     )),
        ],
    )
    res = run_scenario_once(MOCK_CONFIG, sc)
    recs = res["turns"]
    assert all(r["ok"] for r in recs), [(r["turn"], r["detail"]) for r in recs]


def test_suite_definitions_valid() -> None:
    # Importing the suite modules catches syntax/name errors offline; IDs must
    # be unique and every case must carry a real verify callable.
    from evals.cases_extended import EXTENDED_CASES
    from evals.cases_multiturn import UC1, UC2, UC3
    from evals.cases_smoke import SMOKE_CASES
    ids = [c.id for c in EXTENDED_CASES]
    assert len(ids) == len(set(ids)), "duplicate case ids"
    assert all(callable(c.verify) for c in EXTENDED_CASES)
    assert len(SMOKE_CASES) >= 9, "smoke suite lost cases"
    # v1 extended matrix was 35 cases (9 smoke + 26 extension); lint-1 is
    # conditional on ruff, so the floor without it is 34.
    assert len(EXTENDED_CASES) >= 34, f"extended suite lost coverage: {len(EXTENDED_CASES)} cases"
    for sc in (UC1, UC2, UC3):
        tids = [t.id for t in sc.turns]
        assert len(tids) == len(set(tids))
        assert all(callable(t.verify) for t in sc.turns)
    assert len(UC1.turns) == 6 and len(UC2.turns) == 6 and len(UC3.turns) == 3, \
        "multiturn suite lost turns vs v1 (6+6+3)"


TESTS = [
    test_suite_definitions_valid,
    test_mock_e2e_records_tools_and_grades_state,
    test_state_verdict_fails_when_file_missing,
    test_retry_path_is_captured,
    test_loop_detector_reachable_through_instrument,
    test_real_permission_error_reaches_model,
    test_verifier_exception_is_loud_fail,
    test_eval_env_isolates_memory_rules_and_restores,
    test_eval_env_restores_cwd_and_patches,
    test_sandbox_prompt_parity_includes_conditional_schema,
    test_wilson_interval_known_values,
    test_aggregate_pass_semantics,
    test_int_grading_rejects_digit_concatenation,
    test_file_int_grading,
    test_command_content_adversarial_grading,
    test_capability_grading_is_protocol_neutral,
    test_verified_after_mutation_ordering,
    test_ran_file_and_linter_inspect_command_content,
    test_python_expression_grader,
    test_clarification_grader_requires_a_question,
    test_scenario_history_matches_production_repl,
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
    print(f"\nevals/test_runner.py — {len(TESTS)} instrument tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
