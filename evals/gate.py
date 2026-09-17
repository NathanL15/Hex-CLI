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
from math import comb
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


GATE_SET_PATH = Path(__file__).resolve().parent / "gate_set.json"


def load_pinned_set(path: Path | None = None) -> dict[str, Any] | None:
    """The pinned gate set, or None when there is no such file.

    Deriving membership from "3/3 in every baseline" has two defects that a
    pinned file fixes together. It is a filter on LUCK — a case at a true
    86 % is 3/3 in a three-run arm about 64 % of the time, so it enters the
    set on one good morning and is then held to 5/5 for ever — and the set
    it produces depends on WHICH baselines happen to be passed: measured
    2026-09-16, the pairs in use gave 27, 29, 31 and 32 cases, disagreeing
    about four of them. A verdict should not depend on the operator's
    command line.

    A pinned file states the membership and the evidence for it, so changing
    the set is a reviewable commit rather than a side effect.
    """
    path = path or GATE_SET_PATH
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data.get("cases"), dict) else None


def gate_sets(*baselines: dict[str, Any],
              pinned: dict[str, Any] | None = None) -> tuple[list[str], list[str]]:
    """(gate, ceiling).

    With a pinned set: gate = its cases, ceiling = everything else valid in
    the first baseline. Without one: the historical rule, gate = valid in all
    baselines and 3/3 in all of them.

    `pinned` is passed in rather than read here on purpose. A function that
    silently consults a file on disk ignores what its caller asked for, which
    is how adopting evals/gate_set.json broke four tests that were passing
    their own fixtures; main() reads the file once and hands it down.
    """
    first = baselines[0]
    valid = [i for i, r in first.items() if r.get("runs")]
    if pinned:
        gate = sorted(pinned["cases"])
    else:
        gate = sorted(i for i in valid
                      if all(i in b and b[i].get("runs") and b[i].get("pass_all_k")
                             for b in baselines))
    ceiling = sorted(i for i in valid if i not in gate)
    return gate, ceiling


def propose_gate_set(arms: list[dict[str, Any]], min_runs: int = 12) -> dict[str, Any]:
    """A gate set built from measurement instead of luck.

    A case qualifies when it never missed across the supplied arms and was
    run at least `min_runs` times in total. "Never missed" is the bar because
    the rule the gate applies is a perfect score: a case that cannot hold
    15/15 on unchanged code cannot carry a binary verdict, and eight of the
    thirty members failed that test when it was first measured.
    """
    pooled: dict[str, list[int]] = {}
    for arm in arms:
        for cid, case in (arm.get("cases") or {}).items():
            if case.get("runs"):
                tally = pooled.setdefault(cid, [0, 0])
                tally[0] += case["passes"]
                tally[1] += case["runs"]
    cases = {cid: {"passes": k, "runs": n}
             for cid, (k, n) in sorted(pooled.items()) if k == n and n >= min_runs}
    rejected = {cid: {"passes": k, "runs": n}
                for cid, (k, n) in sorted(pooled.items()) if cid not in cases}
    return {
        "criterion": f"no missed run across the arms below, and at least {min_runs} runs",
        "measured": time.strftime("%Y-%m-%d"),
        "arms": [a.get("suite", "?") for a in arms],
        "cases": cases,
        "not_gated": rejected,
    }


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


def evaluate(baselines: list[dict[str, Any]], candidate: dict[str, Any],
             pinned: dict[str, Any] | None = None) -> dict[str, Any]:
    bcs = [b.get("cases", {}) for b in baselines]
    cc = candidate.get("cases", {})
    gate, ceiling = gate_sets(*bcs, pinned=pinned)
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
        "gate_origin": (f"pinned, {pinned.get('criterion', 'see evals/gate_set.json')}"
                        if pinned else "3/3 in every baseline"),
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
    origin = rep.get("gate_origin") or "3/3 in every baseline"
    print(f"  gate set: {rep['gate_size']} cases ({origin})")
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
    gate, ceiling = gate_sets(cases, *[_load(p).get("cases", {}) for p in extra_baselines],
                              pinned=load_pinned_set())
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



# ---------------------------------------------------------------------------
# Calibration — what the gate does to a candidate that changed NOTHING
# ---------------------------------------------------------------------------
#
# Membership is decided by "3/3 in every baseline", which is a filter on luck,
# not a measurement of reliability: a case at a true 86% is 3/3 in one arm 64%
# of the time, so it can enter the set and then be held to 5/5 forever after.
#
# Measured 2026-09-16 over the deduplicated production arms, five members of
# the 27-case set are nowhere near reliable — factual-1 86%, self-correct-1
# 87%, agentic-3 90%, regression-anchor-1 92%, agentic-2 95% — and the gate
# inherits their variance: a regression-free candidate takes a clean PASS
# about 1% of the time and is declared FAIL about 30% of the time. The record
# agrees: of the eight gate runs in evals/results/*.log, EVERY one went to
# RECHECK first and three ended FAIL, two of those overturned by a control on
# unchanged code.
#
# This function does not change any verdict. It reports what the rule implies,
# so the gate set can be re-based on evidence instead of on one lucky arm.


def _binom_pmf(k: int, n: int, p: float) -> float:
    return comb(n, k) * (p ** k) * ((1.0 - p) ** (n - k))


def _p_at_least(k: int, n: int, p: float) -> float:
    return sum(_binom_pmf(i, n, p) for i in range(k, n + 1))


def case_reliability(rate: float, arm_runs: int = 5,
                     recheck_runs: int = RECHECK_RUNS) -> tuple[float, float]:
    """(P(this case goes to recheck), P(it is declared broken)) at a true
    pass rate, under this module's own rule: clean only at arm_runs/arm_runs,
    then broken below recheck_runs - 1 of recheck_runs."""
    p_recheck = 1.0 - rate ** arm_runs
    p_survives = _p_at_least(recheck_runs - 1, recheck_runs, rate)
    return p_recheck, p_recheck * (1.0 - p_survives)


def calibrate(baselines: list[dict[str, Any]], arms: list[dict[str, Any]],
              arm_runs: int = 5, pinned: dict[str, Any] | None = None) -> dict[str, Any]:
    """Estimate each gate case's true pass rate from `arms` — which must NOT
    be the baselines that selected the set, or the estimate inherits the same
    luck — and report what the rule does to a candidate that changed nothing.

    The rate uses a Jeffreys posterior mean, (k + 0.5) / (n + 1), so a case
    seen 10 times and passing 10 does not read as a certain 100%.
    """
    gate, _ = gate_sets(*[b.get("cases", {}) for b in baselines], pinned=pinned)
    pooled: dict[str, list[int]] = {cid: [0, 0] for cid in gate}
    for arm in arms:
        for cid in gate:
            c = (arm.get("cases") or {}).get(cid)
            if c and c.get("runs"):
                pooled[cid][0] += c["passes"]
                pooled[cid][1] += c["runs"]
    rows = []
    for cid in gate:
        k, n = pooled[cid]
        if n < 8:
            rows.append({"case": cid, "passes": k, "runs": n, "rate": None,
                         "p_recheck": None, "p_broken": None})
            continue
        rate = (k + 0.5) / (n + 1)
        p_recheck, p_broken = case_reliability(rate, arm_runs)
        rows.append({"case": cid, "passes": k, "runs": n, "rate": rate,
                     "p_recheck": p_recheck, "p_broken": p_broken})
    scored = [r for r in rows if r["rate"] is not None]
    p_clean = 1.0
    p_no_break = 1.0
    for r in scored:
        p_clean *= 1.0 - r["p_recheck"]
        p_no_break *= 1.0 - r["p_broken"]
    return {
        "rows": sorted(rows, key=lambda r: (r["rate"] is None, r["rate"] or 0)),
        "scored": len(scored), "gate_size": len(gate), "arms": len(arms),
        "p_clean_pass": p_clean, "p_false_fail": 1.0 - p_no_break,
        "unscored": [r["case"] for r in rows if r["rate"] is None],
    }


def print_calibration(rep: dict[str, Any], threshold: float = 0.97) -> None:
    print(f"\nCALIBRATION  {rep['gate_size']} gate cases, "
          f"{rep['scored']} with enough data, estimated from {rep['arms']} arm(s)")
    print("  Pass the arms the gate set was NOT chosen from, or the estimate "
          "inherits the same luck.\n")
    print(f"  {'case':28}{'observed':>12}{'rate':>9}{'P(recheck)':>12}{'P(broken)':>11}")
    print("  " + "-" * 70)
    for r in rep["rows"]:
        if r["rate"] is None:
            print(f"  {r['case']:28}{str(r['passes']) + '/' + str(r['runs']):>12}"
                  f"{'too few runs':>32}")
            continue
        flag = "  <- not gate-worthy" if r["rate"] < threshold else ""
        print(f"  {r['case']:28}{str(r['passes']) + '/' + str(r['runs']):>12}"
              f"{r['rate']:>8.1%}{r['p_recheck']:>11.1%}{r['p_broken']:>11.1%}{flag}")
    print("\n  A candidate that changed NOTHING:")
    print(f"    clean PASS, no recheck   {rep['p_clean_pass']:6.1%}")
    print(f"    declared FAIL            {rep['p_false_fail']:6.1%}")
    weak = [r["case"] for r in rep["rows"] if r["rate"] is not None and r["rate"] < threshold]
    if weak:
        print(f"\n  Below {threshold:.0%}, so they carry that risk without earning it:")
        print(f"    {', '.join(weak)}")
        print("  Moving them to the ceiling panel is a change to the gate SET, which is"
              "\n  the owner's call; this command only reports what the rule implies.")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate", type=Path, nargs="?")
    ap.add_argument("--baseline", type=Path, action="append", default=[],
                    help="Baseline results file (repeatable: the gate set is the intersection).")
    ap.add_argument("--scoreboard", type=Path, help="Write results/LATEST.md from this results file.")
    ap.add_argument("--calibrate", type=Path, action="append", default=[],
                    help="Arm results to estimate each gate case's true rate from "
                         "(repeatable). Reports what the rule does to a candidate "
                         "that changed nothing. Never pass the baselines here.")
    ap.add_argument("--propose-set", type=Path, action="append", default=[],
                    help="Arm results to build a measured gate set from (repeatable). "
                         "Prints a candidate evals/gate_set.json; adopting it is a commit.")
    ap.add_argument("--min-runs", type=int, default=12,
                    help="Runs a case needs before --propose-set will gate on it.")
    args = ap.parse_args()
    if args.propose_set:
        proposal = propose_gate_set([_load(p) for p in args.propose_set],
                                    min_runs=args.min_runs)
        print(json.dumps(proposal, indent=2))
        print(f"\n{len(proposal['cases'])} cases qualify; "
              f"{len(proposal['not_gated'])} do not:", file=sys.stderr)
        for cid, r in proposal["not_gated"].items():
            print(f"  {cid:26}{r['passes']}/{r['runs']}", file=sys.stderr)
        print(f"\nReview it, then write it to {GATE_SET_PATH} to adopt it. "
              f"Changing the gate set is a reviewable commit, not a side effect.",
              file=sys.stderr)
        return 0
    if args.calibrate:
        if not args.baseline:
            ap.error("--calibrate needs --baseline to know the gate set")
        print_calibration(calibrate([_load(p) for p in args.baseline],
                                    [_load(p) for p in args.calibrate],
                                    pinned=load_pinned_set()))
        return 0
    if args.scoreboard:
        SCOREBOARD.write_text(scoreboard(args.scoreboard, args.baseline), encoding="utf-8")
        print(f"wrote {SCOREBOARD}")
        return 0
    if not (args.baseline and args.candidate):
        ap.error("give --baseline A.json [--baseline B.json] CANDIDATE.json, or --scoreboard RESULTS")
    rep = evaluate([_load(p) for p in args.baseline], _load(args.candidate),
                   pinned=load_pinned_set())
    print_verdict(rep, args.baseline, args.candidate)
    return 1 if rep["verdict"] == "FAIL" else 0


if __name__ == "__main__":
    raise SystemExit(main())
