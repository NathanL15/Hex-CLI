"""Stall rate as a function of context length and host polling.

Hex's system prompt is kept cached (prewarm), then each cycle is a fresh
conversation that extends it with a K-token user turn, the shape of a Hex
step that just received a tool result. A cycle that returns nothing, errors,
or exceeds --stall-after seconds counts as a stall; the server is restarted
(instead of waiting out the wedge) and the run continues.

  python stall_rate.py --tag off_3k --user-tokens 800 --n 40 [--env NPURUN_HTP_POLL=1]
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import npu_ab  # noqa: E402


def prewarm(prompt: str, force: bool) -> float:
    body = json.dumps({"messages": [{"role": "system", "content": prompt}], "force": force}).encode()
    t0 = time.time()
    while time.time() - t0 < 120:
        try:
            req = urllib.request.Request(bench.BASES["npu"].rstrip("/") + "/npurun/prewarm", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
            with urllib.request.urlopen(req, timeout=120):
                return round(time.time() - t0, 2)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return -1.0
        except Exception:  # noqa: BLE001
            return -1.0
        time.sleep(0.3)
    return -2.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--user-tokens", type=int, default=800)
    ap.add_argument("--n", type=int, default=40)
    ap.add_argument("--max-tokens", type=int, default=24)
    ap.add_argument("--stall-after", type=float, default=45.0)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab/stall_rate.jsonl"))
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    from hexcli import agent as ag
    ag._ACTIVE_CONFIG = {"prompt_stable_prefix": True, "conditional_rules": False}
    S = ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query="")
    gguf = bench.gguf_path("qwen3:4b-instruct-2507-q4_K_M")
    fh = Path(args.out).open("a", encoding="utf-8")
    print(f"server accepted after {npu_ab.restart_server()} s env={args.env}")
    prewarm(S, True)
    stalls = 0
    walls: list[float] = []
    t_run = time.time()
    for i in range(args.n):
        user = ("TOOL RESULT (read_file):\n" + bench.make_text(args.user_tokens, gguf, seed=7000 + i)
                + f"\n\nSummarise that in one sentence. ({uuid.uuid4().hex[:6]})")
        msgs = [{"role": "system", "content": S}, {"role": "user", "content": user}]
        in_tok = bench.count_tokens(bench.chatml(msgs), gguf)
        t0 = time.time()
        r = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, args.max_tokens, uuid.uuid4().hex, 0.1,
                              timeout=int(args.stall_after) + 30)
        wall = time.time() - t0
        stalled = (not r.text) or bool(r.error) or wall > args.stall_after
        rec = {"tag": args.tag, "env": args.env, "i": i, "in_tok": in_tok, "wall_s": round(wall, 2),
               "ttft_s": round(r.t_first - r.t0, 2) if r.t_first else None, "gen": len(r.text or ""),
               "busy_retries": r.busy_retries, "stall": stalled, "error": r.error[:80]}
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        print(f"  [{i:2d}] in={in_tok} wall={wall:6.2f} ttft={rec['ttft_s']} busy={r.busy_retries} {'STALL' if stalled else ''}", flush=True)
        if stalled:
            stalls += 1
            print(f"       restarting server ({npu_ab.restart_server()} s)", flush=True)
            prewarm(S, True)
        else:
            walls.append(wall)
            # Hex's end-of-turn prewarm (no force): the server rebuilds and
            # re-prefills the system prompt when the cache is long.
            pw = prewarm(S, False)
            print(f"       prewarm {pw} s", flush=True)
    summary = {"tag": args.tag, "env": args.env, "n": args.n, "stalls": stalls, "user_tokens": args.user_tokens,
               "wall_med_s": round(sorted(walls)[len(walls) // 2], 2) if walls else None, "elapsed_s": round(time.time() - t_run)}
    fh.write(json.dumps({"summary": summary}) + "\n")
    print(json.dumps(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
