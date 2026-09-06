# Hex CLI on CPU vs NPU

A measured comparison of running Hex CLI's model, Qwen3-4B-Instruct-2507 at 4-bit, on the
Snapdragon X Elite's Hexagon NPU versus its Oryon CPU cores. Measured 2026-09-05 on a
freshly rebooted, quarantined Lenovo (X1E-78-100, 12 cores, 16 GB, Windows 11 ARM64, on AC).
Raw data: `docs/backend_study/data/`, analysis: `summary/`, harness: `tools/backend_bench/`,
protocol: `RUNBOOK.md`.

## 1. Summary

| | NPU (npurun) | CPU, Ollama | CPU, upstream llama.cpp |
|---|---|---|---|
| **New Hex conversation, first token** | 9.4 s in this benchmark (a nonce inside the prompt forced a rebuild; see §12: same prompt 0.8 s, changed prompt 6.6–8 s) | 74 s first ever, 2.9 s once the prompt prefix is cached | 15 s first ever, 1.1 s once cached |
| **Follow-up turn (transcript + 350 tokens)** | **1.0 s** (turn 2), 1.6 s (turn 3), **10.5 s at turn 4** (cache cliff at ~3.2K tokens) | 13.6 / 13.9 / 14.8 s | 3.6 / 3.9 / 4.3 s |
| **Decode, short context** | 15.5 tok/s | 23.9 tok/s | **31.4 tok/s** |
| **Decode at Hex context (2.4–3.4K)** | 8.8–10.4 tok/s | 8.9–12.2 tok/s | 9.4–13.3 tok/s |
| **Cold prefill, 2,048 tokens** | 8.3 s (≈5 s rebuild + ≈620 tok/s) | 67.7 s (30 tok/s) | 11.9 s (172 tok/s) |
| **System power while decoding** | **21.3 W** | 43.0 W | 36.0 W |
| **Energy per generated token** | 1.44 J (1.21 above machine idle) | 2.23 J (2.06) | **1.19 J** (1.08) |
| **Energy, 4-turn Hex conversation** | **730 J** | 2,100 J (turn 1 already warm) | 1,020 J |
| **CPU during decode** | 30 % (npurun holds 3.3 cores) | 42 % (5.5 cores) | 45 % (6 cores) |
| **Loaded but idle** | **14.4 W** (npurun spins 2.7 cores) | 4.0 W | 4.9 W |
| **Memory taken** | ≈5.8 GB (shared NPU memory) | 3.0 GB | 2.85 GB |
| **SoC temperature, 4 min sustained** | 67→70 °C | 93 °C | 91→95 °C |
| **Decode with 12 busy cores** | 17.6 tok/s, **no loss** | 18.6 tok/s (−22 %) | **0.8 tok/s (−97 %)**; 18.3 with passive OpenMP waits |
| **Your other work slows by** | 0 % | 15–82 % | 14–54 % |
| **Hex smoke suite (10 cases × 2)** | 20/20 pass, 327 s, 6.35 kJ | not run (cold turn ≈ 74 s) | 14/14 valid pass, **6 runs timed out** in Hex's client, 200 s, 9.27 kJ |

Machine idle with no server: 3.4 W. Cold start to first accepted request: npurun 7.0 s, Ollama 4.1 s, llama-server 5.6 s.

**What this means for Hex.** The NPU is the right default for the way Hex actually works: a
2,300-token stable prompt plus short turns. Continuing a conversation costs about one second
to first token and a quarter of the CPU-tier energy, with the CPU free for everything else and
the chassis 25 °C cooler. Its weaknesses are specific and fixable: a changed prompt, a discarded
tail over ~600 tokens or a cache past ~3.3K tokens costs a 6.6–8 s dialog rebuild (the
benchmark's "9 s per new conversation" turned out to be its own nonce, §12), the fourth turn of a
long conversation lands on that cliff, and before 2.6 the resident server burned 11 W doing
nothing. Upstream llama.cpp is the surprise: it decodes twice as fast as the NPU at
short context at the same energy per token and gets a new conversation going in about a
second once the prompt is cached, but it heats the SoC to 95 °C, takes six cores, and collapses
to under one token per second when anything else saturates the CPU. Ollama, the path Hex's
`backend: ollama` uses today, prefills at 30 tok/s on this machine (its bundled build lacks
the fast ARM kernels) and is not usable for Hex-sized prompts.

## 2. Setup

| | NPU tier | CPU tier A | CPU tier B |
|---|---|---|---|
| Server | npurun 0.2.1 `serve` (Genie 1.18 / QAIRT 2.47, Hexagon HTP), Rewind runtime `NPURUN_REWIND=2` | Ollama 0.30.9, `OLLAMA_CONTEXT_LENGTH=4096`, keep-alive forever | llama.cpp b10819 `llama-server`, `-t 6 -tb 12 -c 4096` |
| Model | qwen3-4b-instruct-2507 W4A16 bundle, 4,096 ctx | `qwen3:4b-instruct-2507-q4_K_M` (2.5 GB) | `Qwen3-4B-Instruct-2507` Q4_0 (2.4 GB, ARM repack kernels) |
| Threads | 3 host threads (observed 3.3 cores busy) | 6 of 12 (Ollama default) | 6 decode, 12 prefill |
| Transport | OpenAI-compatible stream with `session_id`, exactly Hex's production path | native `/api/chat` stream with `num_ctx 4096` | OpenAI-compatible stream |
| Prefix reuse | Genie KV Rewind, transcript-granular | llama.cpp prefix cache | llama.cpp prefix cache |

NPU driver 30.0.220.3000, Windows 11 26200. Temperature 0.1 and Hex's stop tokens
everywhere. Quantizations differ (W4A16 vs Q4_K_M vs Q4_0); that is a confound for quality,
not for the timing and power results here.

**Instruments.** Windows exposes this laptop's hardware energy meters as performance
counters (`Energy Meter`: CPU clusters, GPU, SYS) at 1 Hz; energy per request is the
difference of the cumulative counter across the request window (calibrated: 1 count =
3.65 nJ). There is no NPU rail: NPU draw is the SYS residual. NPU utilisation is the
Task Manager counter (`GPU Engine`, the NPU's adapter, a single Compute engine), re-enumerated
every 15 s because a Genie dialog rebuild invalidates the instance. Token counts use one
tokenizer (llama-tokenize on the Qwen GGUF) for all tiers. Every tier started from a guarded
quiet state (CPU < 2 %, SoC < 55 °C, process list empty of servers and helpers, HWiNFO and
iCloud stopped, other Claude sessions offline).

## 3. Prompt processing (time to first token)

Cold: a unique system prompt, nothing cached anywhere. Prefixed: Hex's real pattern, the same
2,331-token system prompt every time and N new user tokens. Append: the same session
continues with +40 tokens. Medians of 3.

| prompt tokens | NPU cold | Ollama cold | llama.cpp cold | NPU prefixed | Ollama prefixed | llama.cpp prefixed | NPU append | Ollama append | llama.cpp append |
|---|---|---|---|---|---|---|---|---|---|
| 64 | 0.47 s | 2.2 s | 0.28 s | 8.7 s | 2.9 s | 1.1 s | 0.56 s | 0.61 s | 0.10 s |
| 256 | 0.66 s | 7.1 s | 0.89 s | 8.9 s | 9.5 s | 2.6 s | 5.5 s* | 0.67 s | 0.11 s |
| 512 | – | – | – | 9.3 s | 19.3 s | 4.8 s | – | – | – |
| 1,024 | 7.0 s | 30.9 s | 4.4 s | 10.2 s | 40.7 s | 11.1 s | 6.7 s* | 1.09 s | 0.21 s |
| 2,048 | 8.3 s | 67.7 s | 11.9 s | – | – | – | 8.3 s* | 1.46 s | 0.43 s |

\* Rewind failed (Genie status −1, empty stream), Hex-style retry paid a dialog rebuild.

The NPU's raw prefill is fast: subtracting the ≈5 s rebuild, 2,048 tokens took ≈3.3 s
(≈620 tok/s), versus 172 tok/s on the best CPU path and 30 tok/s through Ollama. But on this
runtime a divergent prompt of 1K tokens or more costs the rebuild first, so the NPU's
*observed* cold TTFT sits between the two CPU tiers, and a new Hex conversation costs about
9 s regardless of how short the user's message is. Both CPU tiers reuse the cached 2.3K prefix
at token granularity, so a new conversation costs only the new user tokens.

## 4. A real Hex conversation

Production system prompt, then three follow-ups that each append a 300-token tool result, the
way an agent loop grows its transcript. Medians of 3 conversations; turn 1 on the CPU tiers is
already warm because the prompt was cached by the previous phase (cold values in section 3).

| turn (tokens) | NPU TTFT | NPU energy | Ollama TTFT | Ollama energy | llama.cpp TTFT | llama.cpp energy |
|---|---|---|---|---|---|---|
| 1 (2,331) | 9.4 s | 194 J | 3.7 s (warm) | 175 J | 1.3 s (warm) | 104 J |
| 2 (2,680) | 1.0 s | 117 J | 13.6 s | 554 J | 3.6 s | 227 J |
| 3 (3,020) | 1.6 s | 141 J | 13.9 s | 578 J | 3.9 s | 309 J |
| 4 (3,400) | 10.5 s | 278 J | 14.8 s | 790 J | 4.3 s | 384 J |

Turn 4 on the NPU crosses the runtime's divergent-Rewind limit (about 3,200 cached tokens)
and pays a full rebuild plus re-prefill; the memory notes for the project predicted this
limit. Decode at these context lengths is 9–10 tok/s on all three tiers: at Hex's context the
NPU and CPU generate at the same speed, and the whole difference is in prompt handling.

## 5. Generation speed and energy

Short prompt (≈80 tokens), 256 tokens requested, medians of 3, cross-checked by the 240-second
sustained run (18–27 requests).

| | NPU | Ollama | llama.cpp |
|---|---|---|---|
| decode tok/s | 15.5 (sustained 15.7) | 23.9 (23.0) | 31.4 (30.8) |
| system power | 21.3 W | 43.0 W | 36.0 W |
| CPU power (3 clusters) | 9.3 W | 24.9 W | 21.6 W |
| CPU utility | 30 % | 42 % | 45 % |
| server process CPU | 333 % (3.3 cores) | 551 % | 604 % |
| NPU utilisation | 71 % | 0 | 0 |
| J per output token | 1.44 | 2.23 | 1.19 |
| J per token above 3.4 W idle | 1.21 | 2.06 | 1.08 |

The NPU's decode power advantage (half the watts) is offset by its lower rate, so per token
it lands level with upstream llama.cpp and 40 % better than Ollama. Notably 9.3 W of the
NPU tier's 21 W is CPU: npurun keeps roughly three cores busy driving the accelerator.

llama.cpp's own benchmark on this CPU (pp512 / tg128, tok/s):

| threads | Q4_K_M prefill | Q4_K_M decode | Q4_0 prefill | Q4_0 decode |
|---|---|---|---|---|
| 4 | 82 | 26.8 | 112 | 30.3 |
| 6 | 104 | 30.7 | 146 | 35.3 |
| 8 | 131 | 32.1 | 189 | 36.6 |
| 12 | 166 | 33.4 | 242 | 36.5 |

Prefill scales with cores; decode saturates memory bandwidth by 8 threads. Q4_0's ARM
repacking is worth 35–45 % on prefill. Ollama's bundled server, on the same CPU, prefilled at
30–39 tok/s in every test.

## 6. Idle cost, memory, thermals, cold start

| | none | NPU loaded | Ollama loaded | llama.cpp loaded |
|---|---|---|---|---|
| system power, idle | 3.4 W | 14.4 W | 4.0 W | 4.9 W |
| CPU power, idle | 0.1 W | 7.8 W | 0.1 W | 0.1 W |
| server process CPU, idle | – | 267 % | 0.5 % | 0.5 % |
| available memory | 8.3 GB | 2.5 GB | 5.9 GB | 3.9 GB |
| cold start to first reply | – | 7.0 s | 4.1 s | 5.6 s |

A resident npurun costs 11 W while nobody is typing, because its host threads spin-poll
the NPU. That is the single largest energy line in this study for a laptop that keeps Hex open
all day. The CPU tiers idle within 1.5 W of an empty machine.

Sustained four minutes of decode: NPU 15.6→16.0 tok/s at SoC 67→70 °C and 21.8 W; Ollama
23.3→22.8 tok/s at 93 °C and 39.8 W; llama.cpp 30.9→30.7 tok/s at 91→95 °C and 40.4 W,
CPU clock 3.1 GHz. No tier throttled within four minutes, but the CPU tiers run the SoC at
its thermal ceiling.

## 7. Running other things at the same time

Background load: N processes spinning on SHA-256 (ALU-bound, cache-resident, no memory
bandwidth). Foreground job: a single-core Python loop, SHA-256 over 256 MB, and the loop
spread over 12 processes, timed alone and while a backend decodes continuously.

| decode tok/s under load | 0 cores busy | 4 busy | 12 busy |
|---|---|---|---|
| NPU | 15.9 | 15.7 | 17.6 |
| Ollama | 23.4 | 21.5 | 18.6 |
| llama.cpp (default) | 31.4 | 29.6 | 0.8 (one 320 s request) |
| llama.cpp `--poll 0` | 33.8 | – | 1.4 |
| llama.cpp `OMP_WAIT_POLICY=PASSIVE` | 20.6 | – | 18.3 |

| foreground job (s), alone → during inference | 1-core loop | sha256 256 MB | 12-core loop |
|---|---|---|---|
| NPU | 2.33 → 2.20 | 0.58 → 0.58 | 0.97 → 0.98 |
| Ollama | 2.15 → 2.48 | 0.58 → 0.95 | 0.92 → 1.67 |
| llama.cpp | 2.27 → 2.92 | 0.66 → 1.01 | 0.96 → 1.10 |

The NPU tier is invisible to other work in both directions. Upstream llama.cpp's collapse
under a saturated CPU is its OpenMP threadpool spin-waiting for descheduled workers; a passive
wait policy fixes contention at the cost of 35 % of unloaded decode speed. Ollama's runner
(no OpenMP) degrades gracefully.

## 8. End to end: Hex's smoke suite

Ten smoke cases, two runs each, seed 20260905, fresh server, Hex's real agent loop.

| | NPU | llama.cpp |
|---|---|---|
| passes / valid runs | 20 / 20 | 14 / 14 |
| invalid runs | 0 | 6 ("backend unreachable: timed out", Hex's client gave up during a cold 2.3K prefill of 15–20 s) |
| wall, all cases (incl. ≈8 s harness overhead per case) | 327 s | 200 s |
| mean first-LLM latency | 5.1 s | 7.5 s (2.2–4.9 s when cached, 16–20 s cold) |
| energy | 6.35 kJ (19.4 W average) | 9.27 kJ (46 W average) |
| SoC during suite | 63–65 °C | 91–95 °C |

The CPU tier's wall time is shorter only because six runs aborted early. Its cold turns are
slow enough to trip Hex's own timeout, so Hex as shipped would need a longer first-token
timeout to run on CPU at all. The Ollama tier was not run end to end: at 74 s per cold turn
it would not complete the suite in a useful time.

## 9. Limitations

- Different quantizations per tier (W4A16, Q4_K_M, Q4_0); no quality comparison beyond the
  smoke suite's pass rates.
- NPU power is inferred from the system rail; display and platform power are inside SYS,
  which is why the "above idle" columns exist.
- Background load is a synthetic spinner; a browser or a build also compete for memory
  bandwidth and would hurt the CPU tiers more, not less.
- llama.cpp's 12-core contention level is n=1 (the run was cut after one 320 s request).
- The npurun sampling temperature could not be verified to apply; CPU tiers used 0.1.
- One machine, one day; the runbook reproduces the whole study in about two hours.

## 10. Reproduction

`docs/backend_study/RUNBOOK.md`. Harness: `tools/backend_bench/bench.py` (phases), `guard.py`
(quarantine checkpoint), `analyze.py` and `report_data.py` (joins), `run_suite.py` (Hex's own
suites against another server). Pre-reboot attempts, contaminated by a peer session and by
orphaned load workers, are kept under `_pre_restart/` and were not used.

## 11. Follow-up: optimising the NPU tier (same day)

The idle-spin finding in section 6 turned out to be one configuration bit. The Genie
bundle ships `dialog.engine.backend.QnnHtp.poll = true`, which makes npurun's three host
threads busy-poll the accelerator whether or not a query is running. Every other knob
was A/B-tested with the same harness (`tools/backend_bench/npu_ab.py`, data in
`data_npu_ab/`, 45 s idle + 2×256-token decode + 2 four-turn Hex conversations each):

| variant | idle sys W | decode tok/s | sys W decoding | J/token | turn-2 TTFT | SoC °C |
|---|---|---|---|---|---|---|
| bundle as shipped (poll on, burst) | 14.4 | 15.5 | 21.3 | 1.44 | 1.0 s | 67–70 |
| poll off, burst | **4.4** | **19.2** | **9.8** | **0.55** | 1.15 s | 40–43 |
| poll off, sustained_high_performance | 4.2 | 16.8 | 8.2 | 0.53 | 1.10 s | 40–44 |
| poll off, balanced | 4.2 | 14.2 | 7.9 | 0.60 | 1.23 s | 40–42 |
| poll off, power_saver | 4.0 | 11.4 | 7.2 | 0.68 | 1.76 s | 39–41 |
| poll off, rpc_polling_time 0 | 4.1 | 19.1 | 8.8 | 0.49 | 1.09 s | 40–45 |
| poll off, 2 host threads | 4.3 | 19.6 | 9.4 | 0.54 | 1.14 s | 41–44 |
| poll off, 1 host thread | – | 19.5 | – | – | – | – |

Polling off is worth more than every HTP profile combined: the host threads sleep
(server CPU 0 % idle, 40 % of one core while decoding instead of 330 %), the SoC runs
25 °C cooler, decode is 24 % faster, and energy per token drops 2.6×. The lower
profiles save at most 2 W while decoding at the cost of 25–40 % of speed, so burst
stays. Validation of the chosen setting (poll off, burst, bundle threads):

- Sustained 240 s: 19.0–19.2 tok/s flat, SoC 43 °C.
- 12 busy CPU cores: 21 tok/s (unloaded 19); foreground jobs unchanged.
- Prefill sweep: cold and prefixed TTFT within +0.1–0.3 s of polling on.
- Stall hunt (the one 5-minute hang seen in an early gate): 0 in 264 rebuild-then-follow-up
  cycles, streaming and non-streaming. It did not reproduce; documented as a residual risk
  with `NPURUN_HTP_POLL=1` as the escape hatch.
- Hex smoke suite: 19/20 and 19/20 (baseline 20/20, 19/20).
- Hex extended suite, 41 cases × 2, paired same evening and seed: **62/82 with polling off vs
  61/82 with it on**; first-LLM latency 5.85 vs 5.54 s.

Shipped as hexcli-fork npurun **0.2.2**: the engine patches the Genie config in memory at
dialog creation (`NPURUN_HTP_POLL`, default off; `NPURUN_HTP_THREADS`,
`NPURUN_HTP_PERF_PROFILE`, `NPURUN_HTP_RPC_POLLING_US` for experiments), so pulled bundles
are never modified. Hex's launcher sets `NPURUN_HTP_POLL=0` explicitly and the eval
metadata records it. The remaining levers, in order: prefix reuse across new conversations
(9 s), the Rewind cliff near 3,200 tokens (turn 4), and a watchdog that can actually wake a
non-polling Genie wait.

## 12. Second round: the runtime's rules, and what they leave on the table

Probing the Rewind runtime directly (`tools/backend_bench/rewind_probe.py`, 0.2.2 binary,
Hex's 2,331-token system prompt as the prefix):

| transcript shape | first token | server events |
|---|---|---|
| new conversation, same system prompt, different user turn | 0.7–0.8 s | prefix reused |
| same conversation extended (+40 tokens) | 0.7 s | prefix reused |
| same prompt, previous request's tail ≤ 600 tokens discarded | 0.8 s | prefix reused |
| same prompt, ≥ 1,000 tokens discarded | 5–8 s | Rewind fails, rebuild |
| system prompt differs near the top | 6.6–8.3 s | Rewind fails, rebuild |
| conversation extended past ~3.3K cached tokens | 9.7 s | Rewind fails, rebuild |
| any prompt after an empty cache | 4.2 s (2.3K) | plain prefill, ≈550 tok/s |

So the earlier "9 s per new conversation" was an artefact of the benchmark's per-conversation
nonce inside the system prompt: a real Hex conversation with the same prompt starts in under
a second, and what actually costs a rebuild is a discarded tail over ~600 tokens (history
condensation), a cache past ~3.3K, or a changed prompt. In a real six-turn Hex scenario with
10 s of think time the end-of-turn prewarm hid every rebuild; per-turn cost was the first LLM
call's own generation at 2.4–3.1K context.

Decode speed against context (polling off, 128-token stories): 19.1 tok/s at 44 tokens, 17 at
490, 15.3 at 990, 15.7 at 1,500, 13.5 at 2,000, 13.6 at 2,550. The 2.3K system prompt costs
about 30 % of decode speed on every step, which makes prompt length the largest remaining
latency lever, and it is a Hex-side one.

Shipped in this round (fork 0.2.2, same binary):
- `allow-async-init` on by default: dialog creation 4.3 → 2.9 s, so in-line rebuilds cost
  6.6 s instead of 8.0 s and cold start 3.6 s instead of 5.2 s. No change to prefill or decode.
- `/v1/npurun/prewarm` takes `force`; Hex primes the cache with its system prompt at start-up.
  First turn after a server start: 4.2 s → 0.85 s to first token (A/B, fresh server each).
- Gates: smoke 19/20, 0 stalls in 46 rebuild cycles, Hex core/agent/REPL/runner suites green.

Tried and closed: a spare second Genie dialog (to make every rebuild free) cannot be created on
this 16 GB machine; the second context binary fails with error 1007 once the first holds
5.8 GB. Lower HTP profiles only lose speed. A watchdog cannot wake a blocked non-polling wait.

### Optimisation radar, per solved task (fixed targets)

Rebuilt after an independent review of the first radar, which scored one runtime knob against
itself. Generated by `tools/backend_bench/radar.py` from the data on disk (`summary/radar.json`);
`--gate` fails when the 2.6 extended-suite pass-rate Wilson lower bound drops below 65 % or any
run is invalid. Unmeasured axes are listed, not scored.

| axis | target | v2.5.1 | 2.6 | llama.cpp | ceiling | scores (v2.5.1 / 2.6 / llama.cpp / ceiling) |
|---|---|---|---|---|---|---|
| task pass rate, extended suite | 0.83 fraction | 0.793 | 0.756 | — | 0.830 | 95.5 / 91.1 / — / 100 |
| wall per task, extended suite | 20 s | 7.16 | 8 | — | 12 | 100 / 100 / — / 100 |
| energy per solved task, extended suite | 200 J | 453.6 | 215.4 | 662.3 | 150 | 44.1 / 92.9 / 30.2 / 100 |
| first LLM call, whole latency | 4 s | 5.53 | 6.19 | — | 3 | 72.3 / 64.6 / — / 100 |
| first turn after server start | 1 s | 4.2 | 0.85 | 1.3 | 0.85 | 23.8 / 100 / 76.9 / 100 |
| follow-up turn, first token | 1 s | 0.989 | 1.151 | 3.565 | 0.7 | 100 / 86.9 / 28.1 / 100 |
| prompt tokens per task (input effectiveness) | 1500 tokens | 2409 | 2409 | — | 1300 | 62.3 / 62.3 / — / 100 |
| LLM calls per task (flow) | 2 calls | 1 | 1 | — | 2 | 100 / 100 / — / 100 |
| in-line rebuilds per turn (flow) | 0 rebuilds/turn | 0 | 0 | 0 | 0 | 100 / 100 / 100 / 100 |
| decode at Hex context | 15 tok/s | 10.391 | 11.771 | 11.911 | 13.5 | 69.3 / 78.5 / 79.4 / 90 |
| idle power, server loaded | 4.4 W | 14.351 | 4.4 | 4.916 | 4.4 | 30.7 / 100 / 89.5 / 100 |
| SoC temperature, sustained decode | 60 °C | 69.841 | 41.95 | 95.017 | 43 | 85.9 / 100 / 63.1 / 100 |
| foreground work slowdown (components) | 5 % | 0.7 | 2.6 | 54.1 | 0 | 100 / 100 / 9.2 / 100 |
| memory footprint | 4 GB | 5.8 | 5.8 | 2.85 | 5.8 | 69 / 69 / 100 / 69 |
| robustness: hangs per 100 requests | 0 hangs/100 | 0 | 0.133 | — | 0 | 100 / 97.3 / — / 100 |
| robustness: invalid eval runs | 0 runs | 0 | 0 | 3 | 0 | 100 / 100 / 70 / 100 |
| battery minutes per conversation | — min | — | — | — | — | — / — / — / — |
| fan / acoustic | — dB | — | — | — | — | — / — / — / — |

## Notes per axis

- **task pass rate, extended suite**: v2.5.1: 165/208 (5 runs/case, Wilson (0.733, 0.843)); 2.6: 62/82 (Wilson (0.653, 0.836)); paired polling-on control 61/82. Target = project baseline 97/117. llama.cpp: not run on the extended suite; smoke 14/14 valid with 6 client timeouts.
- **wall per task, extended suite**: median trace wall (Enter to final message, no harness overhead), paired runs same evening; v2.5.1 column uses the polling-on control. Ceiling: −40 % from a 1,200-token prompt (30 % decode tax) and fewer rebuilds.
- **energy per solved task, extended suite**: system joules over the case window / passes; harness overhead included. llama.cpp value is from its smoke suite (14 passes, 6 invalid). Ceiling: 2.6 minus prompt-length savings.
- **first LLM call, whole latency**: median first LLM call per run (prefill + generation of the first step) from eval traces, paired same-evening runs; the earlier 5-run baseline measured 3.41 s. Polling off adds wake-up latency to every call.
- **first turn after server start**: fresh server each rep, A/B with and without the start-up prime; llama.cpp = warm prefix.
- **follow-up turn, first token**: four-turn Hex conversation, turn 2 (+350 tokens); ceiling = measured pure extension cost.
- **prompt tokens per task (input effectiveness)**: median estimated input tokens per run; the 2,331-token system prompt is most of it. Ceiling: V2 plan's ≤ 800-token core + tools.
- **LLM calls per task (flow)**: median LLM calls per run; every extra step re-sends the transcript.
- **in-line rebuilds per turn (flow)**: six-turn real Hex scenario with 10 s think time: 0 in-line rebuilds on 2.6 (prewarm hid all); 2.5.1 has the same prewarm and is assumed equal (not re-measured). Score: 100 at 0, 50 at 1/turn.
- **decode at Hex context**: turn-2 decode rate at ~2.7K tokens. Ceiling: decode-vs-context curve at a 1,300-token prompt.
- **idle power, server loaded**: machine floor 3.4 W; target = floor + 1 W.
- **SoC temperature, sustained decode**: TZ2 at the end of a 240 s decode run.
- **foreground work slowdown (components)**: worst of three foreground jobs while the backend decodes. Score: 100 at ≤ 5 %.
- **memory footprint**: NPU shared memory for the 4-part W4A16 bundle; no known lever on this runtime, so the ceiling equals today.
- **robustness: hangs per 100 requests**: v2.5.1: 0 hangs in ~735 requests on record today; 2.6: 1 hang (unreproduced) in ~753 requests (eval LLM calls + bench + 310 stall-hunt requests). Score: 100 − 20 × hangs per 100.
- **robustness: invalid eval runs**: backend-unreachable/timeouts across the paired extended suites (82 runs each) and llama.cpp's smoke (20 runs).
- **battery minutes per conversation**: NOT MEASURED: every run was on AC. Drawn as a gap.
- **fan / acoustic**: NOT MEASURED. Drawn as a gap.

Gaps still open: the ~3.3K cache ceiling (compiled 4K window), the prompt-length cost on
decode, and the unexplained single hang.

## 13. On battery

Unplugged, discharge rate read from the battery itself (WMI `BatteryStatus`, 2 s samples),
Balanced plan, screen on. Machine idle with no server: 7.5 W (≈ 6.8 h on the 50.9 Wh pack).

| | polling on (2.5.1 behaviour) | polling off (2.6) |
|---|---|---|
| server loaded, idle | 10.5 W → 4.8 h | 7.4 W → 6.9 h |
| four-turn Hex conversation | 242 mWh (10.3–11.1 W, 68–98 s) | 190 mWh (11.1 W, 57–66 s) |

Polling off gives back two hours of battery for a laptop that keeps Hex open, and each
conversation costs a fifth less.

**The hang, resolved to a platform issue.** On battery, long-context requests (turn 3 or 4
of a conversation, 3.0–3.5K tokens) sometimes block for ~380 s until the client gives up;
the server log shows the HTP failing the graph (`Failed to execute graph. Error 1011`) and
the 60 s watchdog's abort has no effect. Counts, four-turn conversations on battery:

| configuration | hangs / requests |
|---|---|
| polling on, 3 threads (untouched 0.2.1 behaviour) | 1 / 31 |
| polling off (2.6 default) | 1 / 12 |
| polling off + `rpc_control_latency` 10 µs | 1 / 12 |
| polling off + `sustained_high_performance` | 1 / 7 |
| polling off + one CPU core kept busy | 1 / 19 |
| polling on, 1 host thread | 1 / 5 |
| polling off + `hmx_timeout_us` 5,000,000 (default 300,000) | 1 / ~55 (48 clean, then a 740 s stall) — lowers the rate, does not remove it |

On AC the same request shapes ran ~1,500 times with one hang. So the hang is a property of
the NPU on DC power, present in the shipped 0.2.1 configuration too; the polling flag, the
HTP profile, RPC latency and CPU idle states do not change it. Raising the accelerator's HMX
timeout from 0.3 s to 5 s ran 48 requests clean and then stalled for 740 s: a lower rate, not a fix. Modern Standby entries in the
System log do not coincide with the stalls. It is not a 2.6 regression, but it is the most
important open defect for battery users, and the right fixes are outside the config: npurun
should answer the client with an error the moment its watchdog fires (today the stream
stays open until the client's own timeout), and the driver-level cause needs a Qualcomm
report with the 1011 signature.

**Bounding the stall (fork 0.2.3).** The watchdog now ends the client's request when it fires:
the SSE stream closes with an `inference_error` event (and ends on its first terminal item
instead of waiting for a next item a wedged query never sends), the blocking endpoint answers
504. Measured on battery with the same conversation pattern: the stalled turn cost the client
97 s instead of 400 s. The inference permit stays held until Genie finally returns, so for the
next few minutes requests get an immediate 429 ("busy") rather than a second wedged query;
Hex surfaces that as a backend-busy error after its 25 s wait. The remaining gap is a server
self-restart when the query has not returned a minute after the watchdog, which needs a
supervisor on the Hex side.
