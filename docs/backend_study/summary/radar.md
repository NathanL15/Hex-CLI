# Optimisation radar (per solved task, fixed targets)

| axis | target | v2.5.1 | 2.6 | llama.cpp | ceiling | scores (v2.5.1 / 2.6 / llama.cpp / ceiling) |
|---|---|---|---|---|---|---|
| task pass rate, extended suite | 0.83 fraction | 0.793 | 0.756 | — | 0.830 | 95.5 / 91.1 / — / 100 |
| wall per task, extended suite | 20 s | 7.16 | 8 | — | 12 | 100 / 100 / — / 100 |
| energy per solved task, extended suite | 200 J | 453.8 | 215.4 | 662.3 | 180 | 44.1 / 92.9 / 30.2 / 100 |
| first LLM call, whole latency | 4 s | 5.53 | 6.19 | — | 3 | 72.3 / 64.6 / — / 100 |
| first turn after server start | 1 s | 4.2 | 0.85 | 1.3 | 0.85 | 23.8 / 100 / 76.9 / 100 |
| follow-up turn, first token | 1 s | 0.989 | 1.151 | 3.565 | 0.7 | 100 / 86.9 / 28.1 / 100 |
| prompt tokens per task (input effectiveness) | 1500 tokens | 2409 | 2409 | — | 2280 | 62.3 / 62.3 / — / 65.8 |
| LLM calls per task (flow) | 2 calls | 1 | 1 | — | 2 | 100 / 100 / — / 100 |
| in-line rebuilds per turn (flow) | 0 rebuilds/turn | 0 | 0 | 0 | 0 | 100 / 100 / 100 / 100 |
| decode at Hex context | 15 tok/s | 10.391 | 11.771 | 11.911 | 14.8 | 69.3 / 78.5 / 79.4 / 98.7 |
| idle power, server loaded | 4.4 W | 14.351 | 4.4 | 4.916 | 4.4 | 30.7 / 100 / 89.5 / 100 |
| SoC temperature, sustained decode | 60 °C | 69.841 | 41.95 | 95.017 | 43 | 85.9 / 100 / 63.1 / 100 |
| foreground work slowdown (components) | 5 % | 0.7 | 2.6 | 54.1 | 0 | 100 / 100 / 9.2 / 100 |
| memory footprint | 4 GB | 5.8 | 5.8 | 2.85 | 5.8 | 69 / 69 / 100 / 69 |
| robustness on AC: hangs per 100 requests | 0 hangs/100 | 0 | 0.133 | — | 0 | 100 / 97.3 / — / 100 |
| robustness on AC at 3K context: hangs per 100 requests | 0 hangs/100 | — | 45 | — | 4 | — / 0 / — / 20 |
| robustness on battery: hangs per 100 requests (platform) | 0 hangs/100 | 3.2 | 8.3 | — | 3.2 | 36 / 0 / — / 36 |
| robustness: invalid eval runs | 0 runs | 0 | 0 | 3 | 0 | 100 / 100 / 70 / 100 |
| battery: hours with the server idle | 6.5 h | 4.8 | 6.9 | — | 6.9 | 73.8 / 100 / — / 100 |
| battery: mWh per 4-turn conversation | 200 mWh | 241.8 | 189.9 | — | 150 | 82.7 / 100 / — / 100 |
| fan / acoustic | — dB | — | — | — | — | — / — / — / — |

## Notes per axis

- **task pass rate, extended suite**: v2.5.1: 165/208 (5 runs/case, Wilson (0.733, 0.843)); 2.6: 62/82 (Wilson (0.653, 0.836)); paired polling-on control 61/82. Target = project baseline 97/117. llama.cpp: not run on the extended suite; smoke 14/14 valid with 6 client timeouts.
- **wall per task, extended suite**: median trace wall (Enter to final message, no harness overhead), paired runs same evening; v2.5.1 column uses the polling-on control. Ceiling: the 20 % of first calls that pay an in-line rebuild (PROMPT_LEVER.md §3.3) moved to a background prewarm; prompt length is not a lever (§3.2).
- **energy per solved task, extended suite**: system joules over the case window / passes; harness overhead included. llama.cpp value is from its smoke suite (14 passes, 6 invalid). Ceiling: 2.6 minus the in-line rebuild share (~20 % of LLM time); prompt-length savings measured at zero (PROMPT_LEVER.md).
- **first LLM call, whole latency**: median first LLM call per run (prefill + generation of the first step) from eval traces, paired same-evening runs; the earlier 5-run baseline measured 3.41 s. Polling off adds wake-up latency to every call.
- **first turn after server start**: fresh server each rep, A/B with and without the start-up prime; llama.cpp = warm prefix.
- **follow-up turn, first token**: four-turn Hex conversation, turn 2 (+350 tokens); ceiling = measured pure extension cost.
- **prompt tokens per task (input effectiveness)**: median estimated input tokens per run; the 2,331-token system prompt is most of it. Ceiling: the dedented prompt (−124 tokens), the only cut that changes no words and no outputs; every rule cut regressed and buys no speed (PROMPT_LEVER.md §2–4).
- **LLM calls per task (flow)**: median LLM calls per run; every extra step re-sends the transcript.
- **in-line rebuilds per turn (flow)**: six-turn real Hex scenario with 10 s think time: 0 in-line rebuilds on 2.6 (prewarm hid all); 2.5.1 has the same prewarm and is assumed equal (not re-measured). Score: 100 at 0, 50 at 1/turn.
- **decode at Hex context**: turn-2 decode rate at ~2.7K tokens. Ceiling: today's rate — the decode curve is flat from 2,000 to 2,750 tokens and no safe prompt leaves that step (PROMPT_LEVER.md §3.1).
- **idle power, server loaded**: machine floor 3.4 W; target = floor + 1 W.
- **SoC temperature, sustained decode**: TZ2 at the end of a 240 s decode run.
- **foreground work slowdown (components)**: worst of three foreground jobs while the backend decodes. Score: 100 at ≤ 5 %.
- **memory footprint**: NPU shared memory for the 4-part W4A16 bundle; no known lever on this runtime, so the ceiling equals today.
- **robustness on AC: hangs per 100 requests**: v2.5.1: 0 hangs in ~735 requests on record today; 2.6: 1 hang (unreproduced) in ~753 requests (eval LLM calls + bench + 310 stall-hunt requests). Score: 100 − 20 × hangs per 100.
- **robustness on AC at 3K context: hangs per 100 requests**: stall_rate.py, AC, Hex prompt + 800-token tool result (~3,065 tokens), natural stops, 2026-09-06: 2.6.1 default (async init on) 9 hangs in 20; async init off 1 in 25 (the 2.6.2 default). v2.5.1 not run on this probe (its 0.2.1 binary had no async init; 406 baseline calls incl. ten at 3.3K hung 0 times). Score: 100 − 2 × hangs per 100.
- **robustness on battery: hangs per 100 requests (platform)**: unplugged, four-turn conversations at 3–3.5K tokens: polling on 1 hang in 31 requests; polling off 5 in ~60 across four variants (default, rpc_control_latency 10, sustained profile, one busy CPU core). Same signature every time: HTP 'Failed to execute graph. Error 1011', query blocked ~380 s until the client timeout, watchdog abort ineffective. Present in the untouched 0.2.1 configuration, so a platform/driver issue on DC power, not a 2.6 regression. Score: 100 − 20 × hangs per 100.
- **robustness: invalid eval runs**: backend-unreachable/timeouts across the paired extended suites (82 runs each) and llama.cpp's smoke (20 runs).
- **battery: hours with the server idle**: unplugged, discharge rate from the battery itself (WMI) over 60 s with the server loaded and idle; machine idle alone 7.5 W ≈ 6.8 h. Target: within 0.3 h of the machine's own idle.
- **battery: mWh per 4-turn conversation**: unplugged; median of the non-hung four-turn conversations with 5 s think time. Ceiling: shorter prompt.
- **fan / acoustic**: NOT MEASURED. Drawn as a gap.
