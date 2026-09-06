"""Battery session: discharge rate from the battery itself (WMI BatteryStatus)
while the laptop is unplugged, for: no server, server idle, and real four-turn
Hex conversations. Reports mW per phase, mWh per conversation, and projected
battery minutes.

  python battery_session.py --tag v26 [--env NPURUN_HTP_POLL=1] [--idle 60] [--convs 3]
"""
from __future__ import annotations

import argparse
import json
import os
import statistics as st
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(HERE))
import bench  # noqa: E402
import npu_ab  # noqa: E402

PS_FILE = HERE / ".battery_probe.ps1"
PS_FILE.write_text(
    "$b=Get-CimInstance -Namespace root\\wmi -ClassName BatteryStatus\n"
    "$c=Get-CimInstance -Namespace root\\wmi -ClassName BatteryFullChargedCapacity\n"
    "Write-Output (\"{0},{1},{2},{3},{4}\" -f $b.PowerOnline, $b.Discharging, $b.DischargeRate, "
    "$b.RemainingCapacity, $c.FullChargedCapacity)\n",
    encoding="utf-8")


def read_batt() -> dict:
    out = subprocess.run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(PS_FILE)], capture_output=True, text=True, timeout=30).stdout.strip()
    on, dis, rate, rem, full = out.split(",")
    return {"t": time.time(), "ac": on == "True", "discharging": dis == "True", "mw": int(rate), "remaining_mwh": int(rem), "full_mwh": int(full)}


class Sampler:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self.stop = threading.Event()
        self.th = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self.stop.is_set():
            try:
                self.rows.append(read_batt())
            except Exception:  # noqa: BLE001
                pass
            self.stop.wait(2)

    def window(self, t0: float, t1: float) -> list[int]:
        return [r["mw"] for r in self.rows if t0 <= r["t"] <= t1 and r["discharging"] and r["mw"] > 0]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tag", required=True)
    ap.add_argument("--env", action="append", default=[])
    ap.add_argument("--idle", type=int, default=60)
    ap.add_argument("--convs", type=int, default=3)
    ap.add_argument("--skip-none", action="store_true")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab"))
    args = ap.parse_args()
    for kv in args.env:
        k, _, v = kv.partition("=")
        os.environ[k] = v
    b0 = read_batt()
    if b0["ac"] or not b0["discharging"]:
        print("NOT ON BATTERY:", b0)
        return 2
    print(f"battery: {b0['remaining_mwh']}/{b0['full_mwh']} mWh, discharging at {b0['mw']} mW")
    smp = Sampler()
    smp.th.start()
    res = {"tag": args.tag, "env": args.env, "full_mwh": b0["full_mwh"], "phases": {}}

    def phase(name: str, fn) -> None:
        t0 = time.time()
        fn()
        t1 = time.time()
        time.sleep(2)
        w = smp.window(t0, t1)
        res["phases"][name] = {"seconds": round(t1 - t0, 1), "mw_mean": round(st.fmean(w)) if w else None,
                               "mw_min": min(w) if w else None, "mw_max": max(w) if w else None, "n": len(w),
                               "mwh": round(st.fmean(w) * (t1 - t0) / 3600, 1) if w else None}
        p = res["phases"][name]
        print(f"  {name:22s} {p['seconds']:6.1f}s  {p['mw_mean']} mW (n={p['n']})  {p['mwh']} mWh", flush=True)

    if not args.skip_none:
        subprocess.run(["taskkill", "/F", "/IM", "npurun.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(15)
        phase("machine idle, no server", lambda: time.sleep(args.idle))
    print("server accepted after", npu_ab.restart_server(), "s")
    time.sleep(10)
    phase("server idle", lambda: time.sleep(args.idle))
    from hexcli import agent as ag
    q1 = "List the python files in the hexcli folder and tell me which one is largest."
    follow = ["Now count how many of them import json.", "Which of those also define a class?", "Summarise what you found in two sentences."]
    gguf = bench.gguf_path("qwen3:4b-instruct-2507-q4_K_M")

    def conv(i: int) -> None:
        sid = uuid.uuid4().hex
        msgs = [{"role": "system", "content": ag.build_autopilot_prompt(cwd=str(REPO), max_steps=15, query=q1) + f"\n\n(session {sid})"},
                {"role": "user", "content": q1}]
        for turn in range(4):
            r = bench.chat_openai(bench.BASES["npu"], "qwen3-4b", msgs, 256, sid, 0.1)
            if turn == 3:
                break
            tool = bench.make_text(300, gguf, seed=9100 + i * 10 + turn)
            msgs = msgs + [{"role": "assistant", "content": r.text or "(no reply)"},
                           {"role": "user", "content": f"TOOL RESULT (list_directory):\n{tool}\n\n{follow[turn]}"}]
            time.sleep(5)  # think time

    for i in range(args.convs):
        phase(f"conversation {i + 1} (4 turns)", lambda i=i: conv(i))
    phase("server idle after", lambda: time.sleep(30))
    smp.stop.set()
    b1 = read_batt()
    res["battery_used_mwh"] = b0["remaining_mwh"] - b1["remaining_mwh"]
    convs = [p for k, p in res["phases"].items() if k.startswith("conversation") and p["mwh"]]
    if convs:
        res["mwh_per_conversation"] = round(st.fmean(p["mwh"] for p in convs), 1)
        res["conversations_per_full_battery"] = round(b0["full_mwh"] / res["mwh_per_conversation"])
    for k in ("machine idle, no server", "server idle"):
        if k in res["phases"] and res["phases"][k]["mw_mean"]:
            res["phases"][k]["battery_hours_at_this_rate"] = round(b0["full_mwh"] / res["phases"][k]["mw_mean"], 1)
    out = Path(args.out) / f"battery_{args.tag}.json"
    out.write_text(json.dumps(res, indent=1), encoding="utf-8")
    print(json.dumps({k: v for k, v in res.items() if k != "phases"}, indent=1))
    for k, p in res["phases"].items():
        if "battery_hours_at_this_rate" in p:
            print(f"  {k}: {p['mw_mean']} mW → {p['battery_hours_at_this_rate']} h on a full battery")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
