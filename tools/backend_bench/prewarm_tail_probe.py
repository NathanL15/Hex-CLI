"""The end-of-turn prewarm policy, measured on Hex's real turn shape.

Each rep plays one Hex turn: the request (system prompt + the stamped user
message) and a multi-step transcript whose tool steps add a `--tail` token
tail to the cache, then the end-of-turn prewarm under one of two policies,
then `--think` seconds, then the next turn's request (system prompt + the
condensed pair + a new user message). What is measured is the next turn's
time to first token: an in-line dialog rebuild when the runtime cannot
Rewind, or a warm extension when the prewarm did its job.

  control:   the pre-2.6.2 prewarm — system prompt only, no force
             (the server rebuilds only when the cache passes ~3,100 tokens)
  candidate: the 2.6.2+ prewarm — system prompt, forced when the turn's
             tail exceeds the 600-token discard limit

  python prewarm_tail_probe.py --tag tail --reps 4 --tail 900 --think 15
"""
from __future__ import annotations

import argparse
import json
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

H = {"Content-Type": "application/json", "Authorization": "Bearer local"}


def prewarm(payload: dict) -> None:
    req = urllib.request.Request((bench.BASES["npu"].rstrip("/") + ("" if bench.BASES["npu"].rstrip("/").endswith("/v1") else "/v1") + "/npurun/prewarm"), data=json.dumps(payload).encode(),
                                 method="POST", headers=H)
    for _ in range(100):
        try:
            with urllib.request.urlopen(req, timeout=60):
                return
        except urllib.error.HTTPError as e:
            if e.code != 429:
                return
            time.sleep(0.3)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--reps", type=int, default=4)
    ap.add_argument("--tail", type=int, default=900, help="tokens of tool steps in the turn's transcript")
    ap.add_argument("--think", type=float, default=15.0)
    ap.add_argument("--history", type=int, default=0, help="tokens of prior (query, answer) history before the turn")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab/prewarm_tail_probe.jsonl"))
    args = ap.parse_args()
    from hexcli import agent as ag
    ag._ACTIVE_CONFIG = {"prompt_stable_prefix": True, "conditional_rules": False}
    S = ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query="")
    gguf = bench.gguf_path("qwen3:4b-instruct-2507-q4_K_M")
    fh = Path(args.out).open("a", encoding="utf-8")
    print(f"server accepted after {npu_ab.restart_server()} s")
    prewarm({"messages": [{"role": "system", "content": S}], "force": True})
    time.sleep(8)
    print("policy     rep  turn-1 wall   prewarm  next-turn first token   next-turn wall")
    for rep in range(args.reps):
        for policy in ("control", "candidate"):
            q = f"List the python files here and count them. ({uuid.uuid4().hex[:6]})"
            stamped = f"Date: 2026-09-07.\n[workspace:dir]\nWorking directory: {REPO}\n\nRequest: {q}"
            sid = uuid.uuid4().hex
            hist: list[dict] = []
            if args.history:
                hist = [{"role": "user", "content": "Earlier question. (" + uuid.uuid4().hex[:6] + ")"},
                        {"role": "assistant", "content": bench.make_text(args.history, gguf, seed=900 + rep)}]
            msgs = [{"role": "system", "content": S}, *hist, {"role": "user", "content": stamped}]
            t0 = time.time()
            r1 = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, 200, sid, 0.1, timeout=120)
            # the turn's tool steps, as Hex appends them
            steps = max(1, args.tail // 300)
            step_tokens = max(40, args.tail // steps - 20)
            for i in range(steps):
                msgs = msgs + [{"role": "assistant", "content": r1.text or '{"action":"list_directory","args":{"path":"."}}'},
                               {"role": "user", "content": "TOOL RESULT (list_directory):\n" + bench.make_text(step_tokens, gguf, seed=rep * 10 + i)}]
            r2 = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, 200, sid, 0.1, timeout=120)
            final = r2.text or "Done."
            turn1_wall = time.time() - t0
            tail_est = sum(len(m["content"]) // 4 + 8 for m in msgs[1 + len(hist):]) + len(final) // 4 + 8
            # end of turn: the prewarm policy under test
            tp = time.time()
            if policy == "control":
                prewarm({"messages": [{"role": "system", "content": S}]})
            else:
                payload = {"messages": [{"role": "system", "content": S}]}
                if tail_est > 600:
                    payload["force"] = True
                prewarm(payload)
            prewarm_call = time.time() - tp
            time.sleep(args.think)
            # next turn
            q2 = f"Now tell me which one is largest. ({uuid.uuid4().hex[:6]})"
            msgs2 = [{"role": "system", "content": S}, *hist, {"role": "user", "content": q}, {"role": "assistant", "content": final},
                     {"role": "user", "content": f"Date: 2026-09-07.\n[workspace:dir]\nWorking directory: {REPO}\n\nRequest: {q2}"}]
            r3 = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs2, 200, uuid.uuid4().hex, 0.1, timeout=120)
            ttft = (r3.t_first - r3.t0) if r3.t_first else None
            rec = {"tag": args.tag, "policy": policy, "rep": rep, "history_tokens": args.history, "tail_est_tokens": tail_est, "turn1_wall_s": round(turn1_wall, 2),
                   "prewarm_call_s": round(prewarm_call, 2), "think_s": args.think, "next_ttft_s": round(ttft, 2) if ttft else None,
                   "next_wall_s": round(r3.t_end - r3.t0, 2), "next_busy_retries": r3.busy_retries, "empty": not r3.text}
            fh.write(json.dumps(rec) + "\n")
            fh.flush()
            print(f"{policy:9s}  {rep:3d}  {turn1_wall:7.1f} s   {prewarm_call:5.2f}   {rec['next_ttft_s']!s:>8} s (busy {r3.busy_retries})   {rec['next_wall_s']:6.2f} s   tail≈{tail_est}", flush=True)
            # settle: bring the cache back to the plain prompt for the next rep
            prewarm({"messages": [{"role": "system", "content": S}], "force": True})
            time.sleep(12)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
