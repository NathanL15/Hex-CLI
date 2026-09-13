#!/usr/bin/env python3
"""evals/replay_edits.py — replay saved edit_file attempts through the applier.

The offline check for any change to `protocol_v2.apply_search_replace`: every
edit_file call recorded in the saved multi-turn traces for one turn is run
again against that turn's fixture, and the outcome is graded against what the
turn wanted. No model, no server; seconds, not hours. Run it before a live arm.

    python evals/replay_edits.py            # both turns
    python evals/replay_edits.py --turn uc1-t3

uc1-t3 (fix appned -> append): FIXED means the output equals the fixture with
exactly that fix and nothing else; WRONG is any other successful edit.
uc1-t4 (add a median key): the fixture already has t3's fix; APPLIED means the
edit landed and the file still parses without a harness-made `average` name;
HARMFUL is a successful edit that does not parse or installs that name.
Both must report 0 wrong / 0 harmful; the fixed and applied counts are the
lever's offline estimate.
"""
from __future__ import annotations

import argparse
import ast
import glob
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals.cases_multiturn import PROCESSOR_PY  # noqa: E402
from hexcli import protocol_v2 as p2  # noqa: E402

RESULTS = Path(__file__).resolve().parent / "results"
T3_FIXED = PROCESSOR_PY.replace("result.appned(item * 2)", "result.append(item * 2)")


def _edit_calls(o):
    if isinstance(o, dict):
        if o.get("tool") == "edit_file" and isinstance(o.get("args"), dict):
            yield o
        for v in o.values():
            yield from _edit_calls(v)
    elif isinstance(o, list):
        for v in o:
            yield from _edit_calls(v)


def _turn_records(o, turn):
    if isinstance(o, dict):
        for k, v in o.items():
            if k == turn:
                yield v
            else:
                yield from _turn_records(v, turn)
    elif isinstance(o, list):
        for v in o:
            yield from _turn_records(v, turn)


def _attempts(turn: str, errors_only: bool) -> list[tuple[str, str]]:
    seen: list[tuple[str, str]] = []
    for f in sorted(glob.glob(str(RESULTS / "multiturn*.json"))):
        try:
            d = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        for rec in _turn_records(d, turn):
            for c in _edit_calls(rec):
                if errors_only and c.get("status") != "error":
                    continue
                a = c["args"]
                key = (a.get("old_string") or "", a.get("new_string") or "")
                if key[0] and key[0] != key[1] and key not in seen:
                    seen.append(key)
    return seen


def replay_t3(verbose: bool) -> tuple[int, int, int]:
    fixed = still = wrong = 0
    for old, new in _attempts("uc1-t3", errors_only=True):
        out, err = p2.apply_search_replace(PROCESSOR_PY, [(old, new)])
        if err:
            still += 1
            tag = "STILL"
        elif out == T3_FIXED:
            fixed += 1
            tag = "FIXED"
        else:
            wrong += 1
            tag = "WRONG"
        if verbose or tag == "WRONG":
            print(f"  {tag:5} {old[:60]!r} -> {new[:40]!r}")
    return fixed, still, wrong


def replay_t4(verbose: bool) -> tuple[int, int, int]:
    applied = refused = harmful = 0
    for old, new in _attempts("uc1-t4", errors_only=False):
        if T3_FIXED.count(old) == 1:
            continue  # exact matches are not the applier's problem
        out, err = p2.apply_search_replace(T3_FIXED, [(old, new)])
        if err:
            refused += 1
            tag = "REFUSED"
        else:
            try:
                ast.parse(out)
                parses = True
            except SyntaxError:
                parses = False
            changed = [ln for ln in out.splitlines() if ln not in T3_FIXED.splitlines()]
            bad = not parses or any('"average": average' in ln for ln in changed)
            if bad:
                harmful += 1
                tag = "HARMFUL"
            else:
                applied += 1
                tag = "APPLIED"
        if verbose or tag == "HARMFUL":
            print(f"  {tag:7} {p2.LAST_APPLY_TIER if not err else '':9} {old[:55]!r} -> {new[:45]!r}")
    return applied, refused, harmful


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--turn", choices=["uc1-t3", "uc1-t4"], help="replay one turn only")
    ap.add_argument("-v", "--verbose", action="store_true", help="print every attempt, not only the bad ones")
    args = ap.parse_args()
    bad = 0
    if args.turn in (None, "uc1-t3"):
        fixed, still, wrong = replay_t3(args.verbose)
        print(f"uc1-t3: fixed={fixed} still_error={still} wrong={wrong}")
        bad += wrong
    if args.turn in (None, "uc1-t4"):
        applied, refused, harmful = replay_t4(args.verbose)
        print(f"uc1-t4: applied={applied} refused={refused} harmful={harmful}")
        bad += harmful
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
