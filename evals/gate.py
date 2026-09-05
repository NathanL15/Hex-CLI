#!/usr/bin/env python3
"""evals/gate.py — the ship gate, and the scoreboard.

Council verdict (2026-09-04): with 3 runs per case the leaderboard cannot
distinguish a regression from list position, so the gate is BINARY on the
cases the baseline passes reliably, and the flaky middle is tracked as a
ceiling panel instead of gated.

  gate set       cases 3/3 in EVERY baseline given (one baseline is allowed,
                 two is better: a case at a true 90% shows 3/3 only 73% of
                 the time, so one run's 3/3 is weak evidence of reliability —
                 applied to the two v2.4/v2.5 arms, 30 cases were 3/3 in one
                 and 27 in both)
  ceiling panel  every other valid case — reported with deltas, never gated
  recheck rule   a gate case that misses at 3 runs is RECHECK, not FAIL:
                 re-run it with 6 runs; it is broken when it misses twice
                 (passes < runs - 1 at six runs). A 90% case survives six
                 runs 89% of the time; a case that truly broke never does.

Usage:
    python evals/gate.py --baseline A.json [--baseline B.json] CANDIDATE.json
    python evals/gate.py --scoreboard RESULTS.json         # write results/LATEST.md
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.stats import compare_arms, format_comparison  # noqa: E402

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

RESULTS_DIR = Path(__file__).resolve().parent / "results"
SCOREBOARD = RESULTS_DIR / "LATEST.md"
RECHECK_RUNS = 6


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def gate_sets(*baselines: dict[str, Any]) -> tuple[list[str], list[str]]:
    """(gate, ceiling): gate = valid in all baselines and 3/3 in all of them;
    ceiling = valid in the first baseline and not in the gate."""
    first = baselines[0]
    valid = [i for i, r in first.items() if r.get("runs")]
    gate = sorted(i for i in valid
                  if all(i in b and b[i].get("runs") and b[i].get("pass_all_k") for b in baselines))
    ceiling = sorted(i for i in valid if i not in gate)
    return gate, ceiling


def case_status(r: dict[str, Any]) -> str:
    """'ok' | 'recheck' | 'broken' for one gate case in the candidate."""
    runs, passes = r.get("runs", 0), r.get("passes", 0)
    if not runs:
        return "missing"
    if passes == runs:
        return "ok"
    if runs >= RECHECK_RUNS:
        return "ok" if passes >= runs - 1 else "broken"
    return "recheck"


def evaluate(baselines: list[dict[str, Any]], candidate: dict[str, Any]) -> dict[str, Any]:
    bcs = [b.get("cases", {}) for b in baselines]
    cc = candidate.get("cases", {})
    gate, ceiling = gate_sets(*bcs)
    status = {i: case_status(cc.get(i, {})) for i in gate}
    broken = [i for i in gate if status[i] == "broken"]
    recheck = [i for i in gate if status[i] == "recheck"]
    missing = [i for i in gate if status[i] == "missing"]
    panel = []
    for i in ceiling:
        if i in cc and cc[i].get("runs"):
            a, b = bcs[0][i], cc[i]
            panel.append({"case": i, "baseline": f"{a['passes']}/{a['runs']}",
                          "candidate": f"{b['passes']}/{b['runs']}",
                          "delta": round(b["passes"] / b["runs"] - a["passes"] / a["runs"], 2)})
    if broken:
        verdict = "FAIL"
    elif recheck:
        verdict = "RECHECK"
    elif missing:
        verdict = "INCOMPLETE"
    else:
        verdict = "PASS"
    return {
        "gate_size": len(gate), "broken": broken, "recheck": recheck, "missing": missing,
        "verdict": verdict,
        "ceiling_panel": panel,
        "ceiling_gained": [p["case"] for p in panel if p["delta"] > 0],
        "ceiling_lost": [p["case"] for p in panel if p["delta"] < 0],
        "server_drift": _drift(candidate),
        "stats": compare_arms(bcs[0], cc),
    }


def _drift(d: dict[str, Any]) -> str | None:
    canary = d.get("canary_s") or {}
    s, e = canary.get("start"), canary.get("end")
    if s and e and e > 2.0 * s:
        return f"canary {s:.1f}s → {e:.1f}s: the server slowed {e / s:.1f}× during the run"
    return None


def print_verdict(rep: dict[str, Any], baselines: list[Path], candidate: Path) -> None:
    print(f"\nGATE  baseline={' + '.join(p.name for p in baselines)}  candidate={candidate.name}")
    print(f"  gate set: {rep['gate_size']} cases at 3/3 in every baseline")
    if rep["broken"]:
        print(f"  BROKEN ({len(rep['broken'])}): {', '.join(rep['broken'])}")
    if rep["recheck"]:
        print(f"  missed once at 3 runs ({len(rep['recheck'])}): {', '.join(rep['recheck'])}")
        print(f"    re-run: python evals/run_chunk.py --cases {','.join(rep['recheck'])} "
              f"--runs {RECHECK_RUNS} --out evals/results/{candidate.name}")
    if rep["missing"]:
        print(f"  not run in candidate ({len(rep['missing'])}): {', '.join(rep['missing'])}")
    print(f"  verdict: {rep['verdict']}")
    if rep["server_drift"]:
        print(f"  WARNING {rep['server_drift']} — treat every late case with suspicion")
    print(f"\nCEILING PANEL ({len(rep['ceiling_panel'])} cases not reliably 3/3; tracked, not gated)")
    for p in rep["ceiling_panel"]:
        mark = "+" if p["delta"] > 0 else ("-" if p["delta"] < 0 else " ")
        print(f"  {mark} {p['case']:<26} {p['baseline']:>5} -> {p['candidate']:<5}")
    print("\nSignificance (vs the first baseline):")
    for line in format_comparison(rep["stats"]).splitlines():
        print(f"  {line}")


def scoreboard(path: Path, extra_baselines: list[Path] = ()) -> str:
    d = _load(path)
    cases = d.get("cases", {})
    meta = d.get("metadata") or {}
    server = meta.get("server") or {}
    gate, ceiling = gate_sets(cases, *[_load(p).get("cases", {}) for p in extra_baselines])
    valid = [r for r in cases.values() if r.get("runs")]
    perfect = sum(1 for r in valid if r.get("pass_all_k"))
    passes = sum(r["passes"] for r in valid)
    runs = sum(r["runs"] for r in valid)
    ts = d.get("timestamp")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(ts)) if isinstance(ts, (int, float)) else str(ts)
    canary = d.get("canary_s") or {}
    names = " + ".join([path.name] + [p.name for p in extra_baselines])
    lines = [
        "# Current eval number",
        "",
        f"Source: `{path.name}` — suite `{d.get('suite')}`, {when}, {d.get('runs_per_case')} runs/case.",
        f"Gate set from: {names}.",
        "",
        "| | |",
        "|---|---|",
        f"| pass^k (3/3) | **{perfect}/{len(valid)}** |",
        f"| run-level | {passes}/{runs} ({passes / runs:.1%}) |" if runs else "| run-level | n/a |",
        f"| gate set | {len(gate)} cases — a candidate must keep every one (recheck rule: 6 runs, one miss allowed) |",
        f"| ceiling panel | {len(ceiling)} cases — tracked, not gated |",
        f"| build | git {meta.get('git_sha', '?')}{'+' if meta.get('git_dirty') else ''}, {meta.get('npurun') or 'npurun ?'} |",
        f"| server | budget {server.get('input_token_budget', '?')}, window {server.get('context_size', '?')} |",
        f"| order | seed {d.get('seed', '—')} |",
        (f"| canary | {canary['start']}s → {canary['end']}s |" if canary.get("start") and canary.get("end")
         else f"| canary | not measured{(' — ' + canary['note']) if canary.get('note') else ''} |"),
        f"| model calls | {d.get('llm_calls', '?')} |",
        "",
        "## Ceiling panel",
        "",
    ]
    for i in ceiling:
        r = cases[i]
        lines.append(f"- `{i}` {r['passes']}/{r['runs']}")
    lines += ["", "## How to gate a change", "",
              f"    python evals/gate.py --baseline evals/results/{path.name} "
              + " ".join(f"--baseline evals/results/{p.name}" for p in extra_baselines)
              + " evals/results/<candidate>.json", "",
              "Run the candidate on a fresh server with a recorded seed; a p-value above 0.05 at "
              "3 runs per case is absence of evidence, not parity (evals/stats.py)."]
    return "\n".join(lines) + "\n"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path, nargs="?")
    ap.add_argument("--baseline", type=Path, action="append", default=[],
                    help="Baseline results file (repeatable: the gate set is the intersection).")
    ap.add_argument("--scoreboard", type=Path, help="Write results/LATEST.md from this results file.")
    args = ap.parse_args()
    if args.scoreboard:
        SCOREBOARD.write_text(scoreboard(args.scoreboard, args.baseline), encoding="utf-8")
        print(f"wrote {SCOREBOARD}")
        return 0
    if not (args.baseline and args.candidate):
        ap.error("give --baseline A.json [--baseline B.json] CANDIDATE.json, or --scoreboard RESULTS")
    rep = evaluate([_load(p) for p in args.baseline], _load(args.candidate))
    print_verdict(rep, args.baseline, args.candidate)
    return 1 if rep["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
