"""Quarantine guard for the backend study: run before every tier / suite.

  python guard.py --data docs/backend_study/data --label pre_npu [--keep npurun] [--max-temp 55] [--max-util 12]

1. Kills leftovers from earlier runs (python bench/load workers, typeperf, and any
   inference server not named in --keep: npurun, ollama, llama-server).
2. Refuses to continue if unexpected noise is present (other python processes,
   browsers, HWiNFO, iCloud, Ollama app) — prints them so they can be dealt with.
3. Waits until the machine is quiet: CPU utility below --max-util and the hottest
   SoC zone (TZ2) below --max-temp for 20 consecutive seconds (up to --max-wait).
4. Writes a checkpoint JSON (free RAM, temps, SYS/CPU power, process list) to
   <data>/checkpoints/<label>.json so every tier's starting state is on record.

Stdlib only. Exit code 0 = clean, 2 = noise that needs a decision.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import time
from pathlib import Path

SERVERS = {"npurun.exe": "npurun", "ollama.exe": "ollama", "ollama app.exe": "ollama",
           "llama-server.exe": "llama-server"}
ALWAYS_KILL = {"typeperf.exe"}
NOISE = {"chrome.exe", "msedge.exe", "HWiNFO_ARM64.EXE", "iCloudPhotos.exe", "iCloudServices.exe",
         "iCloudDrive.exe", "iCloudHome.exe", "iCloudCKKS.exe", "ApplePhotoStreams.exe", "Teams.exe",
         "ms-teams.exe", "Spotify.exe", "Discord.exe", "steam.exe", "OneDrive.exe"}


def ps(cmd: str) -> str:
    return subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True,
                          timeout=120).stdout


def processes() -> list[dict]:
    out = ps("Get-CimInstance Win32_Process | Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    return data if isinstance(data, list) else [data]


def kill(pid: int) -> None:
    subprocess.run(["taskkill", "/F", "/PID", str(pid)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def sample() -> dict:
    out = ps(r"$s=(Get-Counter '\Processor Information(_Total)\% Processor Utility','\Thermal Zone Information(\_SB.TZ2)\High Precision Temperature','\Thermal Zone Information(\_SB.TZ0)\High Precision Temperature','\Energy Meter(SYS)\Power','\Energy Meter(CPU_CLUSTER_0)\Power','\Energy Meter(CPU_CLUSTER_1)\Power','\Energy Meter(CPU_CLUSTER_2)\Power','\Memory\Available MBytes').CounterSamples | ForEach-Object { $_.CookedValue }; $s -join ','")
    try:
        u, tz2, tz0, sysw, c0, c1, c2, avail = [float(x) for x in out.strip().split(",")]
    except ValueError:
        return {}
    return {"cpu_util": round(u, 1), "tz2_c": round(tz2 / 10 - 273.15, 1), "tz0_c": round(tz0 / 10 - 273.15, 1),
            "sys_w": round(sysw / 1000, 2), "cpu_w": round((c0 + c1 + c2) / 1000, 2), "avail_mb": int(avail)}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--label", required=True)
    ap.add_argument("--keep", action="append", default=[], help="server to keep alive (npurun|ollama|llama-server)")
    ap.add_argument("--max-temp", type=float, default=55.0)
    ap.add_argument("--max-util", type=float, default=12.0)
    ap.add_argument("--max-wait", type=int, default=600)
    ap.add_argument("--quiet-seconds", type=int, default=20)
    args = ap.parse_args()
    me = os.getpid()

    # 1. kill leftovers
    killed = []
    for p in processes():
        name, pid, cmd = p.get("Name", ""), int(p.get("ProcessId", 0)), p.get("CommandLine") or ""
        if pid == me or pid == os.getppid():
            continue
        low = name.lower()
        if low in ALWAYS_KILL or (low in {k.lower() for k in SERVERS} and SERVERS.get(name, SERVERS.get(low, "")) not in args.keep):
            kill(pid)
            killed.append(f"{name}:{pid}")
        elif low in ("python.exe", "pythonw.exe") and ("bench.py" in cmd or "load.py" in cmd or "cases_" in cmd
                                                          or "run_suite" in cmd or "multiprocessing" in cmd):
            kill(pid)
            killed.append(f"{name}:{pid}")
    if killed:
        print("killed leftovers:", ", ".join(killed))
        time.sleep(2)

    # 2. noise check
    noise = []
    for p in processes():
        name, pid, cmd = p.get("Name", ""), int(p.get("ProcessId", 0)), p.get("CommandLine") or ""
        if name in NOISE or name.lower() in {n.lower() for n in NOISE}:
            noise.append(f"{name}:{pid}")
        elif name.lower() in ("python.exe", "pythonw.exe") and pid != me:
            noise.append(f"{name}:{pid} {cmd[:80]}")
    if noise:
        print("NOISE PRESENT (decide: kill or accept):", *noise, sep="\n  ")

    # 3. wait for quiet
    t0, quiet_since, last = time.time(), None, {}
    while time.time() - t0 < args.max_wait:
        last = sample()
        ok = last and last["cpu_util"] <= args.max_util and last["tz2_c"] <= args.max_temp
        if ok:
            quiet_since = quiet_since or time.time()
            if time.time() - quiet_since >= args.quiet_seconds:
                break
        else:
            quiet_since = None
        print(f"  waiting: util={last.get('cpu_util')}% tz2={last.get('tz2_c')}C sys={last.get('sys_w')}W", flush=True)
        time.sleep(5)
    else:
        print("WARNING: quiet condition not reached within max-wait; recording state anyway")

    # 4. checkpoint
    ckdir = Path(args.data) / "checkpoints"
    ckdir.mkdir(parents=True, exist_ok=True)
    servers = [f"{p['Name']}:{p['ProcessId']}" for p in processes()
               if p.get("Name", "").lower() in {k.lower() for k in SERVERS}]
    ck = {"label": args.label, "at": time.strftime("%Y-%m-%d %H:%M:%S"), "epoch": time.time(), "state": last,
          "killed": killed, "noise": noise, "servers_alive": servers, "waited_s": round(time.time() - t0, 1)}
    (ckdir / f"{args.label}.json").write_text(json.dumps(ck, indent=1), encoding="utf-8")
    print(f"checkpoint {args.label}: {last} servers={servers}")
    return 2 if noise else 0


if __name__ == "__main__":
    raise SystemExit(main())
