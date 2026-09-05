#!/usr/bin/env python3
"""evals/regrade.py — re-apply the CURRENT graders to saved traces.

A grader fix should not cost NPU time. Every results file keeps the full
trace of every run (final message, tool calls with output, model calls), and
a grader that reads only the trace — answer content, tools used, grounding —
can be re-run against it offline. Graders that inspect the sandbox (file
state, executed scripts) cannot; name only trace-graded cases with --cases,
and the file records which cases were regraded and when.

Usage:
    python evals/regrade.py RESULTS.json --cases ambiguous-1,ambiguous-2,ambiguous-3
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.cases_extended import EXTENDED_CASES  # noqa: E402
from evals.runner import (  # noqa: E402
    _TRACE_TEXT_CAP,
    LlmCall,
    RunOutcome,
    ToolCall,
    Trace,
    aggregate,
)


def trace_from_json(t: dict[str, Any]) -> Trace:
    tr = Trace()
    tr.end_kind = t.get("end_kind", "")
    tr.final_message = t.get("final_message", "")
    tr.wall_s = t.get("wall_s", 0.0)
    tr.llm_calls = [LlmCall(c["step"], c["attempt"], c.get("raw", ""), c.get("latency_s", 0.0))
                    for c in t.get("llm_calls", [])]
    tr.tool_calls = [ToolCall(c["step"], c["tool"], dict(c.get("args") or {}), c.get("output", ""),
                              c.get("latency_s", 0.0), c.get("status", "ok"))
                     for c in t.get("tool_calls", [])]
    return tr


def regrade(payload: dict[str, Any], case_ids: list[str]) -> dict[str, dict[str, str]]:
    by_id = {c.id: c for c in EXTENDED_CASES}
    changes: dict[str, dict[str, str]] = {}
    for cid in case_ids:
        case = by_id.get(cid)
        rec = payload.get("cases", {}).get(cid)
        if case is None or rec is None:
            print(f"  {cid}: not in the suite or the file", file=sys.stderr)
            continue
        # A saved trace keeps at most _TRACE_TEXT_CAP chars per tool output.
        # A grader that reads tool output (grounding) would see a truncated
        # page and fail a correct answer: bigfile-1 regraded 3/3 -> 0/3 that
        # way on 2026-09-05. Such cases need a live run.
        truncated = any(len(c.get("output", "")) >= _TRACE_TEXT_CAP
                        for t in rec.get("traces", []) for c in t.get("tool_calls", []))
        if truncated:
            print(f"  {cid}: tool output truncated in the saved trace — re-run live instead", file=sys.stderr)
            continue
        outcomes: list[RunOutcome] = []
        for t in rec.get("traces", []):
            tr = trace_from_json(t)
            try:
                ok, detail = case.verify(Path("."), tr)
            except Exception as exc:  # noqa: BLE001
                ok, detail = False, f"VERIFIER ERROR (fix the eval): {exc!r}"
            outcomes.append(RunOutcome(ok, detail, tr))
        # Invalid runs were never stored as traces; keep their count.
        before = f"{rec['passes']}/{rec['runs']}"
        agg = aggregate(outcomes)
        agg["invalid_runs"] = rec.get("invalid_runs", 0)
        agg["invalid_details"] = rec.get("invalid_details", [])
        for key in ("category", "tag", "traces"):
            agg[key] = rec.get(key)
        payload["cases"][cid] = agg
        after = f"{agg['passes']}/{agg['runs']}"
        changes[cid] = {"before": before, "after": after}
        payload.setdefault("regraded", {})[cid] = {"at": time.time(), "before": before, "after": after}
    return changes


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("results", type=Path)
    ap.add_argument("--cases", required=True)
    args = ap.parse_args()
    payload = json.loads(args.results.read_text(encoding="utf-8"))
    ids = [c.strip() for c in args.cases.split(",") if c.strip()]
    changes = regrade(payload, ids)
    for cid, ch in changes.items():
        mark = "" if ch["before"] == ch["after"] else "  <- changed"
        print(f"  {cid:<26} {ch['before']:>5} -> {ch['after']:<5}{mark}")
    args.results.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    print(f"regraded {len(changes)} case(s) in {args.results}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
