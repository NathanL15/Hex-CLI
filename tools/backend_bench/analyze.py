"""Join bench.py request logs with the typeperf counter CSVs and summarise.

  python analyze.py --data docs/backend_study/data --out docs/backend_study/summary

Energy per request is the difference of the cumulative Energy Meter counter across the
request window (interpolated at the boundaries), calibrated against the Power counter;
power columns are window means. NPU utilisation comes only from the separately sampled
counters_npueng_*.csv files. Absent columns stay None (never 0). Stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import statistics as st
import sys
from collections import defaultdict
from pathlib import Path

METRICS = ("sys_w", "cpu_w", "gpu_w", "cpu_util", "cpu_mhz", "temp_tz2", "temp_tz0", "npu_util",
           "server_cpu", "server_ws_mb", "avail_mb", "run_queue")


def parse_ts(s: str) -> float:
    return dt.datetime.strptime(s, "%m/%d/%Y %H:%M:%S.%f").timestamp()


def load_counters(data: Path) -> tuple[list[dict], list[dict]]:
    main, npu = [], []
    for f in sorted(data.glob("counters_*.csv")):
        is_npu = f.name.startswith("counters_npueng")
        with f.open(encoding="utf-8-sig", newline="") as fh:
            rd = csv.reader(fh)
            try:
                cols = [h.lower() for h in next(rd)]
            except StopIteration:
                continue
            for line in rd:
                if len(line) != len(cols):
                    continue
                try:
                    t = parse_ts(line[0])
                except ValueError:
                    continue
                vals: dict[str, float | None] = {}
                for c, v in zip(cols[1:], line[1:]):
                    try:
                        vals[c] = float(v)
                    except ValueError:
                        vals[c] = None

                def pick(sub: str, must: str = "") -> list[float]:
                    return [v for c, v in vals.items() if sub in c and must in c and v is not None]

                def one(sub: str) -> float | None:
                    p = pick(sub)
                    return p[0] if p else None

                def mw(sub: str) -> float | None:
                    v = one(sub)
                    return v / 1000 if v is not None else None

                r: dict = {"t": t, "file": f.name}
                if is_npu:
                    eng = pick("gpu engine(")
                    r["npu_util"] = sum(eng) if eng else None
                    npu.append(r)
                    continue
                r["sys_w"] = mw("energy meter(sys)\\power")
                cl = pick("energy meter(cpu_cluster", "\\power")
                r["cpu_w"] = sum(cl) / 1000 if cl else None
                r["gpu_w"] = mw("energy meter(gpu)\\power")
                r["sys_e"] = one("energy meter(sys)\\energy")
                cle = pick("energy meter(cpu_cluster", "\\energy")
                r["cpu_e"] = sum(cle) if cle else None
                r["cpu_util"] = one("% processor utility")
                r["cpu_mhz"] = one("processor frequency")
                for z in ("tz0", "tz2", "tz98"):
                    v = one(f"_sb.{z})")
                    r[f"temp_{z}"] = (v / 10 - 273.15) if v else None
                sc = pick("process(npurun)\\% processor time") + pick("process(llama-server", "% processor time")
                r["server_cpu"] = sum(sc) if sc else None
                sw = pick("process(npurun)\\working set") + pick("process(llama-server", "working set")
                r["server_ws_mb"] = sum(sw) / 2**20 if sw else None
                r["avail_mb"] = one("available mbytes")
                r["run_queue"] = one("processor queue length")
                main.append(r)
    main.sort(key=lambda r: r["t"])
    npu.sort(key=lambda r: r["t"])
    return main, npu


def window(rows: list[dict], t0: float, t1: float) -> list[dict]:
    return [r for r in rows if t0 - 0.5 <= r["t"] <= t1 + 0.5]


def mean(xs):
    xs = [x for x in xs if x is not None]
    return round(st.fmean(xs), 3) if xs else None


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 3) if xs else None


def interp(rows: list[dict], key: str, t: float) -> float | None:
    """Linear interpolation of a cumulative counter at time t."""
    pts = [(r["t"], r[key]) for r in rows if r.get(key) is not None]
    if not pts or t < pts[0][0] - 2 or t > pts[-1][0] + 2:
        return None
    lo = max((p for p in pts if p[0] <= t), default=pts[0])
    hi = min((p for p in pts if p[0] >= t), default=pts[-1])
    if hi[0] == lo[0]:
        return lo[1]
    return lo[1] + (hi[1] - lo[1]) * (t - lo[0]) / (hi[0] - lo[0])


def calibrate_energy(rows: list[dict]) -> float | None:
    """Scale factor s such that (ΔEnergy / Δt) * s == Power in W."""
    ratios = []
    for a, b in zip(rows, rows[1:]):
        if a.get("sys_e") is not None and b.get("sys_e") is not None and a.get("sys_w") and b.get("sys_w") \
                and 0.5 < b["t"] - a["t"] < 2 and a["file"] == b["file"]:
            de = (b["sys_e"] - a["sys_e"]) / (b["t"] - a["t"])
            if de > 0:
                ratios.append(((a["sys_w"] + b["sys_w"]) / 2) / de)
    return st.median(ratios) if len(ratios) > 20 else None


def energy_j(rows: list[dict], key: str, t0: float, t1: float, scale: float | None) -> float | None:
    if scale is None:
        return None
    e0, e1 = interp(rows, key, t0), interp(rows, key, t1)
    if e0 is None or e1 is None:
        return None
    return (e1 - e0) * scale


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--data", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    data, out = Path(args.data), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    rows, npu_rows = load_counters(data)
    scale = calibrate_energy(rows)
    reqs = [json.loads(ln) for ln in (data / "requests.jsonl").read_text(encoding="utf-8").splitlines() if ln.strip()]

    # sanity: no main row may carry 0 W SYS while CPU util is present
    bad = [r for r in rows if r.get("sys_w") == 0.0 and r.get("cpu_util") is not None]
    if bad:
        print(f"WARNING: {len(bad)} main rows have SYS power 0 W ({bad[0]['file']})", file=sys.stderr)

    # per-request / mark window means + energy
    for q in reqs:
        w = window(rows, q["t_start"], q["t_end"])
        for m in METRICS:
            if m == "npu_util":
                q["m_npu_util"] = mean([r.get("npu_util") for r in window(npu_rows, q["t_start"], q["t_end"])])
            else:
                q[f"m_{m}"] = mean([r.get(m) for r in w])
        q["n_samples"] = len(w)
        q["sys_j"] = energy_j(rows, "sys_e", q["t_start"], q["t_end"], scale)
        q["cpu_j"] = energy_j(rows, "cpu_e", q["t_start"], q["t_end"], scale)
        if q.get("t_first_attempt"):
            q["sys_j_user"] = energy_j(rows, "sys_e", q["t_first_attempt"], q["t_end"], scale)

    # baselines: 'none' (no server, whole file) and loaded-idle per (backend, tag)
    idle: dict[str, dict] = {}
    for f in sorted({r["file"] for r in rows}):
        if f.startswith("counters_none"):
            w = [r for r in rows if r["file"] == f][5:]
            idle["none"] = {m: mean([r.get(m) for r in w]) for m in METRICS if m != "npu_util"} | {"n": len(w)}
    marks = [q for q in reqs if q["phase"] == "mark"]
    for i, q in enumerate(marks):
        if q.get("mark") == "idle_start":
            end = next((m for m in marks[i + 1:] if m.get("mark") == "idle_end"), None)
            if end:
                w = window(rows, q["t_start"] + 5, end["t_end"] - 1)
                wn = window(npu_rows, q["t_start"] + 5, end["t_end"] - 1)
                idle[f"{q['backend']}/{q['tag']}"] = {m: mean([r.get(m) for r in w]) for m in METRICS if m != "npu_util"} | {
                    "npu_util": mean([r.get("npu_util") for r in wn]), "n": len(w)}
    none_w = (idle.get("none") or {}).get("sys_w")

    for q in reqs:
        if q["phase"] == "mark" or not q.get("output_tokens") or q.get("sys_j") is None:
            continue
        q["j_per_out_token"] = round(q["sys_j"] / q["output_tokens"], 3)
        if none_w is not None:
            q["j_per_out_token_above_machine_idle"] = round(max(q["sys_j"] - none_w * q["total_s"], 0) / q["output_tokens"], 3)
        loaded = (idle.get(f"{q['backend']}/{q['tag']}") or {}).get("sys_w")
        if loaded is not None:
            q["j_per_out_token_above_loaded_idle"] = round(max(q["sys_j"] - loaded * q["total_s"], 0) / q["output_tokens"], 3)

    # aggregate
    groups: dict[tuple, list[dict]] = defaultdict(list)
    for q in reqs:
        if q["phase"] == "mark" or q.get("error"):
            continue
        groups[(q["backend"], q["tag"], q["phase"], q.get("target"), q.get("workers"), q.get("turn"))].append(q)

    def srv_tps(q: dict, count: str, dur: str) -> float | None:
        s = q.get("server") or {}
        return (s[count] / (s[dur] / 1e9)) if s.get(dur) and s.get(count) else None

    agg = []
    for key, qs in sorted(groups.items(), key=lambda kv: [str(x) for x in kv[0]]):
        backend, tag, phase, target, workers, turn = key
        a = {"backend": backend, "tag": tag, "phase": phase, "target": target, "workers": workers, "turn": turn,
             "n": len(qs),
             "prompt_tokens": med([q["prompt_tokens"] for q in qs]),
             "output_tokens": med([q["output_tokens"] for q in qs]),
             "ttft_s": med([q["ttft_s"] for q in qs]),
             "ttft_min_s": min((q["ttft_s"] for q in qs if q["ttft_s"]), default=None),
             "ttft_max_s": max((q["ttft_s"] for q in qs if q["ttft_s"]), default=None),
             "prefill_tps": med([q["prefill_tps"] for q in qs]),
             "decode_tps": med([q["decode_tps"] for q in qs]),
             "decode_tps_min": min((q["decode_tps"] for q in qs if q["decode_tps"]), default=None),
             "decode_tps_max": max((q["decode_tps"] for q in qs if q["decode_tps"]), default=None),
             "total_s": med([q["total_s"] for q in qs]),
             "total_s_user": med([q.get("total_s_user", q["total_s"]) for q in qs]),
             "retried": sum(1 for q in qs if q.get("attempts", 1) > 1),
             "wasted_s_sum": round(sum(q.get("wasted_s", 0) for q in qs), 2),
             "busy_wait_s_sum": round(sum(q.get("busy_wait_s", 0) for q in qs), 2),
             "finish_reasons": sorted({q.get("finish_reason", "") for q in qs}),
             "sys_j": med([q.get("sys_j") for q in qs]),
             "cpu_j": med([q.get("cpu_j") for q in qs]),
             "j_per_out_token": med([q.get("j_per_out_token") for q in qs]),
             "j_per_out_token_above_machine_idle": med([q.get("j_per_out_token_above_machine_idle") for q in qs]),
             "j_per_out_token_above_loaded_idle": med([q.get("j_per_out_token_above_loaded_idle") for q in qs]),
             "server_prompt_eval_tps": med([srv_tps(q, "prompt_eval_count", "prompt_eval_duration") for q in qs]),
             "server_eval_tps": med([srv_tps(q, "eval_count", "eval_duration") for q in qs]),
             }
        for m in METRICS:
            a[f"m_{m}"] = med([q.get(f"m_{m}") for q in qs])
        agg.append(a)

    jobs = defaultdict(list)
    for q in marks:
        if q.get("mark") in ("fg_job_alone", "fg_job_during_inference", "fg_job_no_server") and q.get("job"):
            jobs[(q["backend"], q["tag"], q["mark"])].append(q["job"])
    job_summary = [{"backend": b, "tag": t, "mark": m, "n": len(js), **{k: med([j[k] for j in js]) for k in js[0]}}
                   for (b, t, m), js in sorted(jobs.items())]

    sustain = []
    for key in sorted({(q["backend"], q["tag"]) for q in reqs}):
        qs = [q for q in reqs if q["phase"] == "sustain" and (q["backend"], q["tag"]) == key and not q.get("error")]
        if len(qs) >= 4:
            half = len(qs) // 2
            a_, b_ = qs[:half], qs[half:]
            sustain.append({"backend": key[0], "tag": key[1], "n": len(qs),
                            "first_half_tps": med([q["decode_tps"] for q in a_]),
                            "second_half_tps": med([q["decode_tps"] for q in b_]),
                            "first_temp_tz2": mean([q.get("m_temp_tz2") for q in a_[:2]]),
                            "last_temp_tz2": mean([q.get("m_temp_tz2") for q in b_[-2:]]),
                            "sys_w": mean([q.get("m_sys_w") for q in qs]), "cpu_w": mean([q.get("m_cpu_w") for q in qs]),
                            "cpu_util": mean([q.get("m_cpu_util") for q in qs]), "npu_util": mean([q.get("m_npu_util") for q in qs]),
                            "cpu_mhz": mean([q.get("m_cpu_mhz") for q in qs])})

    summary = {"energy_scale": scale, "idle": idle, "aggregate": agg, "jobs": job_summary, "sustain": sustain,
               "n_requests": len(reqs), "n_counter_rows": len(rows), "n_npueng_rows": len(npu_rows)}
    (out / "summary.json").write_text(json.dumps(summary, indent=1), encoding="utf-8")
    (out / "requests_enriched.jsonl").write_text("\n".join(json.dumps(q) for q in reqs), encoding="utf-8")

    md = ["# Backend study — raw summary", "", f"energy scale (counter→J): {scale}", "", "## Baselines",
          "| baseline | sys W | cpu W | cpu util % | server CPU % | server WS MB | npu util % | avail MB | n |",
          "|---|---|---|---|---|---|---|---|---|"]
    for k, v in idle.items():
        md.append(f"| {k} | {v.get('sys_w')} | {v.get('cpu_w')} | {v.get('cpu_util')} | {v.get('server_cpu')} | "
                  f"{v.get('server_ws_mb')} | {v.get('npu_util')} | {v.get('avail_mb')} | {v['n']} |")
    cols = ("backend", "tag", "phase", "target", "workers", "turn", "n", "prompt_tokens", "output_tokens", "ttft_s",
            "ttft_min_s", "prefill_tps", "decode_tps", "total_s", "total_s_user", "retried", "busy_wait_s_sum",
            "m_sys_w", "m_cpu_w", "m_cpu_util", "m_server_cpu", "m_npu_util", "sys_j", "j_per_out_token",
            "j_per_out_token_above_machine_idle", "server_prompt_eval_tps", "server_eval_tps", "finish_reasons")
    md += ["", "## Per phase (medians)", "", "| " + " | ".join(cols) + " |", "|" + "---|" * len(cols)]
    for a in agg:
        md.append("| " + " | ".join(str(a.get(k)) for k in cols) + " |")
    md += ["", "## Foreground job (seconds, median)", "",
           "| backend | tag | condition | n | pyloop 1 core | sha256 256MB | pyloop 12 cores |", "|---|---|---|---|---|---|---|"]
    for j in job_summary:
        md.append(f"| {j['backend']} | {j['tag']} | {j['mark']} | {j['n']} | {j.get('pyloop_1core_s')} | "
                  f"{j.get('sha256_256mb_s')} | {j.get('pyloop_12core_s')} |")
    md += ["", "## Sustained decode", "",
           "| backend | tag | n | 1st half tok/s | 2nd half tok/s | TZ2 start °C | TZ2 end °C | sys W | cpu W | cpu util | npu util | MHz |",
           "|---|---|---|---|---|---|---|---|---|---|---|---|"]
    for s in sustain:
        md.append(f"| {s['backend']} | {s['tag']} | {s['n']} | {s['first_half_tps']} | {s['second_half_tps']} | "
                  f"{s['first_temp_tz2']} | {s['last_temp_tz2']} | {s['sys_w']} | {s['cpu_w']} | {s['cpu_util']} | "
                  f"{s['npu_util']} | {s['cpu_mhz']} |")
    (out / "summary.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"requests={len(reqs)} counter_rows={len(rows)} npueng_rows={len(npu_rows)} groups={len(agg)} scale={scale} -> {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
