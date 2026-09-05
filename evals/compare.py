#!/usr/bin/env python3
"""evals/compare.py — Compare two eval-v2 result files case by case, with the
significance tests a verdict needs (evals/stats.py): Fisher exact on the
run-level pass rates and McNemar exact on the paired pass^k verdicts.

Usage:
    python evals/compare.py results/window_r3.json results/candidate.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.stats import compare_arms, format_comparison  # noqa: E402

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


def _arm_line(label: str, path: Path, d: dict[str, Any]) -> str:
    meta = d.get("metadata") or {}
    server = meta.get("server") or {}
    bits = [f"protocol {d.get('protocol') or 'v1'}", f"{d.get('runs_per_case')} runs/case"]
    if meta.get("git_sha"):
        bits.append(f"git {meta['git_sha']}{'+' if meta.get('git_dirty') else ''}")
    if meta.get("npurun"):
        bits.append(meta["npurun"])
    if server.get("input_token_budget"):
        bits.append(f"budget {server['input_token_budget']}")
    if d.get("seed") is not None:
        bits.append(f"seed {d['seed']}")
    if d.get("overrides"):
        bits.append(f"overrides {d['overrides']}")
    canary = d.get("canary_s") or {}
    if canary.get("start") and canary.get("end"):
        bits.append(f"canary {canary['start']:.1f}s→{canary['end']:.1f}s")
    return f"{label} = {path.name} ({', '.join(bits)})"


def compare(a_path: Path, b_path: Path) -> int:
    a = json.loads(a_path.read_text(encoding="utf-8"))
    b = json.loads(b_path.read_text(encoding="utf-8"))
    a_cases: dict[str, Any] = a.get("cases", {})
    b_cases: dict[str, Any] = b.get("cases", {})

    print()
    print(_arm_line("A", a_path, a))
    print(_arm_line("B", b_path, b))
    print()

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

    if a_cases and b_cases:
        print("\nSignificance:")
        for line in format_comparison(compare_arms(a_cases, b_cases)).splitlines():
            print(f"  {line}")
    if a.get("scenarios") and b.get("scenarios"):
        _compare_scenarios(a, b)
    return 0


def _compare_scenarios(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Turn-level table for multi-turn results: pass counts and the
    first-response latency per turn (the number a prewarm A/B is about),
    then Fisher / McNemar over all turns as if each were a case."""
    a_turns: dict[str, Any] = {}
    b_turns: dict[str, Any] = {}
    for sid, sc in a.get("scenarios", {}).items():
        for tid, r in sc.get("turns", {}).items():
            a_turns[f"{sid}/{tid}"] = r
    for sid, sc in b.get("scenarios", {}).items():
        for tid, r in sc.get("turns", {}).items():
            b_turns[f"{sid}/{tid}"] = r
    print(f"\nTurns  (think time A {a.get('think_time_s', 0)}s, B {b.get('think_time_s', 0)}s)")
    print(f"{'turn':<16}{'A pass':<9}{'B pass':<9}{'A 1stLLM':<11}{'B 1stLLM':<11}{'Δ lat':<9}{'A retries':<11}{'B retries'}")
    print("-" * 90)
    lats: list[tuple[str, float, float]] = []
    for tid in sorted(set(a_turns) | set(b_turns)):
        ra, rb = a_turns.get(tid), b_turns.get(tid)
        if ra is None or rb is None:
            print(f"{tid:<16}{'—':<9}{'—':<9}only in {'A' if rb is None else 'B'}")
            continue
        la, lb = ra.get("mean_first_llm_latency_s"), rb.get("mean_first_llm_latency_s")
        if la and lb:
            lats.append((tid, la, lb))
        dl = f"{lb - la:+.1f}s" if la and lb else ""
        print(f"{tid:<16}{ra['passes']}/{ra['runs']:<7}{rb['passes']}/{rb['runs']:<7}"
              f"{la!s:<11}{lb!s:<11}{dl:<9}{ra.get('mean_retries')!s:<11}{rb.get('mean_retries')!s}")

    def _mean(rows: list[tuple[str, float, float]]) -> tuple[float, float]:
        return sum(r[1] for r in rows) / len(rows), sum(r[2] for r in rows) / len(rows)

    if lats:
        ma, mb = _mean(lats)
        print(f"\n  mean first-response latency over {len(lats)} turns: A {ma:.1f}s  B {mb:.1f}s  ({mb - ma:+.1f}s)")
        later = [r for r in lats if not r[0].endswith("-t1")]
        if later:
            ma, mb = _mean(later)
            print(f"  turns after the first (where a prewarm can matter): A {ma:.1f}s  B {mb:.1f}s  ({mb - ma:+.1f}s)")
    print("\nSignificance over turns:")
    for line in format_comparison(compare_arms(a_turns, b_turns)).splitlines():
        print(f"  {line}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("a", type=Path)
    parser.add_argument("b", type=Path)
    args = parser.parse_args()
    return compare(args.a, args.b)


if __name__ == "__main__":
    raise SystemExit(main())
