"""Decode speed and short-reply latency as a function of live context length.

Restarts the production server (launcher env: polling off, Rewind), then for
each context size sends a fresh conversation (unique filler system prompt of N
tokens + a short user turn) twice: first a 128-token generation to measure the
decode rate, then a Hex-shaped 32-token reply on the now-warm prefix to measure
what a typical agent step costs at that context. Writes one JSON line per
request to docs/backend_study/data_npu_ab/decode_vs_context.jsonl.

  python decode_vs_context.py --tag v261 --sizes 250,500,...,3500 --reps 2
"""
from __future__ import annotations

import argparse
import json
import os
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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--sizes", default="250,500,750,1000,1250,1500,1750,2000,2250,2500,2750,3000,3250,3500")
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--long", type=int, default=128)
    ap.add_argument("--short", type=int, default=32)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab/decode_vs_context.jsonl"))
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    if not args.no_restart:
        print(f"server accepted after {npu_ab.restart_server()} s")
    gguf = bench.gguf_path("qwen3:4b-instruct-2507-q4_K_M")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    fh = out.open("a", encoding="utf-8")
    sizes = [int(s) for s in args.sizes.split(",")]
    print("size   rep  kind   in_tok  ttft   gen  decode_tok/s  total_s")
    for rep in range(args.reps):
        for n in sizes:
            filler = bench.make_text(max(n - 40, 20), gguf, seed=n * 13 + rep)
            system = "You are a helpful assistant. Background notes:\n" + filler
            sid = uuid.uuid4().hex
            for kind, mt, user in (
                ("long", args.long, "Write a short story about a lighthouse keeper. Keep going until you are cut off."),
                ("short", args.short, "Reply with exactly this JSON and nothing else: {\"action\":\"finish\",\"message\":\"The lighthouse keeper's story is about solitude, routine and the sea.\"}"),
            ):
                msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
                in_tok = bench.count_tokens(bench.chatml(msgs), gguf)
                r = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, mt, sid, 0.1, timeout=300)
                gen = bench.count_tokens(r.text, gguf) if r.text else 0
                ttft = (r.t_first - r.t0) if r.t_first else None
                dec = (gen - 1) / (r.t_end - r.t_first) if (gen > 8 and r.t_first) else None
                rec = {"tag": args.tag, "rep": rep, "size": n, "kind": kind, "in_tok": in_tok, "ttft_s": round(ttft, 3) if ttft else None,
                       "gen_tok": gen, "decode_tok_s": round(dec, 2) if dec else None, "total_s": round(r.t_end - r.t0, 3),
                       "empty": not r.text, "error": r.error}
                fh.write(json.dumps(rec) + "\n")
                fh.flush()
                print(f"{n:5d}  {rep:3d}  {kind:5s} {in_tok:6d}  {rec['ttft_s']}  {gen:4d}  {rec['decode_tok_s']}  {rec['total_s']}", flush=True)
                time.sleep(0.5)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
