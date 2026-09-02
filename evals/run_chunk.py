#!/usr/bin/env python3
"""evals/run_chunk.py — run part of a suite and merge into one results file.

The extended suite takes ~45 minutes, which is longer than some execution
environments will keep a single command alive. This driver runs a subset of
cases and merges them into a persistent JSON, so a full arm can be collected
across several shorter invocations WITHOUT restarting the model server
between chunks (the baseline arm was collected on one server; restarting
mid-arm would hand the later cases a fresher backend than the earlier ones —
see the degrading-server protocol in the eval methodology).

Usage:
    python evals/run_chunk.py --list
    python evals/run_chunk.py --cases casual-1,casual-2 --runs 3 --out results/x.json
    python evals/run_chunk.py --from 0 --count 8 --runs 3 --out results/x.json
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
    backend_preflight,
    load_live_config,
    print_case_report,
    run_cases,
)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cases", default="")
    ap.add_argument("--from", dest="start", type=int, default=None)
    ap.add_argument("--count", type=int, default=None)
    ap.add_argument("--runs", type=int, default=3)
    ap.add_argument("--out", default="evals/results/chunked_arm.json")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="Config override for this chunk (repeatable); recorded in the results.")
    args = ap.parse_args()

    ids = [c.id for c in EXTENDED_CASES]
    if args.list:
        for i, cid in enumerate(ids):
            print(f"{i:>3}  {cid}")
        return 0

    if args.cases:
        wanted = [c.strip() for c in args.cases.split(",") if c.strip()]
    elif args.start is not None:
        wanted = ids[args.start:args.start + (args.count or 8)]
    else:
        print("give --cases or --from/--count", file=sys.stderr)
        return 2
    selected = [c for c in EXTENDED_CASES if c.id in wanted]
    if not selected:
        print(f"no cases matched {wanted}", file=sys.stderr)
        return 2

    config = load_live_config()
    overrides: dict[str, Any] = {}
    for item in args.set:
        key, _, raw = item.partition("=")
        raw = raw.strip()
        value: Any = (raw.lower() == "true") if raw.lower() in ("true", "false") else (
            int(raw) if raw.lstrip("-").isdigit() else raw)
        config[key.strip()] = value
        overrides[key.strip()] = value
    problem = backend_preflight(config)
    if problem:
        print(f"BACKEND PREFLIGHT FAILED: {problem}", file=sys.stderr)
        return 2

    results = run_cases(config, selected, args.runs)
    print_case_report(results, args.runs)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {}
    if out.exists():
        try:
            payload = json.loads(out.read_text(encoding="utf-8"))
        except Exception:
            payload = {}
    payload.setdefault("suite", "extended_v2")
    payload.setdefault("protocol", config.get("protocol", "v1"))
    payload.setdefault("runs_per_case", args.runs)
    payload.setdefault("model", config.get("model"))
    payload.setdefault("overrides", overrides)
    payload["timestamp"] = time.time()
    payload.setdefault("cases", {}).update(results)
    out.write_text(json.dumps(payload, indent=2, default=str), encoding="utf-8")
    done = len(payload["cases"])
    print(f"\nmerged {len(results)} case(s) -> {out}  ({done}/{len(ids)} cases collected)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
