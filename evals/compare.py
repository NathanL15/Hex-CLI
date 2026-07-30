#!/usr/bin/env python3
"""evals/compare.py — Compare two eval-v2 result files (e.g. v1 baseline vs
protocol-v2 A/B) case by case.

Usage:
    python evals/compare.py results/extended_v2_results_baseline_v1.json \
                            results/extended_v2_results_protocol_v2.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")


def _summarise(cases: dict[str, Any]) -> tuple[int, int, int]:
    total = perfect = at_least_one = 0
    for r in cases.values():
        if not r.get("runs"):
            continue
        total += 1
        if r.get("pass_all_k"):
            perfect += 1
        if r.get("pass_at_k"):
            at_least_one += 1
    return total, perfect, at_least_one


def compare(a_path: Path, b_path: Path) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    a_cases: dict[str, Any] = a.get("cases", {})
    b_cases: dict[str, Any] = b.get("cases", {})
    a_label = f"{a.get('protocol') or 'v1'}"
    b_label = f"{b.get('protocol') or 'v1'}"

    print(f"\nA = {a_path.name} (protocol {a_label}, {a.get('runs_per_case')} runs/case)")
    print(f"B = {b_path.name} (protocol {b_label}, {b.get('runs_per_case')} runs/case)\n")

    print(f"{'case':<26}{'A pass':<10}{'B pass':<10}{'Δ':<8}{'A 1stLLM':<10}{'B 1stLLM':<10}{'verdict'}")
    print("-" * 86)
    improved = regressed = unchanged = 0
    for cid in sorted(set(a_cases) | set(b_cases)):
        ra, rb = a_cases.get(cid), b_cases.get(cid)
        if ra is None or rb is None:
            side = "A" if rb is None else "B"
            print(f"{cid:<26}{'—':<10}{'—':<10}{'':<8}{'':<10}{'':<10}only in {side}")
            continue
        pa = f"{ra['passes']}/{ra['runs']}" if ra["runs"] else "invalid"
        pb = f"{rb['passes']}/{rb['runs']}" if rb["runs"] else "invalid"
        rate_a = ra["passes"] / ra["runs"] if ra["runs"] else None
        rate_b = rb["passes"] / rb["runs"] if rb["runs"] else None
        if rate_a is None or rate_b is None:
            verdict, delta = "n/a", ""
        elif rate_b > rate_a:
            verdict, delta = "IMPROVED", f"+{rate_b - rate_a:.1f}"
            improved += 1
        elif rate_b < rate_a:
            verdict, delta = "REGRESSED", f"{rate_b - rate_a:.1f}"
            regressed += 1
        else:
            verdict, delta = "", "0"
            unchanged += 1
        la = ra.get("mean_first_llm_latency_s")
        lb = rb.get("mean_first_llm_latency_s")
        print(f"{cid:<26}{pa:<10}{pb:<10}{delta:<8}{la!s:<10}{lb!s:<10}{verdict}")

    ta, pa_, aa = _summarise(a_cases)
    tb, pb_, ab = _summarise(b_cases)
    print("\nSummary:")
    print(f"  A: {pa_}/{ta} pass^k, {aa}/{ta} pass@k")
    print(f"  B: {pb_}/{tb} pass^k, {ab}/{tb} pass@k")
    print(f"  Cases improved: {improved}, regressed: {regressed}, unchanged: {unchanged}")

    lat_a = [r["mean_first_llm_latency_s"] for r in a_cases.values() if r.get("mean_first_llm_latency_s")]
    lat_b = [r["mean_first_llm_latency_s"] for r in b_cases.values() if r.get("mean_first_llm_latency_s")]
    if lat_a and lat_b:
        print(f"  Mean first-LLM latency: A {sum(lat_a)/len(lat_a):.1f}s → B {sum(lat_b)/len(lat_b):.1f}s")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    args = parser.parse_args()
    return compare(args.a, args.b)


if __name__ == "__main__":
    raise SystemExit(main())
