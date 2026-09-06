"""Run Hex's smoke suite case by case against the NPU server with timestamps,
per-case result files and counters, as a regression gate.

  python smoke_gate.py --tag fork022b [--env NPURUN_HTP_POLL=1] [--runs 2] [--no-restart]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import npu_ab  # noqa: E402

CASES = ["factual-1", "casual-1", "agentic-2", "casual-3", "agentic-1", "agentic-3", "factual-3", "casual-2", "factual-2", "lint-1"]
RESULTS = REPO / "evals/results/smoke_v2_results.json"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--runs", default="2")
    ap.add_argument("--seed", default="20260905")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab"))
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--cases", default=",".join(CASES))
    ap.add_argument("--suite", default="smoke", help="smoke | extended (cases_<suite>.py, <suite>_v2_results.json)")
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    global RESULTS
    RESULTS = REPO / f"evals/results/{args.suite}_v2_results.json"
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    backup = out / f"{args.suite}_v2_results.backup.json"
    if RESULTS.exists() and not backup.exists():
        shutil.copy2(RESULTS, backup)
    if not args.no_restart:
        print(f"server accepted after {npu_ab.restart_server()} s  env={args.env}")
    stamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    tp = subprocess.Popen(["typeperf", "-si", "1", "-y", "-o", str(out / f"counters_npu_smoke_{args.tag}_{stamp}.csv")] + bench.COUNTERS,
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    log = (out / f"smoke_{args.tag}_cases.jsonl").open("a", encoding="utf-8")
    tot_pass = tot_runs = tot_invalid = 0
    slow = []
    try:
        for c in args.cases.split(","):
            ts = time.time()
            subprocess.run([sys.executable, f"evals/cases_{args.suite}.py", "--case", c, "--runs", args.runs, "--seed", args.seed],
                               capture_output=True, text=True, timeout=1800, cwd=str(REPO))
            te = time.time()
            res = json.load(RESULTS.open(encoding="utf-8"))
            r = res["cases"][c]
            (out / f"smoke_{args.tag}_{c}.json").write_text(json.dumps(res), encoding="utf-8")
            # slowest LLM call inside the traces, if the trace carries step latencies
            steps = []
            for tr in r.get("traces", []):
                for st_ in tr.get("steps", []) or []:
                    if isinstance(st_, dict) and st_.get("llm_latency_s") is not None:
                        steps.append(st_["llm_latency_s"])
            rec = {"tag": args.tag, "case": c, "t_start": ts, "t_end": te, "wall_s": round(te - ts, 1),
                   "passes": r.get("passes"), "runs": r.get("runs"), "invalid_runs": r.get("invalid_runs"),
                   "invalid_details": [str(x)[:160] for x in r.get("invalid_details", [])],
                   "fail_details": [str(x)[:160] for x in r.get("fail_details", [])],
                   "first_llm_latency_s": r.get("mean_first_llm_latency_s"), "max_step_llm_s": max(steps) if steps else None}
            log.write(json.dumps(rec) + "\n")
            log.flush()
            tot_pass += r.get("passes") or 0
            tot_runs += r.get("runs") or 0
            tot_invalid += r.get("invalid_runs") or 0
            if te - ts > 90:
                slow.append((c, round(te - ts, 1)))
            print(f"  {c:12s} wall={te - ts:6.1f}s pass {r.get('passes')}/{r.get('runs')} invalid {r.get('invalid_runs')} "
                  f"1stLLM {r.get('mean_first_llm_latency_s')} {rec['fail_details'][:1]}{rec['invalid_details'][:1]}", flush=True)
    finally:
        tp.terminate()
        subprocess.run(["taskkill", "/F", "/IM", "typeperf.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if backup.exists():
            shutil.copy2(backup, RESULTS)
    print(f"{args.tag}: {tot_pass}/{tot_runs} valid passes, {tot_invalid} invalid, slow cases: {slow}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
