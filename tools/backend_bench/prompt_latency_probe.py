"""What a shorter system prompt buys on a real Hex-shaped call.

For each prompt variant (production stable prompt and shorter forms of it),
runs the same set of user queries as fresh conversations on the production
server (polling off, Rewind). Between conversations the probe does what Hex
does at the end of a turn: it asks the server to prewarm the system prompt,
and waits for that, so every step-0 request is a pure prefix extension. Each
tool-calling query is also continued with a ~400-token tool result to measure
a step-2 call at the larger context. Records time to first token, generated
tokens, decode rate, busy waits and total wall per request.

  python prompt_latency_probe.py --tag v261 [--reps 2] [--only A_prod,B_dedent]
"""
from __future__ import annotations

import argparse
import json
import os
import re
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

QUERIES = [
    "what cpu do i have",
    "List the python files in the hexcli folder and tell me which one is largest.",
    "Add a version field with value 1.0 to config.json",
    "What is the capital of Australia?",
    "give me a random number between 1 and 100",
    "Use the write_file tool to tell me a haiku about autumn.",
    "how much free disk space is on C:",
    "Find every file that imports json under hexcli and count them.",
]


def variants() -> dict[str, str]:
    from hexcli import agent as ag
    from hexcli import prompts as P
    ag._ACTIVE_CONFIG = {"prompt_stable_prefix": True, "conditional_rules": False}
    prod = ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query="")
    deleg = "\n\n    " + P._DELEGATE_SCHEMA
    dedent = lambda s: "\n".join(ln.strip() for ln in s.splitlines())  # noqa: E731

    def drop_rules(s: str, nums: list[int]) -> str:
        for n in nums:
            s = s.replace(P._AUTOPILOT_RULES[n].format(max_steps=15), "", 1)
        return s

    no_deleg = prod.replace(deleg, "")
    return {
        "A_prod": prod,
        "B_dedent": dedent(prod),
        "C_dedent_nodelegate": dedent(no_deleg),
        "D_ref_minus_13_14": dedent(drop_rules(no_deleg, [13, 14])),
        "E_ref_minus_9_13_14": dedent(drop_rules(no_deleg, [9, 13, 14])),
    }


def prewarm(prompt: str) -> float:
    """Hex's end-of-turn prewarm: the server resets its cache to the system
    prompt while the user reads the answer. Waits for it (429 = still busy)."""
    body = json.dumps({"messages": [{"role": "system", "content": prompt}]}).encode()
    t0 = time.time()
    while time.time() - t0 < 180:
        try:
            req = urllib.request.Request((bench.BASES["npu"].rstrip("/") + ("" if bench.BASES["npu"].rstrip("/").endswith("/v1") else "/v1") + "/npurun/prewarm"), data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
            with urllib.request.urlopen(req, timeout=180):
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
    ap.add_argument("--reps", type=int, default=2)
    ap.add_argument("--only", default="")
    ap.add_argument("--tool-tokens", type=int, default=400)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--no-restart", action="store_true")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab/prompt_latency_probe.jsonl"))
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    if not args.no_restart:
        print(f"server accepted after {npu_ab.restart_server()} s")
    gguf = bench.gguf_path("qwen3:4b-instruct-2507-q4_K_M")
    fh = Path(args.out).open("a", encoding="utf-8")
    vs = variants()
    if args.only:
        vs = {k: v for k, v in vs.items() if k in args.only.split(",")}
    tool_result = ("TOOL RESULT (list_directory):\n" + bench.make_text(args.tool_tokens, gguf, seed=4242)
                   + "\n\nContinue with the task.")
    user_head = "Date: 2026-09-06. Working directory: " + str(REPO) + "\n\n"

    def one(variant: str, prompt: str, step: int, msgs: list[dict], sid: str, q: str, rep: int) -> str:
        in_tok = bench.count_tokens(bench.chatml(msgs), gguf)
        r = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, 512, sid, 0.1, timeout=300)
        gen = bench.count_tokens(r.text, gguf) if r.text else 0
        ttft = (r.t_first - r.t0) if r.t_first else None
        dec = (gen - 1) / (r.t_end - r.t_first) if (gen > 8 and r.t_first) else None
        rec = {"tag": args.tag, "variant": variant, "prompt_tok": bench.count_tokens(prompt, gguf), "rep": rep, "step": step,
               "query": q, "in_tok": in_tok, "ttft_s": round(ttft, 3) if ttft else None, "gen_tok": gen,
               "decode_tok_s": round(dec, 2) if dec else None, "total_s": round(r.t_end - r.t0, 3),
               "empty": not r.text, "error": r.error, "busy_wait_s": round(r.busy_wait_s, 2),
               "busy_retries": r.busy_retries, "reply": (r.text or "")[:160]}
        fh.write(json.dumps(rec) + "\n")
        fh.flush()
        print(f"  {variant:22s} s{step} in={in_tok:5d} ttft={rec['ttft_s']} gen={gen:4d} dec={rec['decode_tok_s']} "
              f"tot={rec['total_s']} busy={r.busy_retries} {rec['reply'][:50]!r}", flush=True)
        return r.text or ""

    for variant, prompt in vs.items():
        print(f"== {variant}: {bench.count_tokens(prompt, gguf)} prompt tokens")
        # prime: pays the divergent rebuild once, like Hex's start-up prime
        one(variant, prompt, -1, [{"role": "system", "content": prompt}, {"role": "user", "content": user_head + "Say OK."}],
            uuid.uuid4().hex, "prime", 0)
        print(f"    prewarm {prewarm(prompt)} s", flush=True)
        for rep in range(args.reps):
            for q in QUERIES:
                sid = uuid.uuid4().hex
                msgs = [{"role": "system", "content": prompt}, {"role": "user", "content": user_head + q}]
                a = one(variant, prompt, 0, msgs, sid, q, rep)
                if re.search(r'"action"\s*:\s*"(?!finish)', a):
                    msgs2 = msgs + [{"role": "assistant", "content": a}, {"role": "user", "content": tool_result}]
                    one(variant, prompt, 1, msgs2, sid, q, rep)
                print(f"    prewarm {prewarm(prompt)} s", flush=True)
                time.sleep(0.3)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
