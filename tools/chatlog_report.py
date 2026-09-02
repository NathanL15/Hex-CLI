#!/usr/bin/env python3
"""tools/chatlog_report.py — read the full chat logs back.

    python tools/chatlog_report.py                 # summary across all sessions
    python tools/chatlog_report.py --last          # replay the most recent session
    python tools/chatlog_report.py --session 1a2b  # replay one session (id prefix)
    python tools/chatlog_report.py --dir PATH      # another log directory
    python tools/chatlog_report.py --json          # summary as JSON (for scripts)

The summary answers "how do I use it and how does it respond": versions
seen, turns, tools, retries, empty replies, latencies, how turns end, and
the slowest / failed turns to look at first. A replay prints a session as
a readable transcript with every tool call and raw reply.
"""
from __future__ import annotations

import argparse
import json
import statistics as st
import sys
from collections import Counter, defaultdict
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_DIR = Path.home() / ".shellai" / "chatlog"


def load(log_dir: Path) -> dict[str, list[dict]]:
    sessions: dict[str, list[dict]] = defaultdict(list)
    for f in sorted(log_dir.glob("*.jsonl")):
        with f.open(encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rec["_file"] = f.name
                sessions[rec.get("session", f.stem)].append(rec)
    return sessions


def summarize(sessions: dict[str, list[dict]]) -> dict:
    versions: Counter = Counter()
    models: Counter = Counter()
    tools: Counter = Counter()
    ends: Counter = Counter()
    statuses: Counter = Counter()
    commands: Counter = Counter()
    latencies: list[float] = []
    first_latencies: list[float] = []
    turn_durations: list[float] = []
    replies = empties = retries = tool_errors = compactions = errors = 0
    turns = 0
    slow: list[tuple[float, str, str]] = []
    failed: list[tuple[str, str, str]] = []
    for sid, recs in sessions.items():
        query_by_turn: dict[int, str] = {}
        for r in recs:
            k = r.get("kind")
            if k == "session_start":
                versions[f"hexcli {r.get('version', '?')} / {r.get('npurun') or 'npurun ?'}"] += 1
                models[f"{r.get('model', '?')} ({r.get('backend', '?')}, budget {(r.get('server') or {}).get('input_token_budget', '?')})"] += 1
            elif k == "command":
                commands[str(r.get("text", "")).split()[0] if r.get("text") else "?"] += 1
            elif k == "turn_start":
                turns += 1
                query_by_turn[r.get("turn", -1)] = str(r.get("query", ""))
            elif k == "reply":
                replies += 1
                lat = float(r.get("latency_s") or 0)
                latencies.append(lat)
                if r.get("step") == 0 and r.get("attempt") == 0:
                    first_latencies.append(lat)
                if r.get("attempt", 0) > 0:
                    retries += 1
                if r.get("empty"):
                    empties += 1
            elif k == "tool":
                tools[str(r.get("tool"))] += 1
                if r.get("status") != "ok":
                    tool_errors += 1
            elif k == "turn_result":
                ends[str(r.get("end_kind"))] += 1
            elif k == "turn_end":
                statuses[str(r.get("status"))] += 1
                d = r.get("duration_s")
                q = query_by_turn.get(r.get("turn", -1), "")
                if isinstance(d, (int, float)):
                    turn_durations.append(float(d))
                    slow.append((float(d), sid[:8], q[:70]))
                if r.get("status") not in (None, "completed"):
                    failed.append((sid[:8], str(r.get("status")), q[:70]))
            elif k == "compaction":
                compactions += 1
            elif k == "error":
                errors += 1
                failed.append((sid[:8], f"error: {str(r.get('message', ''))[:40]}", ""))
    slow.sort(reverse=True)

    def q(xs: list[float], p: float) -> float | None:
        if not xs:
            return None
        xs = sorted(xs)
        return xs[min(len(xs) - 1, int(p * len(xs)))]

    return {
        "sessions": len(sessions),
        "turns": turns,
        "versions": dict(versions),
        "models": dict(models),
        "commands": dict(commands.most_common()),
        "tools": dict(tools.most_common()),
        "tool_errors": tool_errors,
        "llm_calls": replies,
        "retries": retries,
        "empty_replies": empties,
        "compactions": compactions,
        "errors": errors,
        "turn_status": dict(statuses),
        "turn_end_kind": dict(ends),
        "latency_s": {
            "first_call_median": round(st.median(first_latencies), 2) if first_latencies else None,
            "call_median": round(st.median(latencies), 2) if latencies else None,
            "call_p90": round(q(latencies, 0.9), 2) if latencies else None,
            "turn_median": round(st.median(turn_durations), 2) if turn_durations else None,
            "turn_p90": round(q(turn_durations, 0.9), 2) if turn_durations else None,
        },
        "slowest_turns": [{"duration_s": d, "session": s, "query": qq} for d, s, qq in slow[:5]],
        "failed_turns": [{"session": s, "status": stt, "query": qq} for s, stt, qq in failed[:10]],
    }


def print_summary(s: dict) -> None:
    print(f"\nChat log — {s['sessions']} session(s), {s['turns']} turn(s)\n")
    for label, key in (("Versions", "versions"), ("Models", "models")):
        print(f"{label}:")
        for k, v in s[key].items():
            print(f"  {v:4d}  {k}")
    print("Turns ended:", ", ".join(f"{k} {v}" for k, v in s["turn_end_kind"].items()) or "-")
    print("Turn status:", ", ".join(f"{k} {v}" for k, v in s["turn_status"].items()) or "-")
    lat = s["latency_s"]
    print(f"Model calls: {s['llm_calls']}  retries {s['retries']}  empty {s['empty_replies']}  "
          f"first-call median {lat['first_call_median']}s  call median {lat['call_median']}s  p90 {lat['call_p90']}s")
    print(f"Turns: median {lat['turn_median']}s  p90 {lat['turn_p90']}s  compactions {s['compactions']}  errors {s['errors']}")
    print("Tools:", ", ".join(f"{k} {v}" for k, v in s["tools"].items()) or "-", f" (errors {s['tool_errors']})")
    if s["commands"]:
        print("Commands:", ", ".join(f"{k} {v}" for k, v in s["commands"].items()))
    if s["slowest_turns"]:
        print("\nSlowest turns:")
        for t in s["slowest_turns"]:
            print(f"  {t['duration_s']:7.1f}s  [{t['session']}]  {t['query']}")
    if s["failed_turns"]:
        print("\nTurns to look at:")
        for t in s["failed_turns"]:
            print(f"  [{t['session']}] {t['status']}  {t['query']}")
    print()


def replay(recs: list[dict], full: bool = False) -> None:
    cut = None if full else 600
    for r in recs:
        k = r.get("kind")
        ts = str(r.get("ts", ""))[11:19]
        if k == "session_start":
            print(f"== session {r.get('session', '')[:8]}  hexcli {r.get('version')}  {r.get('model')}  "
                  f"{r.get('npurun')}  budget {(r.get('server') or {}).get('input_token_budget')}  cwd {r.get('cwd')}")
        elif k == "command":
            print(f"{ts}  > {r.get('text')}")
        elif k == "turn_start":
            print(f"\n{ts}  you> {r.get('query')}   [history {r.get('history_messages')} msgs, context {r.get('context_percent')}%]")
        elif k == "request":
            new = r.get("new_messages") or []
            for m in new:
                if m.get("ref"):
                    print(f"          [system prompt {m['ref']}]")
                elif m.get("role") == "user" and r.get("step") == 0 and r.get("attempt") == 0:
                    continue  # the request itself, shown at turn_start
                else:
                    body = str(m.get("content", ""))
                    if cut and len(body) > cut:
                        body = body[:cut] + f"… (+{len(body) - cut} chars)"
                    print(f"          -> {m.get('role')}: {body}")
        elif k == "reply":
            raw = str(r.get("raw", ""))
            if cut and len(raw) > cut:
                raw = raw[:cut] + f"… (+{len(raw) - cut} chars)"
            tag = " (retry)" if r.get("attempt") else ""
            print(f"{ts}  model{tag} {r.get('latency_s')}s: {raw or '<EMPTY>'}")
        elif k == "tool":
            out = str(r.get("output", ""))
            if cut and len(out) > cut:
                out = out[:cut] + f"… (+{len(out) - cut} chars)"
            print(f"{ts}  tool {r.get('tool')} {json.dumps(r.get('args'), ensure_ascii=False)[:200]} "
                  f"[{r.get('status')}, {r.get('latency_s')}s]\n          {out}")
        elif k == "turn_end":
            print(f"{ts}  end: {r.get('status')} ({r.get('end_kind') or '-'}) in {r.get('duration_s')}s")
        elif k == "compaction":
            print(f"{ts}  compaction: {r.get('messages_before')} -> {r.get('messages_after')} msgs")
        elif k == "error":
            print(f"{ts}  ERROR {r.get('where')}: {r.get('message')}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default=str(DEFAULT_DIR))
    ap.add_argument("--last", action="store_true", help="replay the most recent session")
    ap.add_argument("--session", help="replay the session whose id starts with this")
    ap.add_argument("--full", action="store_true", help="do not truncate long outputs in a replay")
    ap.add_argument("--json", action="store_true", help="summary as JSON")
    args = ap.parse_args()
    log_dir = Path(args.dir).expanduser()
    sessions = load(log_dir)
    if not sessions:
        print(f"no chat logs in {log_dir}")
        return 1
    if args.last or args.session:
        if args.session:
            matches = [s for s in sessions if s.startswith(args.session)]
            if len(matches) != 1:
                print(f"{len(matches)} session(s) match {args.session!r}")
                return 1
            sid = matches[0]
        else:
            sid = max(sessions, key=lambda s: sessions[s][0].get("ts", ""))
        replay(sessions[sid], full=args.full)
        return 0
    summary = summarize(sessions)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print_summary(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
