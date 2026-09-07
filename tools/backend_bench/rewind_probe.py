"""Rewind semantics probe for the npurun NPU server (NPURUN_REWIND=2).

Which transcript shapes reuse the KV cache, which fall back to a rebuild, and
what each costs. Every step is a real chat request; the server log is scanned
for "Rewind query failed" / "dialog created" between steps.

  python rewind_probe.py --tag probe1 [--env NPURUN_REWIND_MAX_CACHED=0] [--reps 3]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import npu_ab  # noqa: E402

LOG = REPO / "npurun_server.log"


def log_events(since: int) -> tuple[str, int]:
    raw = LOG.read_bytes()
    chunk = raw[since:].decode("utf-8", "replace")
    chunk = re.sub(r"\x1b\[[0-9;]*m", "", chunk)
    ev = []
    for line in chunk.splitlines():
        if "Rewind query failed" in line or "returned no tokens" in line:
            ev.append("REWIND-FAIL")
        elif "dialog created" in line:
            ev.append("REBUILD")
        elif "streaming inference failed" in line:
            ev.append("ERR")
        elif "prewarm" in line:
            ev.append("PREWARM")
    return ",".join(ev) or "-", len(raw)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--reps", type=int, default=3)
    ap.add_argument("--ks", default="100,300,600,1000,1500", help="discard sizes for --mode distance")
    ap.add_argument("--mode", default="shapes", choices=["shapes", "distance", "distance2", "extend"])
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab"))
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    print(f"server accepted after {npu_ab.restart_server()} s env={args.env}")
    from hexcli import agent as ag
    S = ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query="list files")
    S2 = S.replace("2026-09", "2026-10", 1)  # early divergence: a different date in the prompt
    gguf = bench.gguf_path("qwen3:4b-instruct-2507-q4_K_M")
    out = (Path(args.out) / "rewind_probe.jsonl").open("a", encoding="utf-8")
    pos = LOG.stat().st_size if LOG.exists() else 0

    def step(name: str, msgs: list[dict], sid: str, rep: int) -> str:
        nonlocal pos
        r = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, 24, sid, 0.1, timeout=400)
        time.sleep(0.3)
        ev, pos = log_events(pos)
        ttft = (r.t_first - r.t0) if r.t_first else None
        rec = {"tag": args.tag, "rep": rep, "step": name, "prompt_tokens": bench.count_tokens(bench.chatml(msgs), gguf),
               "ttft_s": round(ttft, 2) if ttft else None, "total_s": round(r.t_end - r.t0, 2), "empty": not r.text,
               "events": ev}
        out.write(json.dumps(rec) + "\n")
        out.flush()
        print(f"  [{name:28s}] in={rec['prompt_tokens']:5d} ttft={rec['ttft_s']} {'EMPTY' if not r.text else ''} events={ev}", flush=True)
        return r.text

    if args.mode == "distance":
        # cache = S + U_k + A; then S + short  → rewind distance ≈ k + |A|
        for rep in range(args.reps):
            for k in [int(x) for x in args.ks.split(',')]:
                u = uuid.uuid4().hex[:6]
                filler = bench.make_text(k, gguf, seed=k * 7 + rep)
                step(f"D{k:4d}a S+U{k} (set cache)", [{"role": "system", "content": S}, {"role": "user", "content": filler + "\n\nSay OK. (" + u + ")"}], uuid.uuid4().hex, rep)
                step(f"D{k:4d}b S+short (discard {k})", [{"role": "system", "content": S}, {"role": "user", "content": f"Name a colour. ({u})"}], uuid.uuid4().hex, rep)
        return 0
    if args.mode == "distance2":
        # same as distance but with a ~900-token prefix so the cache stays well under 3K
        words = S.split()
        Sshort = " ".join(words[: len(words) // 3])
        for rep in range(args.reps):
            for k in (1000, 1500, 2000):
                u = uuid.uuid4().hex[:6]
                filler = bench.make_text(k, gguf, seed=k * 11 + rep)
                step(f"S{k:4d}a Ss+U{k} (set cache)", [{"role": "system", "content": Sshort}, {"role": "user", "content": filler + "\n\nSay OK. (" + u + ")"}], uuid.uuid4().hex, rep)
                step(f"S{k:4d}b Ss+short (discard {k})", [{"role": "system", "content": Sshort}, {"role": "user", "content": "Name a colour. (" + u + ")"}], uuid.uuid4().hex, rep)
        return 0
    if args.mode == "extend":
        # extend one conversation by ~300-token steps until Rewind fails
        u = uuid.uuid4().hex[:6]
        sid = uuid.uuid4().hex
        msgs = [{"role": "system", "content": S}, {"role": "user", "content": f"Count from 1 to 5. ({u})"}]
        for i in range(6):
            a = step(f"E{i} extend ({bench.count_tokens(bench.chatml(msgs), gguf)} tok)", msgs, sid, 0)
            msgs = msgs + [{"role": "assistant", "content": a or "OK"}, {"role": "user", "content": "TOOL RESULT:\n" + bench.make_text(280, gguf, seed=500 + i) + "\n\nContinue briefly."}]
        return 0
    for rep in range(args.reps):
        u = uuid.uuid4().hex[:6]
        sid = uuid.uuid4().hex
        a1 = step("1 S+U1 (new conv)", [{"role": "system", "content": S}, {"role": "user", "content": f"Count from 1 to 5. ({u})"}], sid, rep)
        step("2 S+U1+A1+U2 (extend)", [{"role": "system", "content": S}, {"role": "user", "content": f"Count from 1 to 5. ({u})"},
                                      {"role": "assistant", "content": a1 or "1 2 3 4 5"}, {"role": "user", "content": "Now 6 to 10."}], sid, rep)
        sid2 = uuid.uuid4().hex
        step("3 S+U3 (diverge at user)", [{"role": "system", "content": S}, {"role": "user", "content": f"Name three colours. ({u})"}], sid2, rep)
        sid3 = uuid.uuid4().hex
        step("4 S'+U3 (diverge early)", [{"role": "system", "content": S2}, {"role": "user", "content": f"Name three colours. ({u})"}], sid3, rep)
        sid4 = uuid.uuid4().hex
        step("5 tiny prompt (cache->short)", [{"role": "user", "content": f"Say OK. ({u})"}], sid4, rep)
        sid5 = uuid.uuid4().hex
        step("6 S+U1 after short cache", [{"role": "system", "content": S}, {"role": "user", "content": f"Count from 1 to 5. ({u}b)"}], sid5, rep)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
