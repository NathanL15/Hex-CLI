# CPU vs NPU study — measurement runbook

Clean-machine protocol (after a reboot, laptop on AC, nothing else open).
Every command runs from the repo root. Foreground runs only: background
tasks get killed by the low-memory watchdog on this 16 GB machine.

## 0. Quiet the machine
- Stop the HWiNFO NPU feeder task and HWiNFO for the duration
  (`Stop-ScheduledTask 'HWiNFO NPU sensor'`, stop HWiNFO_ARM64), restart both at the end
  (`Start-ScheduledTask 'HWiNFO NPU sensor'`, `Start-ScheduledTask HWiNFO`).
- Confirm nothing else is running: no python/npurun/ollama/llama-server processes,
  no other Claude sessions running evals (message them: natha-1f, natha-3e).
- Data dir: `docs/backend_study/data` (empty). Previous attempts: `_pre_restart/`.

## 1. No-server idle baseline (60 s)
`typeperf -si 1 -sc 60 -y -o docs/backend_study/data/counters_none_baseline_<stamp>.csv <COUNTERS>`
(COUNTERS = Energy Meter(*) Power, Processor Information(_Total) utility/time/frequency,
Thermal Zone TZ0/TZ2/TZ98, Memory Available MBytes.)

## 2. NPU tier (npurun serve, Qwen3-4B-Instruct-2507 W4A16, Rewind runtime)
- Start fresh: `python -c "import launcher; launcher._start_npurun_server()"`, poll
  `/v1/chat/completions` until it stops returning 429; record cold start.
- `python tools/backend_bench/bench.py --backend npu --tag main --out docs/backend_study/data --phases idle,prefill,decode,hexturn,sustain,contention`
  (~20 min; foreground would exceed 10 min → run as two invocations:
  `idle,prefill,decode,hexturn` then `sustain,contention`).
- Restart the server fresh, then the Hex smoke suite:
  `python evals/cases_smoke.py --runs 2 --seed 20260905` with typeperf logging to
  `counters_npu_smoke_<stamp>.csv`; copy `evals/results/smoke_v2_results.json` to
  `docs/backend_study/data/smoke_npu_results.json`.
- Stop npurun. Reference: `npurun bench qwen3-4b-instruct-2507 --repeats 3 --csv docs/backend_study/data/npurun_bench.csv`
  (env from `launcher._npurun_env()`).

## 3. CPU tier A: Ollama 0.30.9 (what Hex's `backend: ollama` gets)
- `OLLAMA_CONTEXT_LENGTH=4096 OLLAMA_KEEP_ALIVE=-1 ollama serve` (log to data/ollama_server.log);
  load `qwen3:4b-instruct-2507-q4_K_M`; `ollama ps` must say 100% CPU, context 4096.
- bench.py `--backend cpu --tag main` in chunks: `idle,prefill` / `decode,hexturn` /
  `sustain,contention --sustain-seconds 120`.
- Kill ollama.exe AND its `llama-server.exe` runner afterwards.

## 4. CPU tier B: upstream llama.cpp b10819 (best CPU path)
- `C:\Users\Natha\Tools\llama-cpp\cpu\llama-server.exe -m <Q4_0 blob> --alias qwen3-4b-q4_0 -c 4096 -t 6 -tb 12 -np 1 --port 8080`
- bench.py `--backend llamacpp --tag main` in chunks: `idle,prefill` / `decode,hexturn` /
  `sustain` / `contention`.
- Hex smoke suite through this server (exact production code path, OpenAI transport):
  `python tools/backend_bench/run_suite.py smoke tools/backend_bench/shellai_cpu_bench.json --runs 2 --seed 20260905`
  with the config's base_url pointed at :8080/v1 and model `qwen3-4b-q4_0`;
  per-case chunks if it exceeds 10 min; copy results to `smoke_llamacpp_results.json`.
- `llama-bench -m <Q4_K_M> -m <Q4_0> -t 6,12 -p 512 -n 128 -r 2 -o md` → `llama_bench_cpu.md`.

## 5. Analysis
`python tools/backend_bench/analyze.py --data docs/backend_study/data --out docs/backend_study/summary`
then charts + paper (`docs/backend_study/CPU_VS_NPU.md`, artifact).

## Known pitfalls (all hit on 2026-09-05)
- Peer sessions launching evals on :11435 → coordinate first.
- multiprocessing children outlive a killed parent → load.py `spin` per-process, killed by pid.
- The NPU engine perf-counter instance goes stale after a Genie dialog rebuild → sampled
  with 15 s typeperf runs (`counters_npueng_*`).
- npurun Rewind answers a divergent prefix with an empty stream, then rebuilds (~5 s) on retry.
- Ollama's bundled llama-server prefills at ~27–40 tok/s vs 114–212 tok/s upstream (build flags).
- Ollama's `ollama serve` leaves its runner alive when killed.
