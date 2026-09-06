"""Build the paper's derived tables: per-case smoke-suite wall time + energy, joined
from smoke_<tier>_cases.jsonl and the counters_<tier>_smoke_*.csv files.

  python report_data.py --data docs/backend_study/data --out docs/backend_study/summary
"""
from __future__ import annotations

import argparse
import json
import statistics as st
from pathlib import Path

from analyze import calibrate_energy, energy_j, load_counters, window


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data, out = Path(args.data), Path(args.out)
    rows, _ = load_counters(data)
    scale = calibrate_energy(rows)
    idle_none = None
    for f in sorted({r["file"] for r in rows}):
        if f.startswith("counters_none"):
            w = [r for r in rows if r["file"] == f][5:]
            idle_none = st.fmean([r["sys_w"] for r in w if r.get("sys_w") is not None])
    result = {"energy_scale": scale, "machine_idle_w": idle_none, "tiers": {}}
    for tier in ("npu", "llamacpp", "cpu"):
        p = data / f"smoke_{tier}_cases.jsonl"
        if not p.exists():
            continue
        cases = [json.loads(ln) for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]
        for c in cases:
            w = window(rows, c["t_start"], c["t_end"])
            c["sys_w"] = round(st.fmean([r["sys_w"] for r in w if r.get("sys_w") is not None]), 2) if w else None
            c["cpu_util"] = round(st.fmean([r["cpu_util"] for r in w if r.get("cpu_util") is not None]), 1) if w else None
            c["sys_j"] = energy_j(rows, "sys_e", c["t_start"], c["t_end"], scale)
            c["sys_j"] = round(c["sys_j"], 1) if c["sys_j"] is not None else None
            c["temp_tz2"] = round(st.fmean([r["temp_tz2"] for r in w if r.get("temp_tz2") is not None]), 1) if w else None
            if c.get("first_llm_latency_s") is None and c.get("table_line"):
                parts = c["table_line"].split()
                try:
                    c["first_llm_latency_s"] = float(parts[-2])
                    c["passes"] = int(parts[2].split("/")[0])
                    c["runs"] = int(parts[2].split("/")[1])
                except (ValueError, IndexError):
                    pass
        total_wall = round(sum(c["wall_s"] for c in cases), 1)
        total_j = round(sum(c["sys_j"] or 0 for c in cases), 1)
        passes = sum(c.get("passes") or 0 for c in cases)
        runs = sum(c.get("runs") or 0 for c in cases)
        invalid = sum(c.get("invalid_runs") or (2 - (c.get("runs") or 2)) for c in cases)
        result["tiers"][tier] = {"cases": cases, "total_wall_s": total_wall, "total_sys_j": total_j,
                                 "passes": passes, "valid_runs": runs, "invalid_runs": invalid,
                                 "mean_first_llm_latency_s": round(st.fmean([c["first_llm_latency_s"] for c in cases if c.get("first_llm_latency_s")]), 2)}
    out.mkdir(parents=True, exist_ok=True)
    (out / "smoke_summary.json").write_text(json.dumps(result, indent=1), encoding="utf-8")
    for tier, t in result["tiers"].items():
        print(f"{tier}: wall {t['total_wall_s']} s, energy {t['total_sys_j']} J, passes {t['passes']}/{t['valid_runs']} valid, invalid {t['invalid_runs']}, mean 1st-LLM {t['mean_first_llm_latency_s']} s")
        for c in t["cases"]:
            print(f"   {c['case']:12s} wall {c['wall_s']:6.1f}s  {c.get('passes')}/{c.get('runs')}  1stLLM {c.get('first_llm_latency_s')}  {c['sys_w']} W  {c['sys_j']} J  tz2 {c['temp_tz2']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
