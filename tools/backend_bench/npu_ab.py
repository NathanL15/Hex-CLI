"""A/B driver for npurun runtime settings (Genie config + HTP backend extension).

  python npu_ab.py --variant poll0 --phases idle,decode,hexconv --out docs/backend_study/data_npu_ab
  python npu_ab.py --restore

Each variant edits the installed bundle's genie_config.json / htp_backend_ext_config.json
(originals kept as *.orig), restarts npurun through launcher._start_npurun_server(), waits
for the first accepted request, then runs bench.py with --tag <variant>. --restore puts the
originals back and restarts nothing.
"""
from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(REPO))
BUNDLE = (Path.home() / "AppData/Local/npurun/models/qwen3-4b-instruct-2507/bundle"
          / "qwen3_4b_instruct_2507-genie-w4a16-qualcomm_snapdragon_x_elite")
GENIE = BUNDLE / "genie_config.json"
HTP = BUNDLE / "htp_backend_ext_config.json"

# variant → (genie backend overrides, htp core overrides, genie engine overrides)
VARIANTS: dict[str, tuple[dict, dict, dict]] = {
    "base": ({}, {}, {}),
    "poll0": ({"poll": False}, {}, {}),
    "poll0_sustained": ({"poll": False}, {"perf_profile": "sustained_high_performance"}, {}),
    "poll0_highperf": ({"poll": False}, {"perf_profile": "high_performance"}, {}),
    "poll0_balanced": ({"poll": False}, {"perf_profile": "balanced"}, {}),
    "poll0_powersaver": ({"poll": False}, {"perf_profile": "power_saver"}, {}),
    "poll0_rpc0": ({"poll": False}, {"rpc_polling_time": 0}, {}),
    "poll0_t2": ({"poll": False}, {}, {"n-threads": 2}),
    "poll0_t1": ({"poll": False}, {}, {"n-threads": 1}),
    "burst_rpc0": ({}, {"rpc_polling_time": 0}, {}),
    "cand": ({"poll": False}, {"rpc_polling_time": 0}, {"n-threads": 2}),
    "asyncinit": ({"allow-async-init": True}, {}, {}),
    "poll0_rcl10": ({"poll": False}, {"rpc_control_latency": 10}, {}),
    "poll1_t1": ({"poll": True}, {}, {"n-threads": 1}),
    "poll0_hmx5s": ({"poll": False}, {"hmx_timeout_us": 5000000}, {}),
    "poll1_t2": ({"poll": True}, {}, {"n-threads": 2}),
    "poll0_sustained2": ({"poll": False}, {"perf_profile": "sustained_high_performance"}, {}),
}


def backup() -> None:
    for f in (GENIE, HTP):
        o = f.with_suffix(f.suffix + ".orig")
        if not o.exists():
            shutil.copy2(f, o)


def restore() -> None:
    for f in (GENIE, HTP):
        o = f.with_suffix(f.suffix + ".orig")
        if o.exists():
            shutil.copy2(o, f)


def apply(variant: str) -> dict:
    backend_over, core_over, engine_over = VARIANTS[variant]
    restore()
    g = json.loads(GENIE.read_text(encoding="utf-8"))
    g["dialog"]["engine"]["backend"]["QnnHtp"].update(backend_over)
    g["dialog"]["engine"].update(engine_over)
    GENIE.write_text(json.dumps(g, indent=2), encoding="utf-8")
    h = json.loads(HTP.read_text(encoding="utf-8"))
    h["devices"][0]["cores"][0].update(core_over)
    HTP.write_text(json.dumps(h), encoding="utf-8")
    return {"backend": g["dialog"]["engine"]["backend"]["QnnHtp"], "n_threads": g["dialog"]["engine"]["n-threads"],
            "core": h["devices"][0]["cores"][0]}


def restart_server() -> float:
    subprocess.run(["taskkill", "/F", "/IM", "npurun.exe"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2)
    import launcher
    t0 = time.time()
    launcher._start_npurun_server()
    body = json.dumps({"model": "qwen3-4b", "messages": [{"role": "user", "content": "Say OK. (probe)"}],
                       "max_tokens": 4, "stream": False}).encode()
    while time.time() - t0 < 300:
        try:
            req = urllib.request.Request("http://127.0.0.1:11435/v1/chat/completions", data=body, method="POST",
                                         headers={"Content-Type": "application/json", "Authorization": "Bearer local"})
            with urllib.request.urlopen(req, timeout=60):
                return round(time.time() - t0, 1)
        except urllib.error.HTTPError as e:
            if e.code != 429:
                raise
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    raise SystemExit("server never accepted a request")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--variant", choices=list(VARIANTS))
    ap.add_argument("--restore", action="store_true")
    ap.add_argument("--phases", default="idle,decode,hexconv")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab"))
    ap.add_argument("--idle-seconds", default="45")
    ap.add_argument("--repeats", default="2")
    ap.add_argument("--extra", default="", help="extra bench.py args")
    args = ap.parse_args()
    backup()
    if args.restore:
        restore()
        print("restored originals")
        return 0
    cfg = apply(args.variant)
    print(f"variant {args.variant}: {cfg}")
    t = restart_server()
    print(f"server accepted after {t} s")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / f"variant_{args.variant}.json").write_text(json.dumps({"variant": args.variant, "config": cfg, "cold_start_s": t}, indent=1))
    cmd = [sys.executable, str(HERE / "bench.py"), "--backend", "npu", "--tag", args.variant, "--out", str(out),
           "--phases", args.phases, "--idle-seconds", args.idle_seconds, "--repeats", args.repeats] + args.extra.split()
    log = (out / f"bench_{args.variant}.log").open("a", encoding="utf-8")
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, cwd=str(REPO))
    for line in p.stdout:
        log.write(line)
        if line.startswith("  [") or "ERR" in line or "Traceback" in line:
            print(line.rstrip(), flush=True)
    return p.wait()


if __name__ == "__main__":
    raise SystemExit(main())
