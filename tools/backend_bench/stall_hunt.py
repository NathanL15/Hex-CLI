"""Stall hunt for the npurun NPU server: repeat the pattern that hung once
during the smoke suite with NPURUN_HTP_POLL=0 — a divergent new conversation
(forces a Genie dialog rebuild) immediately followed by a same-session
follow-up — and count requests slower than --stall seconds.

  python stall_hunt.py --tag poll0 --cycles 40 --stall 30 [--env NPURUN_HTP_POLL=1]

Restarts the server through launcher._start_npurun_server() with any --env
overrides, then appends one JSON line per request to <out>/stall_hunt.jsonl.
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


def nonstream(messages: list[dict]) -> bench.Reply:
    import urllib.request
    r = bench.Reply()
    sid = messages[0]["content"].rsplit("(session ", 1)[-1].rstrip(")")
    payload = {"model": "qwen3-4b", "temperature": 0.1, "max_tokens": 16, "messages": messages, "stream": False,
               "stop": ["<|im_end|>", "<|im_start|>"], "session_id": sid}
    req = urllib.request.Request(bench.BASES["npu"] + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                 method="POST", headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
    r.t0 = time.perf_counter()
    for _ in range(60):
        try:
            with urllib.request.urlopen(req, timeout=400) as resp:
                obj = json.loads(resp.read().decode("utf-8", "replace"))
            r.text = ((obj.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
            break
        except Exception as e:  # noqa: BLE001
            if "429" in repr(e):
                time.sleep(0.3)
                continue
            r.error = repr(e)
            break
    r.t_end = time.perf_counter()
    r.t_first = r.t_end if r.text else 0.0
    return r


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--cycles", type=int, default=40)
    ap.add_argument("--stall", type=float, default=30.0)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab"))
    ap.add_argument("--max-minutes", type=float, default=8.5)
    ap.add_argument("--no-stream", action="store_true", help="use the non-streaming endpoint (the request that hung was stream=false)")
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    t = npu_ab.restart_server()
    print(f"server accepted after {t} s  env={args.env}")
    from hexcli import agent as ag
    sys_prompt = ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query="list files")
    log = (Path(args.out) / "stall_hunt.jsonl").open("a", encoding="utf-8")
    stalls, n, t_end = 0, 0, time.time() + args.max_minutes * 60
    for c in range(args.cycles):
        if time.time() > t_end:
            break
        sid = uuid.uuid4().hex
        m1 = [{"role": "system", "content": sys_prompt + f"\n\n(session {sid})"},
              {"role": "user", "content": f"Count from 1 to 5. ({c})"}]
        call = nonstream if args.no_stream else (lambda m: bench.chat_openai(bench.BASES["npu"], "qwen3-4b", m, 16, sid, 0.1, timeout=400))
        r1 = call(m1)
        m2 = m1 + [{"role": "assistant", "content": r1.text or "1 2 3 4 5"}, {"role": "user", "content": "Now 6 to 10."}]
        r2 = call(m2)
        for i, r in ((1, r1), (2, r2)):
            dt = r.t_end - r.t0
            n += 1
            stall = dt > args.stall or bool(r.error)
            stalls += stall
            log.write(json.dumps({"tag": args.tag, "cycle": c, "req": i, "total_s": round(dt, 2), "ttft_s": round((r.t_first - r.t0), 2) if r.t_first else None,
                                  "empty": not r.text, "error": r.error, "stall": stall}) + "\n")
            log.flush()
            flag = "  STALL" if stall else ""
            print(f"  cycle {c:3d} req {i}: {dt:6.1f}s {'empty' if not r.text else ''}{flag}", flush=True)
    print(f"{args.tag}: {stalls} stalls in {n} requests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
