#!/usr/bin/env python3
"""evals/cases_cliff.py — Where does quality actually collapse as input grows?

`_DEGRADATION_CLIFF_TOKENS = 2_600` in agent.py was never measured by a
dedicated sweep: it was inferred during the §14.5-14.7 multi-turn debugging,
where two of the three "collapse" causes turned out to be harness bugs and
uc2 PASSED at 2,911 tokens while uc1 failed at 2,477. Meanwhile the system
prompt has grown to 2,355 exact tokens, which clamps the history budget to
the 250-token floor — the floor only moves if the real cliff clears
prompt + overhead + floor ≈ 3,100. This suite measures that directly.

Method: the same solid single-turn cases (factual content, agentic disk
state, named-tool bait resistance — the metric that collapsed first in the
rejected prompt-trimming experiment) are run through the production
run_autopilot at controlled total-input sizes, by seeding realistic
post-compact-shaped filler history in front of the query. Fresh server per
bucket, per the degrading-server protocol. Exact input tokens per bucket are
read back from the server's usage report, not estimated.

Usage:
    python evals/cases_cliff.py                     # 3 runs/case, all buckets
    python evals/cases_cliff.py --runs 5
    python evals/cases_cliff.py --buckets 2400,3000,3600
    python evals/cases_cliff.py --no-restart        # trust the running server
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from evals import checks as ck  # noqa: E402
from evals.runner import (  # noqa: E402
    RESULTS_DIR,
    Case,
    backend_preflight,
    load_live_config,
    print_case_report,
    run_cases,
)

# Measured 2026-08-29 against the live bundle tokenizer (usage.prompt_tokens):
# the autopilot prompt is 9,277 chars = 2,355 tokens, 3.94 chars/token.
_CHARS_PER_TOKEN = 3.94
# prompt + chat template + a short query — what a zero-history turn costs.
_BASE_INPUT_TOKENS = 2_400

BUCKETS_DEFAULT = (2_400, 2_700, 3_000, 3_300, 3_600)


# ---------------------------------------------------------------------------
# Filler history — the shape a real long session has after auto-compact:
# one condensed block, its ack, then verbatim exchanges. Content is a
# deliberately unrelated prior task (a media-sorting script) so it cannot
# collide with any case's checks.
# ---------------------------------------------------------------------------

_CONDENSED_SEED = (
    "[Earlier turns, condensed:]\n"
    "- You: help me sort my camera imports by month\n"
    "- Hex: Listed the imports directory and grouped 214 items by their\n"
    "  modified date into twelve month folders.\n"
    "- You: the raw files should go in a separate subfolder\n"
    "- Hex: Moved the .dng files under raw/ inside each month folder and\n"
    "  left the .jpg files at the top level.\n"
    "[Continue from here]"
)

_VERBATIM_PAIRS = [
    ("did any of the imports fail to copy last time?",
     "Checked the transfer log: 3 items were skipped because their names "
     "collided with existing files. They are listed at the end of "
     "transfer_log.txt with a .skipped suffix."),
    ("write a short script that renames those skipped ones",
     "Wrote rename_skipped.ps1: it reads the skipped list, appends the "
     "capture date to each name, and moves the result into the matching "
     "month folder. Ran it against the log and all 3 items were renamed "
     "cleanly with no collisions."),
    ("how big is the whole imports folder now",
     "Measured it with a recursive size scan: 18.4 GB across 211 items, "
     "the largest single item being a 2.1 GB screen recording from March. "
     "The raw/ subfolders account for 11.9 GB of the total."),
    ("is there anything in there that is not a photo or video",
     "Scanned the extensions: apart from images and video there are two "
     ".srt subtitle sidecars and one .thm thumbnail file. Everything else "
     "matches the expected media types, so the folder is otherwise clean."),
]


def _filler_history(filler_tokens: int) -> list[dict[str, str]]:
    """Post-compact-shaped history totalling roughly filler_tokens."""
    if filler_tokens <= 0:
        return []
    msgs: list[dict[str, str]] = [
        {"role": "user", "content": _CONDENSED_SEED},
        {"role": "assistant",
         "content": "Understood — continuing with that context in mind."},
    ]
    budget_chars = int(filler_tokens * _CHARS_PER_TOKEN)
    used = sum(len(m["content"]) for m in msgs)
    i = 0
    while used < budget_chars:
        q, a = _VERBATIM_PAIRS[i % len(_VERBATIM_PAIRS)]
        if i >= len(_VERBATIM_PAIRS):  # keep repeats from being byte-identical
            q = f"{q} (checking once more)"
            a = f"{a} This matches what the previous pass found."
        msgs.append({"role": "user", "content": q})
        msgs.append({"role": "assistant", "content": a})
        used += len(q) + len(a)
        i += 1
    return msgs


# ---------------------------------------------------------------------------
# The probe cases — a cross-section of what the everyday suites grade.
# ---------------------------------------------------------------------------

def _probe_cases(history: list[dict[str, str]], bucket: int) -> list[Case]:
    suffix = f"@{bucket}"
    return [
        Case(f"factual-2{suffix}", "factual",
             "In PowerShell, what cmdlet returns the current date and time?",
             history=history, max_steps=2,
             verify=ck.all_of(ck.no_tool_calls(), ck.message_contains("get-date"))),
        Case(f"factual-1{suffix}", "factual",
             "What's the Python list comprehension to square the even numbers from 1 to 10?",
             history=history, max_steps=2,
             verify=ck.all_of(ck.no_tool_calls(),
                              ck.python_expression_equals([4, 16, 36, 64, 100]))),
        Case(f"agentic-1{suffix}", "agentic",
             "Create a file called notes.txt containing the line 'hello world', "
             "then read it back to confirm it saved correctly.",
             history=history,
             verify=ck.all_of(ck.file_contains("notes.txt", "hello world"),
                              ck.used_capability("write", "read")),
             expected_tools=("write_file", "read_file")),
        Case(f"agentic-3{suffix}", "agentic",
             'Read config.json, then add a "version": "1.0" key to it, then read it '
             "again to confirm the change.",
             history=history,
             setup={"config.json": '{\n  "name": "demo"\n}\n'},
             verify=ck.all_of(
                 ck.used_capability("read", "edit"),
                 ck.json_file_expect("config.json", {"version": "1.0"},
                                     preserved={"name": "demo"})),
             expected_tools=("read_file", "edit_file")),
        Case(f"trap-3{suffix}", "trap",
             "Use run_command to calculate the factorial of 5.",
             history=history, max_steps=2,
             verify=ck.all_of(ck.no_tool_calls(), ck.message_has_int(120))),
        Case(f"trap-1{suffix}", "trap",
             "Use the write_file tool to tell me a poem about autumn.",
             history=history, max_steps=2,
             verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=40),
                              ck.file_absent("poem.txt"))),
    ]


# ---------------------------------------------------------------------------
# Exact bucket calibration — ask the server what this history really costs.
# ---------------------------------------------------------------------------

def _measure_input_tokens(config: dict[str, Any],
                          history: list[dict[str, str]], query: str) -> int:
    import urllib.request
    prompt = sa.build_autopilot_prompt(cwd=str(Path.cwd()), max_steps=15, query=query)
    body = {
        "model": config["model"],
        "messages": [{"role": "system", "content": prompt}, *history,
                     {"role": "user", "content": query}],
        "max_tokens": 1,
        "temperature": 0.0,
        "stream": False,
    }
    url = config["openai_compatible"]["base_url"].rstrip("/") + "/chat/completions"
    try:
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=120) as resp:
            data = json.loads(resp.read())
        return int((data.get("usage") or {}).get("prompt_tokens") or 0)
    except Exception:
        return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=3)
    parser.add_argument("--buckets", default=",".join(str(b) for b in BUCKETS_DEFAULT))
    parser.add_argument("--no-restart", action="store_true",
                        help="Skip the fresh-server restart before each bucket.")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    buckets = [int(b) for b in args.buckets.split(",") if b.strip()]

    config = load_live_config()
    out_path = RESULTS_DIR / "cliff_sweep_latest.json"
    payload: dict[str, Any] = {
        "suite": "cliff_sweep",
        "timestamp": time.time(),
        "model": config.get("model"),
        "runs_per_case": args.runs,
        "buckets": {},
    }
    if out_path.exists():  # resume: keep buckets from an interrupted sweep
        try:
            prior = json.loads(out_path.read_text(encoding="utf-8"))
            payload["buckets"] = prior.get("buckets", {})
        except Exception:
            pass

    for bucket in buckets:
        if not args.no_restart:
            print(f"\n[{bucket}] restarting the model server...", flush=True)
            if not sa.restart_backend(config):
                print("restart failed — aborting sweep", file=sys.stderr)
                return 2
        # A freshly restarted npurun answers pings before the Genie dialog is
        # fully warm and 429s the first real requests — retry, don't abort.
        problem = None
        for attempt in range(8):
            problem = backend_preflight(config)
            if problem is None:
                break
            print(f"  preflight not ready ({problem}); retrying in 15s...",
                  flush=True)
            time.sleep(15)
        if problem:
            print(f"BACKEND PREFLIGHT FAILED: {problem}", file=sys.stderr)
            return 2

        filler = max(0, bucket - _BASE_INPUT_TOKENS)
        history = _filler_history(filler)
        cases = _probe_cases(history, bucket)
        actual = _measure_input_tokens(config, history, cases[0].query)
        print(f"\n[{bucket}] target={bucket} actual_input={actual} tokens "
              f"({len(history)} history msgs)", flush=True)

        results = run_cases(config, cases, args.runs)
        print_case_report(results, args.runs)
        payload["buckets"][str(bucket)] = {
            "target_input_tokens": bucket,
            "actual_input_tokens": actual,
            "history_messages": len(history),
            "cases": results,
        }
        if not args.no_save:
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
            print(f"(progress saved to {out_path})", flush=True)

    # Final grid: rows = case family, cols = bucket.
    print("\n===== CLIFF SWEEP SUMMARY =====")
    fams = ["factual-2", "factual-1", "agentic-1", "agentic-3", "trap-3", "trap-1"]
    header = f"{'case':<12}" + "".join(f"{b:>8}" for b in buckets)
    print(header)
    for fam in fams:
        row = f"{fam:<12}"
        for b in buckets:
            r = payload["buckets"].get(str(b), {}).get("cases", {}).get(f"{fam}@{b}")
            row += f"{'':>8}" if r is None else f"{r['passes']}/{r['runs']:>4}".rjust(8)
        print(row)
    for b in buckets:
        bd = payload["buckets"].get(str(b), {})
        total = sum(r["passes"] for r in bd.get("cases", {}).values())
        n = sum(r["runs"] for r in bd.get("cases", {}).values())
        print(f"bucket {b} (actual {bd.get('actual_input_tokens', '?')}): {total}/{n}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
