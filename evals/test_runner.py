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
import unittest.mock
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402

# Offline suites must never wait on a human at a consent prompt.
sa.ui.CONFIRM_TIMEOUT_S = 0.05
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
                # The fake home must sit OUTSIDE the sandbox so it never shows
                # up in the model's own directory listings.
                assert str(Path(box)) not in str(memory._RULES_PATH)
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

def test_backend_failures_are_invalid_not_model_failures() -> None:
    import urllib.error

    from evals.runner import is_backend_failure
    assert is_backend_failure(urllib.error.HTTPError("u", 500, "ISE", {}, None))
    assert is_backend_failure(urllib.error.URLError("refused"))
    assert is_backend_failure(ConnectionResetError())
    assert is_backend_failure(RuntimeError("Genie status -6 (ERROR_QUERY_FAILED)"))
    # A 4xx is a client bug and a plain exception is a real failure — neither
    # may be excused as a backend problem.
    assert is_backend_failure(urllib.error.HTTPError("u", 400, "Bad", {}, None)) is None
    assert is_backend_failure(ValueError("bad args")) is None


def _trace(final: str, tools: list[str] | None = None) -> Any:
    """A Trace with just the fields the graders read."""
    from evals.runner import ToolCall, Trace
    t = Trace()
    t.final_message = final
    t.tool_calls = [ToolCall(step=i, tool=name, args={}, output="ok", latency_s=0.0, status="ok")
                    for i, name in enumerate(tools or [])]
    return t


def test_graders_for_the_find_a_file_case() -> None:
    """From the owner's 2026-09-15 17:53 session: "find my current resume" ran
    Get-Date and finished "No resume was found in the workspace directory"."""
    from evals import checks as ck

    real = "The current date is September 15, 2026. No resume was found in the workspace directory."
    assert ck.claims_nothing_found()(Path("."), _trace(real))[0]
    # The inverted form is what the case actually uses.
    negative = ck.not_(ck.claims_nothing_found(), "Claimed nothing was found")
    assert not negative(Path("."), _trace(real))[0]
    good = "The notes are at work/archive/project-notes.md."
    assert negative(Path("."), _trace(good))[0]
    assert ck.answer_names_path("archive", "project-notes")(Path("."), _trace(good))[0]
    # Separator-agnostic and case-insensitive.
    assert ck.answer_names_path("archive", "project-notes")(
        Path("."), _trace(r"Found it: WORK\ARCHIVE\Project-Notes.md"))[0]
    assert not ck.answer_names_path("archive")(Path("."), _trace("It is in work/readme.txt"))[0]
    # A search-class tool is required; Get-Date (run_command) is not one.
    searched = ck.any_of(ck.used_capability("list"), ck.used_capability("search"))
    assert not searched(Path("."), _trace(real, ["run_command"]))[0]
    assert searched(Path("."), _trace(good, ["find_files"]))[0]
    assert searched(Path("."), _trace(good, ["search_files"]))[0]
    # A plain statement of fact must not read as a negative claim.
    assert not ck.claims_nothing_found()(
        Path("."), _trace("The file contains four functions and a main block."))[0]


_SERVER_LOG_SAMPLE = """\
2026-09-15T02:10:01.001Z  INFO npurun_server::openai: chat completion request model=qwen3-4b
2026-09-15T02:10:02.002Z  INFO npurun_server::openai: REWIND continuation: skipping prefill
2026-09-15T02:10:07.003Z  WARN npurun_core::engine: Rewind query failed; recreating dialog
2026-09-15T02:10:09.004Z  WARN npurun_server::openai: inference slot busy, returning 503
2026-09-15T02:10:11.005Z  INFO npurun_server::openai: chat completion request model=qwen3-4b
"""


def _arm(cases: dict[str, tuple[int, int]]) -> dict[str, Any]:
    """A results payload with just the fields the gate reads."""
    return {"cases": {cid: {"passes": k, "runs": n, "pass_all_k": k == n}
                      for cid, (k, n) in cases.items()}}


def test_a_pinned_gate_set_replaces_the_luck_filter() -> None:
    """Membership by "3/3 in every baseline" is a filter on luck and it also
    depends on which baselines the operator passed — measured 2026-09-16, the
    pairs in use gave 27, 29, 31 and 32 cases. A pinned file states the
    membership and the evidence, so changing it is a reviewable commit."""
    from evals.gate import gate_sets

    baselines = [_arm({"lucky-1": (3, 3), "solid-1": (3, 3), "panel-1": (2, 3)}),
                 _arm({"lucky-1": (5, 5), "solid-1": (5, 5), "panel-1": (3, 5)})]

    # Without a pinned set, the historical rule stands: both 3/3 cases gate.
    gate, ceiling = gate_sets(*[b["cases"] for b in baselines], pinned=None)
    assert gate == ["lucky-1", "solid-1"], gate
    assert ceiling == ["panel-1"], ceiling

    # With one, membership is exactly what the file says, and everything else
    # valid in the first baseline is reported as ceiling rather than gated.
    pinned = {"cases": {"solid-1": {"passes": 15, "runs": 15}}}
    gate, ceiling = gate_sets(*[b["cases"] for b in baselines], pinned=pinned)
    assert gate == ["solid-1"], gate
    assert ceiling == ["lucky-1", "panel-1"], ceiling


def test_load_pinned_set_is_absent_until_someone_adopts_one() -> None:
    """No file, or a malformed one, must leave the gate exactly as it was —
    adopting a measured set is a decision, not something that happens because
    a file appeared."""
    from evals.gate import load_pinned_set

    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "gate_set.json"
        assert load_pinned_set(missing) is None

        missing.write_text("{not json", encoding="utf-8")
        assert load_pinned_set(missing) is None

        missing.write_text('{"note": "no cases key"}', encoding="utf-8")
        assert load_pinned_set(missing) is None

        missing.write_text('{"cases": {"a": {"passes": 15, "runs": 15}}}', encoding="utf-8")
        assert load_pinned_set(missing)["cases"] == {"a": {"passes": 15, "runs": 15}}


def test_propose_gate_set_admits_only_what_never_missed() -> None:
    """The bar is a perfect record because the rule the gate applies is a
    perfect score: a case that cannot hold 15/15 on unchanged code cannot
    carry a binary verdict."""
    from evals.gate import propose_gate_set

    arms = [_arm({"solid-1": (8, 8), "flaky-1": (7, 8), "thin-1": (4, 4)}),
            _arm({"solid-1": (7, 7), "flaky-1": (8, 8), "thin-1": (3, 3)})]
    proposal = propose_gate_set(arms, min_runs=12)

    # solid-1 pooled 15/15 -> gated. flaky-1 missed once, so no.
    assert set(proposal["cases"]) == {"solid-1"}, proposal["cases"]
    assert proposal["cases"]["solid-1"] == {"passes": 15, "runs": 15}
    # thin-1 never missed but has only 7 runs: not enough evidence to gate on.
    assert "thin-1" in proposal["not_gated"] and "flaky-1" in proposal["not_gated"]
    # Rejections carry their numbers, so the file explains its own membership.
    assert proposal["not_gated"]["flaky-1"] == {"passes": 15, "runs": 16}


def test_a_chunk_file_carries_the_same_identity_as_a_whole_suite_run() -> None:
    """A recheck is merged INTO an arm and then gated against it, so the two
    files have to be comparable. `temperature` was missing from chunk files
    until 2026-09-16: run_suite_cli wrote it, run_chunk did not, and every
    control run this driver created had a blank where the comparison rule
    expects a value."""
    from evals.run_chunk import IDENTITY_FIELDS, seed_identity

    config = {"model": "qwen3-4b", "temperature": 0.1}
    payload = seed_identity({}, config, 6, {}, 42)
    for field in IDENTITY_FIELDS:
        assert field in payload, field
    assert payload["temperature"] == 0.1, payload
    assert payload["runs_per_case"] == 6 and payload["seed"] == 42, payload

    # The first chunk owns the identity; a later chunk joins it and must not
    # rewrite it, or a merged file would describe conditions it never ran in.
    joined = seed_identity({"temperature": 0.7, "seed": 1, "model": "other"},
                           config, 3, {}, 99)
    assert (joined["temperature"], joined["seed"], joined["model"]) == (0.7, 1, "other"), joined


def test_case_reliability_matches_the_binomial() -> None:
    from evals.gate import case_reliability

    # A case that never misses is never rechecked and never broken.
    assert case_reliability(1.0) == (0.0, 0.0)

    # A 90% case: 1 - 0.9^5 = 41.0% rechecked, and once rechecked it survives
    # six runs 88.6% of the time, so 4.7% of no-op candidates lose it.
    p_recheck, p_broken = case_reliability(0.9)
    assert abs(p_recheck - 0.40951) < 1e-5, p_recheck
    assert abs(p_broken - 0.046793) < 1e-5, p_broken

    # Reliability is monotonic: a worse case is rechecked and broken more.
    rates = [0.99, 0.95, 0.9, 0.85, 0.8]
    rechecks = [case_reliability(r)[0] for r in rates]
    brokens = [case_reliability(r)[1] for r in rates]
    assert rechecks == sorted(rechecks), rechecks
    assert brokens == sorted(brokens), brokens


def test_calibrate_reports_what_the_rule_does_to_a_no_op_candidate() -> None:
    """The gate set is chosen by '3/3 in every baseline', which admits a case
    that was merely lucky; calibrate estimates the rate from other arms."""
    from evals.gate import calibrate

    # Two baselines agree that both cases are perfect, so both are gated.
    baselines = [_arm({"solid-1": (3, 3), "flaky-1": (3, 3)}),
                 _arm({"solid-1": (5, 5), "flaky-1": (5, 5)})]
    # Other arms tell the truth: one case is reliable, the other is not.
    arms = [_arm({"solid-1": (10, 10), "flaky-1": (8, 10)}),
            _arm({"solid-1": (10, 10), "flaky-1": (9, 10)})]
    rep = calibrate(baselines, arms)

    assert rep["gate_size"] == 2 and rep["scored"] == 2, rep
    by_case = {r["case"]: r for r in rep["rows"]}
    assert by_case["solid-1"]["rate"] > 0.95, by_case["solid-1"]
    assert 0.75 < by_case["flaky-1"]["rate"] < 0.9, by_case["flaky-1"]
    # The flaky case carries essentially all of the false-failure risk.
    assert by_case["flaky-1"]["p_broken"] > 20 * by_case["solid-1"]["p_broken"]
    # A perfect record still is not read as a certain 100%.
    assert by_case["solid-1"]["rate"] < 1.0, by_case["solid-1"]
    assert 0.0 < rep["p_clean_pass"] < 1.0 and 0.0 < rep["p_false_fail"] < 1.0, rep
    # Sanity: dropping the flaky case makes the gate far less trigger-happy.
    rep2 = calibrate([_arm({"solid-1": (3, 3)}), _arm({"solid-1": (5, 5)})], arms)
    assert rep2["p_false_fail"] < rep["p_false_fail"] / 10, (rep2, rep)


def test_calibrate_will_not_score_a_case_it_has_barely_seen() -> None:
    from evals.gate import calibrate

    baselines = [_arm({"rare-1": (3, 3)}), _arm({"rare-1": (5, 5)})]
    rep = calibrate(baselines, [_arm({"rare-1": (3, 3)})])
    assert rep["unscored"] == ["rare-1"], rep
    assert rep["scored"] == 0, rep
    # With nothing scored the report must not claim a probability of anything.
    assert rep["p_clean_pass"] == 1.0 and rep["p_false_fail"] == 0.0, rep


def test_platform_reading_counts_only_this_suites_traffic() -> None:
    """Invalid runs are a reading of the machine. The 2026-09-15 arm lost 23
    of 205 runs with 67 Rewind failures in 509 requests behind them, and the
    verdict was only readable with those numbers beside it."""
    from evals import runner

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp).resolve() / "npurun_server.log"
        log.write_text("older traffic, not ours\n", encoding="utf-8")
        with unittest.mock.patch.object(runner.paths, "npurun_log_path", return_value=log):
            offset = runner._server_log_size()
            with log.open("a", encoding="utf-8") as fh:
                fh.write(_SERVER_LOG_SAMPLE)
            reading = runner.platform_reading(offset)
    assert reading["requests"] == 2, reading
    assert reading["rewind_failures"] == 1, reading
    assert reading["slot_busy"] == 1, reading
    assert reading["rewind_failure_rate"] == 0.5, reading


def test_platform_reading_survives_a_truncated_or_missing_log() -> None:
    from evals import runner

    with tempfile.TemporaryDirectory() as tmp:
        log = Path(tmp).resolve() / "npurun_server.log"
        with unittest.mock.patch.object(runner.paths, "npurun_log_path", return_value=log):
            assert runner._server_log_size() == 0            # no log at all
            assert runner.platform_reading(0) == {}
            # A server restart truncates the log mid-suite: read what is there
            # rather than seeking past the end and reporting nothing.
            log.write_text(_SERVER_LOG_SAMPLE, encoding="utf-8")
            assert runner.platform_reading(10_000)["requests"] == 2
            # A log with none of our markers is not a reading.
            log.write_text("DSP_INFO UNSUPPORTED_KEY: 1\n", encoding="utf-8")
            assert runner.platform_reading(0) == {}


def test_describe_platform_reads_as_one_line() -> None:
    from evals import runner

    line = runner.describe_platform(
        {"requests": 509, "rewind_failures": 67, "slot_busy": 225,
         "rewind_failure_rate": 0.132}, 23, 205)
    assert line == ("23 invalid of 205 runs; 67 Rewind failures in 509 requests (13%); "
                    "225 busy-slot retries"), line
    assert runner.describe_platform({}, 0, 0) == ""
    assert runner.describe_platform({}, 0, 40) == "0 invalid of 40 runs"


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


def test_clarification_grader_rejects_empty_completions_and_bare_question_marks() -> None:
    """Council review 2026-09-04: "Done. Let me know if you need anything
    else." and "Should I proceed? Done." both passed. A question must be
    aimed at the user (wh-word or auxiliary + you/I in the sentence), and a
    message that opens with a completion claim is not a question."""
    g = ck.asks_clarification()
    for msg in ("Done. Let me know if you need anything else.",
                "Done.",
                "All set! Anything else?",
                "Completed the task. Should I proceed?",
                "No action could be taken.",
                "Sure! Here is the improved version?"):
        t = Trace()
        t.final_message = msg
        ok, why = g(Path("."), t)
        assert not ok, f"{msg!r} must not count as asking: {why}"
    for msg in ("Which file should I update?",
                "Could you tell me what 'it' refers to?",
                "Please describe the code you want fixed.",
                "Let me know which file you mean.",
                "What exactly should be improved — performance or readability?",
                "Do you want me to change config.py or utils.py?",
                "I am unable to proceed. If you can clarify what needs improvement (e.g., files), I will assist."):
        t = Trace()
        t.final_message = msg
        ok, why = g(Path("."), t)
        assert ok, f"{msg!r} is a real question: {why}"


def test_grounded_answer_grader() -> None:
    """bigfile-1 must name what it read and nothing it did not."""
    from evals.runner import ToolCall
    g = ck.answer_grounded_in_tool_output(["alpha", "omega", "filler", "filler_01", "pipeline"])
    page = "def alpha():\n    return 1\n\ndef filler_01(x):\n    # step 1 of the pipeline\n"
    t = Trace()
    t.tool_calls = [ToolCall(0, "read_file", {"path": "big_module.py"}, page, 0.01, "ok")]
    t.final_message = "It defines alpha and a series of filler functions forming a pipeline."
    ok, why = g(Path("."), t)
    assert ok, why
    t.final_message = "It defines alpha, filler functions and finally omega."
    ok, why = g(Path("."), t)
    assert not ok and "omega" in why, "omega was never read: confabulation must fail"
    t.final_message = "It is a Python module with several functions."
    ok, why = g(Path("."), t)
    assert not ok and "nothing" in why


def test_live_state_patterns_are_word_bounded() -> None:
    g = ck.answer_matches([r"\b(snapdragon|oryon|qualcomm|arm|x1e)\b"], [r"\b(intel|ryzen|core i[3579])\b"])
    t = Trace()
    t.final_message = "The room is warm and the alarm is off."
    assert not g(Path("."), t)[0], "'warm'/'alarm' must not match 'arm'"
    t.final_message = "This is a Snapdragon X Elite (ARM) machine."
    assert g(Path("."), t)[0]


def test_stats_exact_tests_known_values() -> None:
    from evals.stats import compare_arms, fisher_exact_two_sided, mcnemar_exact
    assert abs(fisher_exact_two_sided(3, 4, 1, 4) - 0.4857) < 1e-3, "2x2 [[3,1],[1,3]]"
    assert fisher_exact_two_sided(4, 4, 4, 4) == 1.0
    assert abs(fisher_exact_two_sided(10, 10, 0, 10) - 1.083e-5) < 1e-6, "10/10 vs 0/10"
    assert mcnemar_exact(0, 0) == 1.0
    assert abs(mcnemar_exact(5, 0) - 0.0625) < 1e-9
    assert abs(mcnemar_exact(3, 3) - 1.0) < 1e-9
    a = {"x": {"runs": 3, "passes": 3, "pass_all_k": True}, "y": {"runs": 3, "passes": 1, "pass_all_k": False},
         "z": {"runs": 0, "passes": 0, "pass_all_k": None}}
    b = {"x": {"runs": 3, "passes": 2, "pass_all_k": False}, "y": {"runs": 3, "passes": 3, "pass_all_k": True},
         "z": {"runs": 3, "passes": 3, "pass_all_k": True}}
    c = compare_arms(a, b)
    assert c["valid_cases"] == 2 and c["run_level"]["a"] == [4, 6] and c["run_level"]["b"] == [5, 6]
    assert c["pass_all_k"]["a_only"] == ["x"] and c["pass_all_k"]["b_only"] == ["y"]
    assert set(c["per_case"]) == {"x", "y"}


def test_gate_is_binary_on_reliable_cases_and_tracks_the_rest() -> None:
    from evals.gate import case_status, evaluate, gate_sets
    base = {"cases": {
        "solid": {"runs": 3, "passes": 3, "pass_all_k": True},
        "lucky": {"runs": 3, "passes": 3, "pass_all_k": True},
        "flaky": {"runs": 3, "passes": 1, "pass_all_k": False},
        "dead": {"runs": 3, "passes": 0, "pass_all_k": False},
    }}
    base2 = {"cases": {"solid": {"runs": 3, "passes": 3, "pass_all_k": True},
                       "lucky": {"runs": 3, "passes": 2, "pass_all_k": False}}}
    assert gate_sets(base["cases"]) == (["lucky", "solid"], ["dead", "flaky"])
    assert gate_sets(base["cases"], base2["cases"]) == (["solid"], ["dead", "flaky", "lucky"]),         "two baselines: only cases 3/3 in BOTH are gate cases"
    good = {"cases": {"solid": {"runs": 3, "passes": 3, "pass_all_k": True},
                      "flaky": {"runs": 3, "passes": 0, "pass_all_k": False},
                      "dead": {"runs": 3, "passes": 2, "pass_all_k": False}}}
    rep = evaluate([base, base2], good)
    assert rep["verdict"] == "PASS", "a flaky case getting worse is tracked, not gated"
    assert rep["ceiling_lost"] == ["flaky"] and rep["ceiling_gained"] == ["dead"]
    once = {"cases": {"solid": {"runs": 3, "passes": 2, "pass_all_k": False}},
            "canary_s": {"start": 1.0, "end": 3.0}}
    rep = evaluate([base, base2], once)
    assert rep["verdict"] == "RECHECK" and rep["recheck"] == ["solid"], "one miss at 3 runs is a recheck"
    assert rep["server_drift"] and "3.0" in rep["server_drift"]
    assert case_status({"runs": 6, "passes": 5}) == "ok", "one miss in six is tolerated"
    assert case_status({"runs": 6, "passes": 4}) == "broken"
    rep = evaluate([base], {"cases": {"solid": {"runs": 6, "passes": 4, "pass_all_k": False}}})
    assert rep["verdict"] == "FAIL" and rep["broken"] == ["solid"]


def test_scenario_think_time_pauses_between_turns_only() -> None:
    """--think-time sleeps between turns, never after the last one."""
    import time as _time
    from unittest import mock
    sa.set_mock_responses([
        '{"action":"finish","message":"one"}',
        '{"action":"finish","message":"two"}',
    ])
    cfg = {**sa.DEFAULT_CONFIG, "backend": "mock", "memory_enabled": False, "telemetry_enabled": False}
    sc = Scenario("tt", turns=[
        TurnSpec("t1", "say one", lambda s, t, p: (True, "")),
        TurnSpec("t2", "say two", lambda s, t, p: (True, "")),
    ])
    with mock.patch.object(_time, "sleep") as slept:
        run_scenario_once(cfg, sc, think_time_s=7.5)
    assert slept.call_args_list == [mock.call(7.5)], slept.call_args_list


# ---------------------------------------------------------------------------
# Scenario driver
# ---------------------------------------------------------------------------

def test_scenario_driver_applies_auto_compaction() -> None:
    """The scenario driver must run the REPL's auto-compact step, or the eval
    silently diverges from production on exactly the long sessions it tests."""
    long_answer = "detail " * 300  # ~2100 chars per turn, forces the budget
    sa.set_mock_responses([_finish(long_answer)] * 6)
    specs = [
        TurnSpec(f"t{i}", f"Please give me a long answer about topic {i}.",
                 lambda s, t, prior: (True, ""))
        for i in range(1, 6)
    ]
    # Assert on the LAST turn's prompt: it must contain the compaction marker
    # rather than every prior turn verbatim.
    specs[-1] = TurnSpec(
        "t5", "One more long answer please.",
        lambda s, t, prior: (
            any("Earlier turns, condensed" in m.get("content", "")
                for m in t.initial_messages),
            "history was never compacted — REPL parity broken",
        ),
    )
    sc = Scenario("t-compact", turns=specs, max_steps=2)
    cfg = {**MOCK_CONFIG, "context_warn_tokens": 400}
    res = run_scenario_once(cfg, sc)
    last = res["turns"][-1]
    assert last["ok"], last["detail"]


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
    # Floors, not equalities: v1 had 6+6+3; suites may GAIN turns (uc3 gained
    # a model-resistance tracker when harness and model properties were split)
    # but must never lose coverage.
    assert len(UC1.turns) >= 6 and len(UC2.turns) >= 6 and len(UC3.turns) >= 3, \
        f"multiturn suite lost turns vs v1 (6+6+3): {len(UC1.turns)}+{len(UC2.turns)}+{len(UC3.turns)}"


def test_wait_for_free_slot_probes_until_the_backend_answers() -> None:
    import unittest.mock

    from evals import runner as R
    answers = iter(["backend unreachable: timed out", "backend HTTP 429", None])
    slept: list[float] = []
    with unittest.mock.patch.object(R, "backend_preflight", side_effect=lambda cfg: next(answers)):
        assert R.wait_for_free_slot({}, sleep=slept.append) is True
    assert slept == [R._SLOT_WAIT_SLEEP_S, R._SLOT_WAIT_SLEEP_S], slept
    with unittest.mock.patch.object(R, "backend_preflight", return_value="backend unreachable: timed out"):
        assert R.wait_for_free_slot({}, sleep=lambda s: None) is False


TESTS = [
    test_wait_for_free_slot_probes_until_the_backend_answers,
    test_clarification_grader_rejects_empty_completions_and_bare_question_marks,
    test_grounded_answer_grader,
    test_live_state_patterns_are_word_bounded,
    test_stats_exact_tests_known_values,
    test_gate_is_binary_on_reliable_cases_and_tracks_the_rest,
    test_scenario_think_time_pauses_between_turns_only,
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
    test_backend_failures_are_invalid_not_model_failures,
    test_graders_for_the_find_a_file_case,
    test_a_pinned_gate_set_replaces_the_luck_filter,
    test_load_pinned_set_is_absent_until_someone_adopts_one,
    test_propose_gate_set_admits_only_what_never_missed,
    test_a_chunk_file_carries_the_same_identity_as_a_whole_suite_run,
    test_case_reliability_matches_the_binomial,
    test_calibrate_reports_what_the_rule_does_to_a_no_op_candidate,
    test_calibrate_will_not_score_a_case_it_has_barely_seen,
    test_platform_reading_counts_only_this_suites_traffic,
    test_platform_reading_survives_a_truncated_or_missing_log,
    test_describe_platform_reads_as_one_line,
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
    test_scenario_driver_applies_auto_compaction,
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
