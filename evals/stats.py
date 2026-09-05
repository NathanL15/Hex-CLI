#!/usr/bin/env python3
"""evals/stats.py — the significance tests the A/B verdicts rest on.

Lifted out of the one-off analysis script that produced the prompt-split
and window verdicts (2026-08-31, 2026-09-02) so a verdict is never computed
in a scratchpad again. Pure stdlib, exact tests: the arms are small (n=3
per case, ~40 cases) and normal approximations lie at that size.

  fisher_exact_two_sided  run-level: are the two arms' pass RATES different?
  mcnemar_exact           case-level: on the paired pass^k verdicts, do the
                          arms flip cases in one direction more than the other?
  compare_arms            both, plus the per-case deltas, in one dict.
"""
from __future__ import annotations

from math import comb
from typing import Any


def fisher_exact_two_sided(k1: int, n1: int, k2: int, n2: int) -> float:
    """Two-sided Fisher exact test on a 2x2 table (k1/n1 vs k2/n2 passes),
    summing every hypergeometric point-probability at or below the observed."""
    K = k1 + k2
    N = n1 + n2
    if N == 0 or K == 0 or K == N:
        return 1.0

    def pmf(x: int) -> float:
        return comb(n1, x) * comb(n2, K - x) / comb(N, K)

    lo, hi = max(0, K - n2), min(K, n1)
    p_obs = pmf(k1)
    return min(1.0, sum(pmf(x) for x in range(lo, hi + 1) if pmf(x) <= p_obs * (1 + 1e-9)))


def mcnemar_exact(b: int, c: int) -> float:
    """Exact (binomial) McNemar test on the discordant pairs: b cases that
    pass in A only, c that pass in B only. Two-sided."""
    n = b + c
    if n == 0:
        return 1.0
    m = min(b, c)
    p = sum(comb(n, x) for x in range(0, m + 1)) / 2 ** n
    return min(1.0, 2 * p)


def compare_arms(a_cases: dict[str, Any], b_cases: dict[str, Any]) -> dict[str, Any]:
    """Run-level and case-level comparison of two results files' `cases`."""
    ids = sorted(set(a_cases) & set(b_cases))
    valid = [i for i in ids if a_cases[i].get("runs") and b_cases[i].get("runs")]
    kA = sum(a_cases[i]["passes"] for i in valid)
    nA = sum(a_cases[i]["runs"] for i in valid)
    kB = sum(b_cases[i]["passes"] for i in valid)
    nB = sum(b_cases[i]["runs"] for i in valid)
    a_only = [i for i in valid if a_cases[i].get("pass_all_k") and not b_cases[i].get("pass_all_k")]
    b_only = [i for i in valid if not a_cases[i].get("pass_all_k") and b_cases[i].get("pass_all_k")]
    deltas = {}
    for i in valid:
        ra, rb = a_cases[i], b_cases[i]
        d = rb["passes"] / rb["runs"] - ra["passes"] / ra["runs"]
        if abs(d) > 1e-9:
            deltas[i] = {"a": f"{ra['passes']}/{ra['runs']}", "b": f"{rb['passes']}/{rb['runs']}",
                         "delta": round(d, 3)}
    return {
        "paired_cases": len(ids),
        "valid_cases": len(valid),
        "invalid_runs": {"a": sum(r.get("invalid_runs", 0) for r in a_cases.values()),
                         "b": sum(r.get("invalid_runs", 0) for r in b_cases.values())},
        "run_level": {"a": [kA, nA], "b": [kB, nB],
                      "delta": round((kB / nB if nB else 0) - (kA / nA if nA else 0), 4),
                      "fisher_p": round(fisher_exact_two_sided(kA, nA, kB, nB), 4)},
        "pass_all_k": {"a": sum(1 for i in valid if a_cases[i].get("pass_all_k")),
                       "b": sum(1 for i in valid if b_cases[i].get("pass_all_k")),
                       "a_only": a_only, "b_only": b_only,
                       "mcnemar_p": round(mcnemar_exact(len(a_only), len(b_only)), 4)},
        "per_case": deltas,
    }


def format_comparison(cmp: dict[str, Any]) -> str:
    rl, pk = cmp["run_level"], cmp["pass_all_k"]
    lines = [
        f"paired cases: {cmp['paired_cases']} ({cmp['valid_cases']} valid in both arms); "
        f"invalid runs A={cmp['invalid_runs']['a']} B={cmp['invalid_runs']['b']}",
        f"run-level:  A {rl['a'][0]}/{rl['a'][1]}  B {rl['b'][0]}/{rl['b'][1]}  "
        f"delta {rl['delta']:+.1%}  Fisher p={rl['fisher_p']:.3f}",
        f"pass^k:     A {pk['a']}  B {pk['b']}  A-only {len(pk['a_only'])}  B-only {len(pk['b_only'])}  "
        f"McNemar p={pk['mcnemar_p']:.3f}",
    ]
    if pk["a_only"]:
        lines.append(f"  lost in B:   {', '.join(pk['a_only'])}")
    if pk["b_only"]:
        lines.append(f"  gained in B: {', '.join(pk['b_only'])}")
    lines.append("A p-value above 0.05 is ABSENCE OF EVIDENCE at this n, not evidence of parity: "
                 "with 3 runs per case a real 10-point drop is usually invisible.")
    return "\n".join(lines)
