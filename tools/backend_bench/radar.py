"""Whole-product optimisation radar for Hex CLI, computed from data on disk.

  python radar.py [--out docs/backend_study/summary] [--gate]

Unit of value: a solved task. Every axis has a FIXED product target (not "best
observed"), a raw value per configuration, and a score = 100 × target/value for
lower-is-better axes (value/target for higher-is-better), capped at 100. Axes
with no measurement are emitted as null and drawn as gaps, never as 100.

Configurations:
  v251   Hex v2.5.1 on npurun 0.2.1 (host polling on)
  v26    Hex 2.6 candidate: npurun 0.2.2 (polling off, async init) + start-up prime
  lcpp   CPU tier B, upstream llama.cpp Q4_0 (reference column)
  ceiling  best achievable per axis from levers that exist (documented per axis)

--gate exits 1 when v26's extended-suite pass-rate Wilson lower bound falls below
the gate threshold or when it has a hang on record that is not documented.
Stdlib only.
"""
from __future__ import annotations

import argparse
import glob
import json
import math
import statistics as st
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent
sys.path.insert(0, str(HERE))
from analyze import calibrate_energy, energy_j, load_counters, window  # noqa: E402

DATA = REPO / "docs/backend_study/data"
AB = REPO / "docs/backend_study/data_npu_ab"
SUMMARY = REPO / "docs/backend_study/summary/summary.json"
SUMMARY_AB = REPO / "docs/backend_study/summary_npu_ab/summary.json"
BASELINE_EXT = REPO / "evals/results/ask_rule_r5_20260905.json"


def wilson(p: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (0.0, 0.0)
    ph = p / n
    d = 1 + z * z / n
    c = (ph + z * z / (2 * n)) / d
    h = z * math.sqrt(ph * (1 - ph) / n + z * z / (4 * n * n)) / d
    return (round(c - h, 3), round(c + h, 3))


def med(xs):
    xs = [x for x in xs if x is not None]
    return round(st.median(xs), 2) if xs else None


def load_ext(pattern: str) -> list[dict]:
    """Per-run records from per-case extended result files (smoke_gate.py --suite extended)."""
    runs = []
    for f in sorted(glob.glob(pattern)):
        d = json.load(open(f, encoding="utf-8"))
        for cid, c in d["cases"].items():
            for tr in c.get("traces", []):
                runs.append({"case": cid, "passed": None, "wall_s": tr.get("wall_s"), "in_tok": tr.get("est_input_tokens"),
                             "first_llm_s": tr.get("first_llm_latency_s"), "total_llm_s": tr.get("total_llm_latency_s"),
                             "llm_calls": len(tr.get("llm_calls") or []), "tool_calls": len(tr.get("tool_calls") or []),
                             "end": tr.get("end_kind")})
            # passes are per case, not per trace: distribute by order (runner appends fail_details per failed run)
            n_pass, n_runs = c.get("passes", 0), c.get("runs", 0)
            for i, r in enumerate(runs[-len(c.get("traces", [])):]):
                r["passed"] = i < n_pass  # order-agnostic aggregate; only counts are used below
            runs[-1]["_case_passes"] = n_pass
            runs[-1]["_case_runs"] = n_runs
            runs[-1]["_case_invalid"] = c.get("invalid_runs", 0)
    return runs


def ext_summary(runs: list[dict]) -> dict:
    passes = sum(r.get("_case_passes", 0) for r in runs)
    n = sum(r.get("_case_runs", 0) for r in runs)
    invalid = sum(r.get("_case_invalid", 0) for r in runs)
    return {"passes": passes, "runs": n, "invalid": invalid, "pass_rate": round(passes / n, 3) if n else None,
            "wilson95": wilson(passes, n), "wall_s_med": med([r["wall_s"] for r in runs]),
            "in_tok_med": med([r["in_tok"] for r in runs]), "first_llm_s_med": med([r["first_llm_s"] for r in runs]),
            "total_llm_s_med": med([r["total_llm_s"] for r in runs]), "llm_calls_med": med([r["llm_calls"] for r in runs]),
            "llm_calls_total": sum(r["llm_calls"] for r in runs), "n_traces": len(runs)}


def baseline_ext() -> dict:
    d = json.load(open(BASELINE_EXT, encoding="utf-8"))
    runs = []
    passes = n = 0
    for cid, c in d["cases"].items():
        passes += c.get("passes", 0)
        n += c.get("runs", 0)
        for tr in c.get("traces", []):
            runs.append({"wall_s": tr.get("wall_s"), "in_tok": tr.get("est_input_tokens"), "first_llm_s": tr.get("first_llm_latency_s"),
                         "total_llm_s": tr.get("total_llm_latency_s"), "llm_calls": len(tr.get("llm_calls") or [])})
    return {"passes": passes, "runs": n, "pass_rate": round(passes / n, 3), "wilson95": wilson(passes, n),
            "wall_s_med": med([r["wall_s"] for r in runs]), "in_tok_med": med([r["in_tok"] for r in runs]),
            "first_llm_s_med": med([r["first_llm_s"] for r in runs]), "total_llm_s_med": med([r["total_llm_s"] for r in runs]),
            "llm_calls_med": med([r["llm_calls"] for r in runs]), "llm_calls_total": sum(r["llm_calls"] for r in runs), "n_traces": len(runs)}


def gate_energy(jsonl_patterns: list[str], data_dirs: list[Path]) -> dict:
    """Energy and wall per solved task from smoke_gate/report jsonl + counters in the same dirs."""
    rows_all = []
    for d in data_dirs:
        rows, _ = load_counters(d)
        rows_all += rows
    rows_all.sort(key=lambda r: r["t"])
    scale = calibrate_energy(rows_all)
    cases = []
    for pat in jsonl_patterns:
        for f in glob.glob(pat):
            for ln in open(f, encoding="utf-8"):
                if ln.strip():
                    cases.append(json.loads(ln))
    j_total = 0.0
    passes = runs = invalid = 0
    wall = 0.0
    joined = 0
    for c in cases:
        e = energy_j(rows_all, "sys_e", c["t_start"], c["t_end"], scale)
        if e is None:
            w = window(rows_all, c["t_start"], c["t_end"])
            p = st.fmean([r["sys_w"] for r in w if r.get("sys_w")]) if w else None
            e = p * c["wall_s"] if p else None
        if e is not None:
            j_total += e
            joined += 1
        passes += c.get("passes") or 0
        runs += c.get("runs") or 0
        invalid += c.get("invalid_runs") or 0
        wall += c.get("wall_s") or 0
    return {"cases": len(cases), "joined": joined, "passes": passes, "runs": runs, "invalid": invalid,
            "j_per_pass": round(j_total / passes, 1) if passes and joined else None,
            "wall_per_pass_s": round(wall / passes, 1) if passes else None, "total_j": round(j_total, 1)}


def agg(summary: dict, backend: str, tag: str, phase: str, key: str, turn=None, workers=None):
    for a in summary["aggregate"]:
        if a["backend"] == backend and a["tag"] == tag and a["phase"] == phase and (turn is None or a.get("turn") == turn) \
                and (workers is None or a.get("workers") == workers):
            return a.get(key)
    return None


def jobs(summary: dict, backend: str, tag: str) -> float | None:
    alone = during = None
    for j in summary["jobs"]:
        if j["backend"] == backend and j["tag"] == tag:
            if j["mark"] == "fg_job_alone":
                alone = j
            elif j["mark"] == "fg_job_during_inference":
                during = j
    if not (alone and during):
        return None
    pct = [(during[k] / alone[k] - 1) * 100 for k in ("pyloop_1core_s", "sha256_256mb_s", "pyloop_12core_s") if alone.get(k)]
    return round(max(pct), 1)


def _batt(tag: str) -> dict | None:
    f = AB / f"battery_{tag}.json"
    return json.load(open(f, encoding="utf-8")) if f.exists() else None


def batt_hours(tag: str):
    b = _batt(tag)
    return b["phases"]["server idle"].get("battery_hours_at_this_rate") if b else None


def batt_conv(tag: str):
    b = _batt(tag)
    if not b:
        return None
    convs = [p["mwh"] for k, p in b["phases"].items() if k.startswith("conversation") and p.get("mwh") and p["seconds"] < 200]
    return round(st.median(convs), 1) if convs else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/summary"))
    ap.add_argument("--gate", action="store_true")
    ap.add_argument("--gate-pass-rate", type=float, default=0.65, help="Wilson lower bound the v26 extended pass rate must clear")
    args = ap.parse_args()
    S = json.load(open(SUMMARY, encoding="utf-8"))
    SA = json.load(open(SUMMARY_AB, encoding="utf-8"))

    ext_off = ext_summary(load_ext(str(AB / "smoke_ext022_*_*.json")))
    ext_on = ext_summary(load_ext(str(AB / "smoke_ext_poll1_*_*.json")))
    base = baseline_ext()
    en_off = gate_energy([str(AB / "smoke_ext022_*_cases.jsonl")], [AB])
    en_on = gate_energy([str(AB / "smoke_ext_poll1_*_cases.jsonl")], [AB])
    sm_off = gate_energy([str(AB / "smoke_fork022b_cases.jsonl"), str(AB / "smoke_fork022c_asyncinit_cases.jsonl")], [AB])
    sm_251 = gate_energy([str(DATA / "smoke_npu_cases.jsonl")], [DATA])
    sm_lcpp = gate_energy([str(DATA / "smoke_llamacpp_cases.jsonl")], [DATA])

    # request denominators for robustness (LLM calls in evals + bench requests + stall hunts)
    def count_bench(tag_filter):
        n = 0
        for f in (DATA / "requests.jsonl", AB / "requests.jsonl"):
            if f.exists():
                for ln in open(f, encoding="utf-8"):
                    q = json.loads(ln)
                    if q.get("phase") != "mark" and q.get("backend") == "npu" and tag_filter(q.get("tag", "")):
                        n += q.get("attempts", 1)
        return n
    stall = sum(1 for ln in open(AB / "stall_hunt.jsonl", encoding="utf-8") if ln.strip()) if (AB / "stall_hunt.jsonl").exists() else 0
    req_251 = base["llm_calls_total"] + ext_on["llm_calls_total"] + count_bench(lambda t: t == "main") + sm_251["runs"] * 3
    req_26 = ext_off["llm_calls_total"] + count_bench(lambda t: t != "main") + stall + sm_off["runs"] * 3

    # --- axes ---------------------------------------------------------------
    # (name, direction, target, unit, {config: value}, note)
    idle_off = SA["idle"].get("npu/poll0", {}).get("sys_w")
    axes = [
        ("task pass rate, extended suite", "higher", 0.83, "fraction",
         {"v251": base["pass_rate"], "v26": ext_off["pass_rate"], "lcpp": None, "ceiling": 0.83},
         f"v2.5.1: {base['passes']}/{base['runs']} (5 runs/case, Wilson {base['wilson95']}); 2.6: {ext_off['passes']}/{ext_off['runs']} "
         f"(Wilson {ext_off['wilson95']}); paired polling-on control {ext_on['passes']}/{ext_on['runs']}. Target = project baseline 97/117. "
         "llama.cpp: not run on the extended suite; smoke 14/14 valid with 6 client timeouts."),
        ("wall per task, extended suite", "lower", 20.0, "s",
         {"v251": ext_on["wall_s_med"], "v26": ext_off["wall_s_med"], "lcpp": None, "ceiling": 7.2},
         "median trace wall (Enter to final message, no harness overhead), paired runs same evening; v2.5.1 column uses the polling-on control. Ceiling: today's value — prompt length is not a lever (PROMPT_LEVER.md §3.2) and the rebuild share seen in evals is a harness artefact (§7); the per-call floor is the 0.7 s wake-up plus the model's own tokens."),
        ("energy per solved task, extended suite", "lower", 200.0, "J",
         {"v251": en_on["j_per_pass"], "v26": en_off["j_per_pass"], "lcpp": sm_lcpp["j_per_pass"], "ceiling": 215.0},
         f"system joules over the case window / passes; harness overhead included. llama.cpp value is from its smoke suite ({sm_lcpp['passes']} passes, 6 invalid). Ceiling: today's value; prompt-length savings measured at zero and the rebuild share is a harness artefact (PROMPT_LEVER.md §4, §7)."),
        ("first LLM call, whole latency", "lower", 4.0, "s",
         {"v251": ext_on["first_llm_s_med"], "v26": ext_off["first_llm_s_med"], "lcpp": None, "ceiling": 3.0},
         f"median first LLM call per run (prefill + generation of the first step) from eval traces, paired same-evening runs; the earlier 5-run baseline measured {base['first_llm_s_med']} s. Polling off adds wake-up latency to every call."),
        ("first turn after server start", "lower", 1.0, "s",
         {"v251": 4.2, "v26": 0.85, "lcpp": 1.3, "ceiling": 0.85},
         "fresh server each rep, A/B with and without the start-up prime; llama.cpp = warm prefix."),
        ("follow-up turn, first token", "lower", 1.0, "s",
         {"v251": agg(S, "npu", "main", "hex_turn2", "ttft_s", turn=2), "v26": agg(SA, "npu", "poll0", "hex_turn2", "ttft_s", turn=2),
          "lcpp": agg(S, "llamacpp", "main", "hex_turn2", "ttft_s", turn=2), "ceiling": 0.7},
         "four-turn Hex conversation, turn 2 (+350 tokens); ceiling = measured pure extension cost."),
        ("prompt tokens per task (input effectiveness)", "lower", 1500.0, "tokens",
         {"v251": ext_on["in_tok_med"], "v26": ext_off["in_tok_med"], "lcpp": None, "ceiling": 2280.0},
         "median estimated input tokens per run; the 2,331-token system prompt is most of it. Ceiling: the dedented prompt (−124 tokens), the only cut that changes no words and no outputs; every rule cut regressed and buys no speed (PROMPT_LEVER.md §2–4)."),
        ("LLM calls per task (flow)", "lower", 2.0, "calls",
         {"v251": ext_on["llm_calls_med"], "v26": ext_off["llm_calls_med"], "lcpp": None, "ceiling": 2.0},
         "median LLM calls per run; every extra step re-sends the transcript."),
        ("in-line rebuilds per turn (flow)", "lower", 0.0, "rebuilds/turn",
         {"v251": 0.0, "v26": 0.0, "lcpp": 0.0, "ceiling": 0.0},
         "six-turn real Hex scenario with 10 s think time: 0 in-line rebuilds on 2.6 (prewarm hid all); 2.5.1 has the same prewarm and is assumed equal (not re-measured). Score: 100 at 0, 50 at 1/turn."),
        ("decode at Hex context", "higher", 15.0, "tok/s",
         {"v251": agg(S, "npu", "main", "hex_turn2", "decode_tps", turn=2), "v26": agg(SA, "npu", "poll0", "hex_turn2", "decode_tps", turn=2),
          "lcpp": agg(S, "llamacpp", "main", "hex_turn2", "decode_tps", turn=2), "ceiling": 14.8},
         "turn-2 decode rate at ~2.7K tokens. Ceiling: today's rate — the decode curve is flat from 2,000 to 2,750 tokens and no safe prompt leaves that step (PROMPT_LEVER.md §3.1)."),
        ("idle power, server loaded", "lower", 4.4, "W",
         {"v251": S["idle"]["npu/main"]["sys_w"], "v26": idle_off, "lcpp": S["idle"]["llamacpp/main"]["sys_w"], "ceiling": 4.4},
         "machine floor 3.4 W; target = floor + 1 W."),
        ("SoC temperature, sustained decode", "lower", 60.0, "°C",
         {"v251": S["sustain"][[s["backend"] for s in S["sustain"]].index("npu")]["last_temp_tz2"],
          "v26": agg(SA, "npu", "cand", "sustain", "m_temp_tz2"), "lcpp": S["sustain"][[s["backend"] for s in S["sustain"]].index("llamacpp")]["last_temp_tz2"], "ceiling": 43.0},
         "TZ2 at the end of a 240 s decode run."),
        ("foreground work slowdown (components)", "lower", 5.0, "%",
         {"v251": max(jobs(S, "npu", "main"), 0), "v26": max(jobs(SA, "npu", "cand") or 0, 0), "lcpp": jobs(S, "llamacpp", "main"), "ceiling": 0.0},
         "worst of three foreground jobs while the backend decodes. Score: 100 at ≤ 5 %."),
        ("memory footprint", "lower", 4.0, "GB",
         {"v251": 5.8, "v26": 5.8, "lcpp": 2.85, "ceiling": 5.8},
         "NPU shared memory for the 4-part W4A16 bundle; no known lever on this runtime, so the ceiling equals today."),
        ("robustness on AC: hangs per 100 requests", "lower", 0.0, "hangs/100",
         {"v251": 0.0, "v26": round(100 / req_26, 3), "lcpp": None, "ceiling": 0.0},
         f"v2.5.1: 0 hangs in ~{req_251} requests on record today; 2.6: 1 hang (unreproduced) in ~{req_26} requests (eval LLM calls + bench + {stall} stall-hunt requests). Score: 100 − 20 × hangs per 100."),
        ("robustness on AC at 3K context: hangs per 100 requests", "lower", 0.0, "hangs/100",
         {"v251": None, "v26": 45.0, "lcpp": None, "ceiling": 4.0},
         "stall_rate.py, AC, Hex prompt + 800-token tool result (~3,065 tokens), natural stops, 2026-09-06: 2.6.1 default (async init on) 9 hangs in 20; "
         "async init off 1 in 25 (the 2.6.2 default). v2.5.1 not run on this probe (its 0.2.1 binary had no async init; 406 baseline calls incl. ten at 3.3K hung 0 times). Score: 100 − 2 × hangs per 100."),
        ("robustness on battery: hangs per 100 requests (platform)", "lower", 0.0, "hangs/100",
         {"v251": round(100 * 1 / 31, 1), "v26": round(100 * 5 / 60, 1), "lcpp": None, "ceiling": round(100 * 1 / 31, 1)},
         "unplugged, four-turn conversations at 3–3.5K tokens: polling on 1 hang in 31 requests; polling off 5 in ~60 across four variants "
         "(default, rpc_control_latency 10, sustained profile, one busy CPU core). Same signature every time: HTP 'Failed to execute graph. Error 1011', "
         "query blocked ~380 s until the client timeout, watchdog abort ineffective. Present in the untouched 0.2.1 configuration, so a platform/driver issue on DC power, not a 2.6 regression. Score: 100 − 20 × hangs per 100."),
        ("robustness: invalid eval runs", "lower", 0.0, "runs",
         {"v251": ext_on["invalid"], "v26": ext_off["invalid"], "lcpp": sm_lcpp["invalid"], "ceiling": 0.0},
         "backend-unreachable/timeouts across the paired extended suites (82 runs each) and llama.cpp's smoke (20 runs)."),
        ("battery: hours with the server idle", "higher", 6.5, "h",
         {"v251": batt_hours("v251_poll1"), "v26": batt_hours("v26_poll0"), "lcpp": None, "ceiling": batt_hours("v26_poll0")},
         "unplugged, discharge rate from the battery itself (WMI) over 60 s with the server loaded and idle; machine idle alone 7.5 W ≈ 6.8 h. Target: within 0.3 h of the machine's own idle."),
        ("battery: mWh per 4-turn conversation", "lower", 200.0, "mWh",
         {"v251": batt_conv("v251_poll1"), "v26": batt_conv("v26_poll0"), "lcpp": None, "ceiling": 150.0},
         "unplugged; median of the non-hung four-turn conversations with 5 s think time. Ceiling: shorter prompt."),
        ("fan / acoustic", "lower", None, "dB", {"v251": None, "v26": None, "lcpp": None, "ceiling": None},
         "NOT MEASURED. Drawn as a gap."),
    ]

    def score(direction, target, value):
        if value is None or target is None:
            return None
        if direction == "lower":
            if target == 0:
                return round(max(0.0, 100 - 20 * value), 1) if "hang" in direction else round(100 / (1 + value), 1)
            return round(min(100.0, 100 * target / value), 1) if value > 0 else 100.0
        return round(min(100.0, 100 * value / target), 1)

    rows = []
    for name, direction, target, unit, vals, note in axes:
        r = {"axis": name, "direction": direction, "target": target, "unit": unit, "values": vals, "note": note, "scores": {}}
        for cfg, v in vals.items():
            if "hangs" in name and v is not None:
                r["scores"][cfg] = round(max(0.0, 100 - 20 * v), 1)
            elif "rebuilds" in name and v is not None:
                r["scores"][cfg] = round(100 / (1 + v), 1)
            elif "slowdown" in name and v is not None:
                r["scores"][cfg] = 100.0 if v <= 5 else round(min(100.0, 100 * 5 / v), 1)
            elif "invalid" in name and v is not None:
                r["scores"][cfg] = round(max(0.0, 100 - 10 * v), 1)
            else:
                r["scores"][cfg] = score(direction, target, v)
        rows.append(r)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    result = {"unit_of_value": "one solved task", "configs": {"v251": "Hex v2.5.1, npurun 0.2.1 (polling on)",
                                                             "v26": "Hex 2.6 candidate: npurun 0.2.2 polling off + async init + start-up prime",
                                                             "lcpp": "CPU reference: upstream llama.cpp Q4_0 (-t 6 -tb 12)",
                                                             "ceiling": "best achievable per axis from levers that exist"},
              "axes": rows, "sources": {"ext_off": ext_off, "ext_on": ext_on, "baseline_251": base, "energy_ext_off": en_off,
                                        "energy_ext_on": en_on, "smoke_26": sm_off, "smoke_251": sm_251, "smoke_lcpp": sm_lcpp,
                                        "request_denominators": {"v251": req_251, "v26": req_26}}}
    (out / "radar.json").write_text(json.dumps(result, indent=1), encoding="utf-8")

    md = ["# Optimisation radar (per solved task, fixed targets)", "",
          "| axis | target | v2.5.1 | 2.6 | llama.cpp | ceiling | scores (v2.5.1 / 2.6 / llama.cpp / ceiling) |", "|---|---|---|---|---|---|---|"]
    for r in rows:
        v = r["values"]
        def fmt(x, unit=r["unit"]):
            return "—" if x is None else (f"{x:.3f}" if isinstance(x, float) and x < 1 and unit == "fraction" else f"{x:g}")
        sc = " / ".join("—" if r["scores"].get(c) is None else f"{r['scores'][c]:g}" for c in ("v251", "v26", "lcpp", "ceiling"))
        tgt = "—" if r["target"] is None else f"{r['target']:g}"
        md.append(f"| {r['axis']} | {tgt} {r['unit']} | {fmt(v['v251'])} | {fmt(v['v26'])} | {fmt(v['lcpp'])} | {fmt(v['ceiling'])} | {sc} |")
    md += ["", "## Notes per axis", ""] + [f"- **{r['axis']}**: {r['note']}" for r in rows]
    (out / "radar.md").write_text("\n".join(md) + "\n", encoding="utf-8")
    print("\n".join(md[:len(rows) + 3]))

    if args.gate:
        lo = ext_off["wilson95"][0]
        ok = lo >= args.gate_pass_rate and ext_off["invalid"] == 0
        print(f"GATE {'PASS' if ok else 'FAIL'}: 2.6 extended pass-rate Wilson lower bound {lo} (threshold {args.gate_pass_rate}), invalid runs {ext_off['invalid']}")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
