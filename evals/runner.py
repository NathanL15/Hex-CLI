#!/usr/bin/env python3
"""evals/runner.py — Eval v2 core: drives the PRODUCTION agent loop.

Design goals (see docs/V2_PLAN.md §8):
  * Prompt parity by construction — cases run through hexcli.agent.run_autopilot
    itself, observed via the AutopilotProbe seam. No reimplemented loop, so the
    conditional tool schemas, workspace snapshot, retry policy, loop detector,
    and compaction paths are all the real thing.
  * Every case has a BINARY verdict from state + content assertions. No
    advisory-only categories; a case either passes or fails, with a reason.
  * Stochastic honesty — N runs per case at production sampling settings;
    reports pass@k, pass^k, and a Wilson 95% interval on the pass rate.
  * Isolation — fresh sandbox per run; the user's real ~/.shellai memory rules
    and global vector store are redirected into the sandbox so eval prompts are
    controlled, not contaminated by whatever is on the host.
  * Latency accounting that can't lie — per-LLM-call latencies are recorded
    with their step/attempt indices, so retries are never folded into
    "first response latency". Token counts are chars/4 ESTIMATES and labelled
    as such until the server reports usage.

Suites live in cases_smoke.py / cases_extended.py / cases_multiturn.py and
call run_suite_cli() with their case lists.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent  # project root
sys.path.insert(0, str(APP_DIR))

import hexcli.agent as sa  # noqa: E402
from hexcli import memory  # noqa: E402

CONFIG_PATH = APP_DIR / "shellai_npurun.json"
RESULTS_DIR = Path(__file__).resolve().parent / "results"

_TRACE_TEXT_CAP = 2000  # chars kept per raw response / tool output in saved traces


# ---------------------------------------------------------------------------
# Trace — everything the probe observed during one run_autopilot call
# ---------------------------------------------------------------------------

@dataclass
class LlmCall:
    step: int
    attempt: int
    raw: str
    latency_s: float


@dataclass
class ToolCall:
    step: int
    tool: str
    args: dict[str, Any]
    output: str
    latency_s: float
    status: str  # "ok" | "error"


class Trace(sa.AutopilotProbe):
    """AutopilotProbe implementation that records the full loop transcript."""

    def __init__(self) -> None:
        self.system_prompt: str = ""
        self.initial_messages: list[dict[str, str]] = []
        self.llm_calls: list[LlmCall] = []
        self.tool_calls: list[ToolCall] = []
        self.end_kind: str = ""      # finish | fallthrough | loop_stop | step_limit | "" (short-circuit)
        self.final_message: str = ""
        self.wall_s: float = 0.0

    # -- probe callbacks ----------------------------------------------------
    def on_start(self, system_prompt: str, messages: list[dict[str, str]]) -> None:
        self.system_prompt = system_prompt
        self.initial_messages = messages

    def on_llm(self, step: int, attempt: int, raw: str, latency_s: float) -> None:
        self.llm_calls.append(LlmCall(step, attempt, raw, latency_s))

    def on_tool(self, step: int, tool: str, args: dict[str, Any], output: str,
                latency_s: float, status: str) -> None:
        self.tool_calls.append(ToolCall(step, tool, dict(args), output, latency_s, status))

    def on_end(self, kind: str, message: str) -> None:
        self.end_kind = kind
        self.final_message = message

    # -- convenience accessors for verify callables -------------------------
    @property
    def tools_used(self) -> list[str]:
        return [t.tool for t in self.tool_calls]

    @property
    def retries(self) -> int:
        """Number of malformed-output retry round-trips."""
        return sum(1 for c in self.llm_calls if c.attempt > 0)

    @property
    def first_llm_latency_s(self) -> float | None:
        """Latency of the very first LLM call only — never includes retries."""
        for c in self.llm_calls:
            if c.step == 0 and c.attempt == 0:
                return c.latency_s
        return None

    @property
    def total_llm_latency_s(self) -> float:
        return sum(c.latency_s for c in self.llm_calls)

    @property
    def est_input_tokens(self) -> int:
        """chars/4 ESTIMATE of the initial prompt size (system + history + user)."""
        return sum(len(m.get("content", "")) for m in self.initial_messages) // 4

    def commands(self) -> list[str]:
        """Every command/script string handed to an execution tool — for
        content-level adversarial grading (tool *names* are not enough)."""
        out: list[str] = []
        for t in self.tool_calls:
            if t.tool == "run_command":
                out.append(str(t.args.get("command", "")))
            elif t.tool == "run_code":
                out.append(str(t.args.get("path", "")))
            elif t.tool == "batch":
                out.append(json.dumps(t.args))
        return out

    def args_for(self, tool: str) -> list[dict[str, Any]]:
        return [t.args for t in self.tool_calls if t.tool == tool]

    def outputs_for(self, tool: str) -> list[str]:
        return [t.output for t in self.tool_calls if t.tool == tool]

    def to_json(self) -> dict[str, Any]:
        return {
            "end_kind": self.end_kind,
            "final_message": self.final_message[:_TRACE_TEXT_CAP],
            "wall_s": round(self.wall_s, 3),
            "est_input_tokens": self.est_input_tokens,
            "retries": self.retries,
            "first_llm_latency_s": round(self.first_llm_latency_s, 3) if self.first_llm_latency_s is not None else None,
            "total_llm_latency_s": round(self.total_llm_latency_s, 3),
            "llm_calls": [
                {"step": c.step, "attempt": c.attempt, "latency_s": round(c.latency_s, 3),
                 "raw": c.raw[:_TRACE_TEXT_CAP]}
                for c in self.llm_calls
            ],
            "tool_calls": [
                {"step": t.step, "tool": t.tool, "args": t.args, "status": t.status,
                 "latency_s": round(t.latency_s, 3), "output": t.output[:_TRACE_TEXT_CAP]}
                for t in self.tool_calls
            ],
        }


# ---------------------------------------------------------------------------
# Case / Scenario definitions
# ---------------------------------------------------------------------------

VerifyFn = Callable[[Path, Trace], tuple[bool, str]]


@dataclass
class Case:
    id: str
    category: str
    query: str
    verify: VerifyFn                       # REQUIRED — binary verdict
    max_steps: int = 6
    setup: dict[str, str] = field(default_factory=dict)      # sandbox files
    setup_fn: Callable[[Path], None] | None = None           # richer fixtures (ACLs, memory seeds)
    history: list[dict[str, str]] = field(default_factory=list)  # prior conversation turns
    expected_tools: tuple[str, ...] = ()   # metadata for reporting only
    tag: str = ""
    requires_memory_seed: bool = False     # run is INVALID (not failed) if seeding didn't take


@dataclass
class TurnSpec:
    id: str
    query: str
    # verify(sandbox, trace, prior_traces) -> (ok, detail)
    verify: Callable[[Path, Trace, list[Trace]], tuple[bool, str]]


@dataclass
class Scenario:
    """A multi-turn conversation sharing one sandbox and accumulated history,
    driven exactly the way the production REPL drives run_autopilot: history
    grows by (user query, assistant final message) pairs per turn."""
    id: str
    turns: list[TurnSpec]
    setup: dict[str, str] = field(default_factory=dict)
    max_steps: int = 12
    # Optional: replay another scenario first (its history feeds this one).
    prelude: Scenario | None = None


# ---------------------------------------------------------------------------
# Environment control
# ---------------------------------------------------------------------------

class _NoopMonitor:
    def __init__(self, *a: Any, **k: Any) -> None:
        import threading
        self.cancelled = threading.Event()

    def __enter__(self) -> _NoopMonitor:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class _NoopSpinner:
    def __init__(self, *a: Any, **k: Any) -> None: ...

    def __enter__(self) -> _NoopSpinner:
        return self

    def __exit__(self, *a: Any) -> None:
        return None


class _EvalEnv:
    """Context manager that makes one eval run hermetic:

    * cwd → fresh sandbox (run_autopilot keys everything off Path.cwd())
    * memory rules + global vector store → sandbox-local paths
    * Esc-cancel monitor + spinner → no-ops (headless-safe, no msvcrt polling
      threads inflating latency measurements)
    """

    def __init__(self, sandbox: Path) -> None:
        self.sandbox = sandbox
        self._saved: dict[str, Any] = {}

    def __enter__(self) -> _EvalEnv:
        self._saved["cwd"] = Path.cwd()
        self._saved["rules"] = memory._RULES_PATH
        self._saved["global_store"] = memory._GLOBAL_STORE_DIR
        self._saved["monitor"] = sa.CancelMonitor
        self._saved["spinner"] = sa.Spinner
        memory._RULES_PATH = self.sandbox / ".eval_home" / "memory_rules.md"
        memory._GLOBAL_STORE_DIR = self.sandbox / ".eval_home" / "global_vector_store"
        memory._RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        sa.CancelMonitor = _NoopMonitor  # type: ignore[misc,assignment]
        sa.Spinner = _NoopSpinner  # type: ignore[misc,assignment]
        os.chdir(self.sandbox)
        return self

    def __exit__(self, *a: Any) -> None:
        os.chdir(self._saved["cwd"])
        memory._RULES_PATH = self._saved["rules"]
        memory._GLOBAL_STORE_DIR = self._saved["global_store"]
        sa.CancelMonitor = self._saved["monitor"]
        sa.Spinner = self._saved["spinner"]


def seed_memory(sandbox: Path, entries: list[dict[str, Any]], config: dict[str, Any]) -> bool:
    """Seed the PROJECT vector store inside the sandbox. Returns True only if
    the entries verifiably landed on disk — callers must treat False as an
    invalid run, never as a model failure."""
    store = memory.VectorStore(config, cwd=str(sandbox))
    for entry in entries:
        entry = dict(entry)
        text = entry.pop("text")
        store.add(text, entry)
    store_dir = sandbox / ".shellai" / "vector_store"
    meta = store_dir / "metadata.json"
    try:
        if meta.exists():
            data = json.loads(meta.read_text(encoding="utf-8"))
            return len(data) >= len(entries)
    except Exception:
        pass
    return False


# ---------------------------------------------------------------------------
# Single-run drivers
# ---------------------------------------------------------------------------

@dataclass
class RunOutcome:
    ok: bool
    detail: str
    trace: Trace
    invalid: bool = False   # environment problem (e.g. memory seed failed), not a verdict


def run_case_once(config: dict[str, Any], case: Case) -> RunOutcome:
    # ignore_cleanup_errors: fixtures may leave read-only files (real
    # permission-error injection) which Windows rmtree can't delete.
    with tempfile.TemporaryDirectory(prefix="hexeval_", ignore_cleanup_errors=True) as tmp:
        sandbox = Path(tmp).resolve()
        for name, content in case.setup.items():
            p = sandbox / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(content, encoding="utf-8")
        with _EvalEnv(sandbox):
            if case.setup_fn:
                try:
                    case.setup_fn(sandbox)
                except _InvalidRun as exc:
                    return RunOutcome(False, str(exc), Trace(), invalid=True)
            run_config = dict(config)
            run_config["max_agent_steps"] = case.max_steps
            # Non-streaming keeps eval logs clean; the LLM call is blocking
            # either way, so latency semantics are unchanged.
            run_config["use_streaming"] = False
            trace = Trace()
            t0 = time.perf_counter()
            try:
                trace.final_message = sa.run_autopilot(
                    run_config, list(case.history), case.query,
                    sa.detect_shell(run_config.get("shell_exe", "")),
                    session=None, turn=None, probe=trace,
                )
            except Exception as exc:  # noqa: BLE001 — an unhandled loop crash is a FAIL, not a crash of the suite
                trace.wall_s = time.perf_counter() - t0
                return RunOutcome(False, f"run_autopilot raised: {exc!r}", trace)
            trace.wall_s = time.perf_counter() - t0
            try:
                ok, detail = case.verify(sandbox, trace)
            except Exception as exc:  # noqa: BLE001 — verifier bugs must be loud, not silent passes
                return RunOutcome(False, f"VERIFIER ERROR (fix the eval): {exc!r}", trace)
    return RunOutcome(ok, detail, trace)


class _InvalidRun(Exception):
    """Raised by setup_fn when the fixture could not be established."""


InvalidRun = _InvalidRun  # public alias for case modules


def run_scenario_once(config: dict[str, Any], scenario: Scenario) -> dict[str, Any]:
    """Run all turns of a scenario in one sandbox with accumulated history.
    Returns {turn_id: RunOutcome-like dict, ...} plus scenario-level info."""
    with tempfile.TemporaryDirectory(prefix="hexeval_mt_", ignore_cleanup_errors=True) as tmp:
        sandbox = Path(tmp).resolve()
        history: list[dict[str, str]] = []

        def _play(sc: Scenario, record: bool) -> list[dict[str, Any]]:
            recs: list[dict[str, Any]] = []
            traces: list[Trace] = []
            for name, content in sc.setup.items():
                p = sandbox / name
                if not p.exists():
                    p.parent.mkdir(parents=True, exist_ok=True)
                    p.write_text(content, encoding="utf-8")
            run_config = dict(config)
            run_config["max_agent_steps"] = sc.max_steps
            run_config["use_streaming"] = False
            for spec in sc.turns:
                trace = Trace()
                t0 = time.perf_counter()
                try:
                    trace.final_message = sa.run_autopilot(
                        run_config, list(history), spec.query,
                        sa.detect_shell(run_config.get("shell_exe", "")),
                        session=None, turn=None, probe=trace,
                    )
                except Exception as exc:  # noqa: BLE001
                    trace.wall_s = time.perf_counter() - t0
                    if record:
                        recs.append({"turn": spec.id, "ok": False,
                                     "detail": f"run_autopilot raised: {exc!r}",
                                     "trace": trace})
                    history.append({"role": "user", "content": spec.query})
                    history.append({"role": "assistant", "content": f"(error: {exc})"})
                    traces.append(trace)
                    continue
                trace.wall_s = time.perf_counter() - t0
                if record:
                    try:
                        ok, detail = spec.verify(sandbox, trace, traces)
                    except Exception as exc:  # noqa: BLE001
                        ok, detail = False, f"VERIFIER ERROR (fix the eval): {exc!r}"
                    recs.append({"turn": spec.id, "ok": ok, "detail": detail, "trace": trace})
                # Mirror the production REPL: condensed (query, final message) pairs.
                history.append({"role": "user", "content": spec.query})
                history.append({"role": "assistant", "content": trace.final_message})
                traces.append(trace)
            return recs

        with _EvalEnv(sandbox):
            if scenario.prelude:
                _play(scenario.prelude, record=False)
            turn_records = _play(scenario, record=True)

    return {"scenario": scenario.id, "turns": turn_records}


# ---------------------------------------------------------------------------
# Multi-run statistics
# ---------------------------------------------------------------------------

def wilson_interval(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 1.0)
    phat = passes / n
    denom = 1 + z * z / n
    centre = phat + z * z / (2 * n)
    margin = z * math.sqrt((phat * (1 - phat) + z * z / (4 * n)) / n)
    return (max(0.0, (centre - margin) / denom), min(1.0, (centre + margin) / denom))


def aggregate(outcomes: list[RunOutcome]) -> dict[str, Any]:
    valid = [o for o in outcomes if not o.invalid]
    n = len(valid)
    passes = sum(1 for o in valid if o.ok)
    lo, hi = wilson_interval(passes, n)
    firsts = [o.trace.first_llm_latency_s for o in valid if o.trace.first_llm_latency_s is not None]
    return {
        "runs": n,
        "invalid_runs": len(outcomes) - n,
        "passes": passes,
        "pass_rate": round(passes / n, 3) if n else None,
        "pass_at_k": passes > 0 if n else None,
        "pass_all_k": passes == n if n else None,
        "wilson95": [round(lo, 3), round(hi, 3)],
        "mean_first_llm_latency_s": round(sum(firsts) / len(firsts), 3) if firsts else None,
        "mean_retries": round(sum(o.trace.retries for o in valid) / n, 2) if n else None,
        "fail_details": sorted({o.detail for o in valid if not o.ok}),
        "invalid_details": sorted({o.detail for o in outcomes if o.invalid}),
    }


# ---------------------------------------------------------------------------
# Suite execution + report
# ---------------------------------------------------------------------------

def run_cases(config: dict[str, Any], cases: list[Case], runs: int,
              progress: bool = True) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for case in cases:
        outcomes: list[RunOutcome] = []
        for i in range(runs):
            if progress:
                print(f"  {case.id} run {i + 1}/{runs}...", file=sys.stderr, flush=True)
            outcomes.append(run_case_once(config, case))
        agg = aggregate(outcomes)
        agg["category"] = case.category
        agg["tag"] = case.tag or case.category
        agg["traces"] = [o.trace.to_json() for o in outcomes]
        results[case.id] = agg
    return results


def run_scenarios(config: dict[str, Any], scenarios: list[Scenario], runs: int,
                  progress: bool = True) -> dict[str, Any]:
    results: dict[str, Any] = {}
    for sc in scenarios:
        all_runs: list[dict[str, Any]] = []
        for i in range(runs):
            if progress:
                print(f"  scenario {sc.id} run {i + 1}/{runs}...", file=sys.stderr, flush=True)
            all_runs.append(run_scenario_once(config, sc))
        # Aggregate per turn across runs.
        per_turn: dict[str, Any] = {}
        for spec in sc.turns:
            outs = []
            for r in all_runs:
                rec = next((t for t in r["turns"] if t["turn"] == spec.id), None)
                if rec:
                    outs.append(RunOutcome(rec["ok"], rec["detail"], rec["trace"]))
            agg = aggregate(outs)
            agg["traces"] = [o.trace.to_json() for o in outs]
            per_turn[spec.id] = agg
        results[sc.id] = {"turns": per_turn}
    return results


def print_case_report(results: dict[str, Any], runs: int) -> list[str]:
    findings: list[str] = []
    print()
    print(f"{'ID':<24}{'Tag':<16}{'Pass':<10}{'pass^k':<8}{'Wilson95':<16}{'1st-LLM(s)':<12}{'Retries'}")
    print("-" * 96)
    for cid, r in results.items():
        rate = f"{r['passes']}/{r['runs']}" if r["runs"] else "n/a"
        pall = "yes" if r.get("pass_all_k") else ("NO" if r["runs"] else "-")
        wil = f"[{r['wilson95'][0]},{r['wilson95'][1]}]"
        lat = r["mean_first_llm_latency_s"] if r["mean_first_llm_latency_s"] is not None else "-"
        print(f"{cid:<24}{r['tag']:<16}{rate:<10}{pall:<8}{wil:<16}{lat!s:<12}{r['mean_retries']}")
        for d in r["fail_details"]:
            findings.append(f"[FAIL] {cid}: {d}")
        for d in r["invalid_details"]:
            findings.append(f"[INVALID-RUN] {cid}: {d}")
    print()
    if findings:
        print(f"FINDINGS ({len(findings)}):")
        for f in findings:
            print(f"  - {f}")
    else:
        print(f"All cases passed all {runs} run(s).")
    print()
    return findings


def print_scenario_report(results: dict[str, Any]) -> list[str]:
    findings: list[str] = []
    for sid, sc in results.items():
        print(f"\n── Scenario {sid} ──")
        print(f"{'Turn':<14}{'Pass':<10}{'1st-LLM(s)':<12}{'~in-tok':<10}{'Retries'}")
        print("-" * 56)
        for tid, r in sc["turns"].items():
            rate = f"{r['passes']}/{r['runs']}" if r["runs"] else "n/a"
            lat = r["mean_first_llm_latency_s"] if r["mean_first_llm_latency_s"] is not None else "-"
            toks = r["traces"][0]["est_input_tokens"] if r["traces"] else "-"
            print(f"{tid:<14}{rate:<10}{lat!s:<12}{toks!s:<10}{r['mean_retries']}")
            for d in r["fail_details"]:
                findings.append(f"[FAIL] {sid}/{tid}: {d}")
    print()
    if findings:
        print(f"FINDINGS ({len(findings)}):")
        for f in findings:
            print(f"  - {f}")
    print()
    return findings


def latency_scaling(results: dict[str, Any], scenario_id: str) -> None:
    """Print first-attempt-latency vs est-input-token scaling for a scenario.
    Retries are excluded by construction (first_llm_latency_s is attempt 0)."""
    sc = results.get(scenario_id)
    if not sc:
        return
    points = []
    for tid, r in sc["turns"].items():
        if r["traces"]:
            t = r["traces"][0]
            if t["first_llm_latency_s"] is not None:
                points.append((tid, t["est_input_tokens"], t["first_llm_latency_s"]))
    if len(points) < 2:
        return
    print("── LATENCY SCALING (first LLM call only, retries excluded; ~tokens are chars/4 estimates) ──")
    for tid, tok, lat in points:
        print(f"  {tid:<14}~{tok:<8} tok   {lat:.1f}s")
    (t0_id, t0_tok, t0_lat), (tn_id, tn_tok, tn_lat) = points[0], points[-1]
    if t0_tok > 0 and tn_tok > t0_tok and t0_lat > 0 and tn_lat > t0_lat:
        alpha = math.log(tn_lat / t0_lat) / math.log(tn_tok / t0_tok)
        print(f"  α ≈ {alpha:.2f} ({t0_id} → {tn_id}; α=1 linear, α>1 superlinear)")
    print()


# ---------------------------------------------------------------------------
# CLI entry shared by suite modules
# ---------------------------------------------------------------------------

def load_live_config() -> dict[str, Any]:
    return sa.load_config(CONFIG_PATH)


def run_suite_cli(
    suite_name: str,
    cases: list[Case] | None = None,
    scenarios: list[Scenario] | None = None,
    default_runs: int = 1,
    scaling_scenario: str | None = None,
) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="Run only this case/scenario id.")
    parser.add_argument("--runs", type=int, default=default_runs,
                        help=f"Runs per case (default {default_runs}).")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()

    config = load_live_config()
    payload: dict[str, Any] = {
        "suite": suite_name,
        "timestamp": time.time(),
        "model": config.get("model"),
        "temperature": config.get("temperature"),
        "runs_per_case": args.runs,
    }
    findings: list[str] = []

    if cases:
        selected = [c for c in cases if not args.case or c.id == args.case]
        if selected:
            case_results = run_cases(config, selected, args.runs)
            findings += print_case_report(case_results, args.runs)
            payload["cases"] = case_results
    if scenarios:
        selected_sc = [s for s in scenarios if not args.case or s.id == args.case]
        if selected_sc:
            sc_results = run_scenarios(config, selected_sc, args.runs)
            findings += print_scenario_report(sc_results)
            if scaling_scenario:
                latency_scaling(sc_results, scaling_scenario)
            payload["scenarios"] = sc_results
    if cases and scenarios is None and not [c for c in cases if not args.case or c.id == args.case]:
        print(f"No case with id '{args.case}'.")
        return 1

    payload["findings"] = findings
    if not args.no_save:
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        out = RESULTS_DIR / f"{suite_name}_results.json"
        out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
        print(f"Saved: {out}")
    return 0
