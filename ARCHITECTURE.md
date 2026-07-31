# Architecture Overview — Hex CLI

A local-first, agentic CLI for Windows on ARM. The design goal throughout was to push as
much capability as possible onto a small, NPU-resident model and a thin Python orchestration
layer, without reaching for a vector database, a heavyweight agent framework, or a cloud
fallback. This document covers the objective, the routing logic that keeps the agent from
mis-using tools, the testing infrastructure that catches prompt regressions before they ship,
the on-device semantic memory layer, and the real system limitations discovered along the way.

## 1. The Objective

The target hardware is a Snapdragon X Elite laptop — an ARM64 SoC with a Hexagon NPU. The
constraint that shaped every other decision in this project: keep the agent loop, tool
dispatch, and supporting infrastructure (telemetry, memory, eval harnesses) in the Python
standard library, and keep the model itself small enough to run entirely on-device at
interactive latency.

The production inference path is:

```
hexcli/agent.py  ──HTTP (OpenAI-compatible)──>  npurun serve (Rust)  ──FFI──>  Qualcomm Genie SDK  ──>  Hexagon NPU
```

**`npurun`** ([bpbonker/npurun](https://github.com/bpbonker/npurun)) is an open-source Rust
runtime that wraps Qualcomm's closed-source Genie SDK behind an Ollama-class CLI and an
OpenAI-compatible HTTP server. It is not built on ONNX Runtime or the QNN Execution
Provider — those would be the obvious first guess for "NPU inference on Windows ARM," but
the actual dependency graph here is Rust FFI directly into Genie. That distinction mattered
later (see §5).

**Model:** `qwen3-4b-instruct-2507` (w4a16 quantization, ~2.5 GB). It was chosen over the
other models in npurun's registry (`phi-3.5-mini`, `qwen-2-5-7b`, `qwen-2-5-vl-7b-instruct`,
`llama-v3-1-8b-instruct`) on four criteria: agentic/tool-calling benchmark scores (BFCLv3
65.9, MultiIF 66.3), parity with `Qwen2.5-7B-Instruct` at roughly half the parameter count,
a much larger context window (262K vs 32K tokens), and confirmed working throughput on this
exact hardware (~15 tok/s). A 4B model is small enough to keep latency in the single-digit
seconds for most turns, which matters for a CLI tool people expect to interrupt and reuse
constantly — but a 4B model is also the source of most of the interesting behavioral
problems documented in this overview.

`hexcli/agent.py` itself talks to the backend over plain HTTP using only `urllib`/`http.client`
from the standard library — no `requests`, no `httpx`. Two backends are supported
(Ollama for the simplest local setup, and any OpenAI-compatible endpoint for npurun or a
DirectML/Phi-4-mini fallback), selected by a one-line config change.

## 2. The Routing Logic — Avoiding Over-Tooling

The hardest behavioral problem with a small instruction-following model in an agentic loop
isn't getting it to use tools — it's getting it to **stop** using tools for things it already
knows. A 4B model will happily call `run_command` to answer "what's the syntax for a Python
list comprehension," burning 5-10 seconds of NPU inference on a question that needed zero
tool calls.

The fix is entirely in the system prompt (`_AUTOPILOT_TEMPLATE` in `hexcli/agent.py`), not in code.
The model emits exactly one JSON action per turn (`{"action": "<tool>", "args": {...}}` or
`{"action": "finish", "message": "..."}`), and the routing rules draw a hard line between two
categories of request:

- **Static knowledge** (rule 4): "general knowledge questions — programming syntax,
  cmdlet/command names, algorithms, concepts, explanations — do not require this machine's
  state. Answer them directly with finish and 0 tool calls."
- **Live state** (rules 3, 9): anything that depends on *this machine's actual current
  state* — running processes, installed software, file contents, git status — must go
  through a tool. The model is explicitly told never to claim it lacks access and guess
  instead.

A second failure mode is **bait compliance**: a user prompt that explicitly names a tool for
something answerable directly ("use `write_file` to tell me a poem about autumn"). Rule 10
calls this out by name and gives a worked example, instructing the model to treat the named
tool as "irrelevant noise" when the underlying request doesn't need it.

A third failure mode is **acting on underspecified requests**. Rule 12 requires the model to
recognize when a request doesn't name a specific file or target and the working directory
doesn't make one obvious — in that case it must ask a clarifying question with zero tool
calls, and is explicitly forbidden from opening the response with "Done" or claiming anything
was completed.

These three rules are tested directly by the `trap`, `negative`, and `ambiguous` categories
in `evals/extended.py` (see §3) — and the trap category in particular exposed a real ceiling
in the model's instruction-following that no amount of prompt tuning fully closed (§5).

## 3. Testing Infrastructure

Two tiers, and the split matters: **deterministic logic is tested offline against a mock
backend; model behaviour is measured live and statistically.** Conflating the two was the
original sin of the v1 instrument — it graded string matches from single live runs, so
stochastic 4B variance and real regressions were indistinguishable.

### Tier 1 — offline suites (the merge gate)

19 suites, 591 tests, no LLM and no NPU required. This is what CI (windows-latest) runs,
alongside the compile gate and `ruff check hexcli/ evals/`:

```powershell
python evals/test_core.py           # core coverage
python evals/test_agent_loop.py     # the loop end to end, mock backend
python evals/test_write_scope.py    # writes confined to the workspace
python evals/test_injection_defense.py
python evals/test_lineedit.py       # input line, injected key source
```

Anything that can be pinned deterministically lives here — parsing, safety classification,
write-scoping, compaction, diffing, the input line. Injecting the seams (mock backend,
scripted key source) is what makes that possible.

### Tier 2 — live evals (`evals/runner.py`, `evals/cases_*.py`)

```powershell
python evals/cases_smoke.py                    # fast gate
python evals/cases_extended.py --runs 5        # pass^5 over 36 cases
python evals/cases_multiturn.py --runs 3       # deep-context scenarios
python evals/compare.py <before.json> <after.json>
```

These drive the **production** `run_autopilot` (via `AutopilotProbe`, not a reimplementation)
against the real NPU endpoint, and grade **filesystem state and answer content**, never string
matches. Each case runs N times and reports pass@k and pass^k with Wilson intervals, because
a single run of a 4B model means very little.

Two hard-won rules encoded in the runner:

- **Backend failures are not model failures.** `is_backend_failure` marks a run invalid rather
  than failed when the NPU server is unreachable or has degraded. The Genie dialog starts
  returning errors for everything after 1–2 hours of traffic, which looks exactly like a
  catastrophic regression; this trap cost a full day before it was identified.
- **Restart the server between suites**, so results stay comparable.

`evals/harness.py`, `extended.py` and `multiturn.py` are the **superseded v1 instrument**,
retained for reference only. The categories below describe that older suite, and are kept
because they document the failure modes the routing rules in §2 target:

| Category | What it pressure-tests |
| --- | --- |
| `regression` | Fresh phrasings of bugs already fixed once — guards against silent reintroduction. |
| `negative` | "Do nothing" turns — strict assertion of EXACTLY 0 tool calls and 0 tool-name tokens anywhere in output. |
| `trap` | Prompts that explicitly instruct the model to use a tool for something answerable directly. |
| `error_recovery` | A fabricated tool-error (`[Permission Denied]`, `[File Not Found]`) is pre-seeded; the model must pivot strategy, not crash or hallucinate success. |
| `ambiguous` | Underspecified requests — model should ask for specifics rather than guess and act. |
| `self_correct` | A `.py` file seeded with a deliberate syntax error; model must fix it and call `verify_syntax` to confirm before finishing. |
| `semantic_memory` | A past session is pre-seeded into the vector store; model must call `search_memory` to recall it rather than guessing or claiming no memory exists. |

These categories carried over into `evals/cases_extended.py`, which grades them on state
rather than strings and repeats each N times. The triage habit they taught remains correct:
a handful of findings is run-to-run variance until a rerun of the specific case says
otherwise (see §5 for two worked examples).

## 4. On-Device Semantic Memory

The goal: let the agent recall what it did in past sessions ("the file I fixed before," "what
error did I get") without bloating every prompt with raw conversation history, and without
adding ChromaDB, FAISS, or LangChain to a project that otherwise depends on nothing but the
standard library.

`hexcli/memory.py` implements a **pure-NumPy cosine-similarity vector index** — no external
vector database, just a `.npz` array and a `.json` metadata sidecar:

```
.shellai/vector_store/
├── vectors.npz      # (N, 384) float32 embeddings, atomic temp-file replace
└── metadata.json     # per-entry text, tool_sequence, key_paths, outcome, timestamp
```

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2`, exported to an ARM64-quantized
ONNX graph (`onnx/model_qint8_arm64.onnx`, int8, ~23 MB) and run via `onnxruntime` on CPU.
This wasn't the first choice — the natural move would be to call the backend's own embeddings
endpoint — but neither the Ollama nor the npurun backend currently exposes a working
embeddings API, so a small standalone ONNX model running on CPU was the pragmatic substitute.
A process-wide lazy singleton (`_Embedder`) loads the session and tokenizer once on first use
(~0.2s cold load) and reuses them for the life of the process (~3ms per embed call
afterward), so REPL startup latency is unaffected by memory being enabled.

**Indexing:** whenever an agentic turn finishes having made at least one tool call,
`maybe_index_turn()` embeds a short summary of the prompt plus its tool sequence and touched
file paths, and appends it to the store (capped at 500 entries, FIFO eviction on overflow).

**Retrieval:** the model has a `search_memory(query, top_k)` tool. Cosine similarity scores
below a `0.15` floor are dropped as noise — an earlier `0.35` threshold turned out to be too
strict in practice and silently starved the model of relevant matches; this was caught and
fixed during the `semantic_memory` eval category's first failing run, not by code review.

**Trigger rule:** the autopilot system prompt requires the model to call `search_memory`
*first* whenever the user explicitly references a prior session ("earlier," "last time," "the
file I fixed before") — but this is deliberately scoped narrowly so it never overrides the
existing ambiguous-request rule (§2, rule 12) or causes the model to call it just because an
unrelated question happens to sound open-ended (rule 10's bait-compliance protection).

**Failure mode by design:** every public method in `hexcli/memory.py` swallows its own
exceptions. An offline first run, a missing model cache, or a corrupted store degrades to a
silent no-op rather than blocking or crashing a turn that doesn't strictly need memory —
matching the same one-way-dependency, fail-silent convention already established by
`hexcli/telemetry.py` and `hexcli/ui.py`.

## 5. System Limitations & Telemetry

### TTFT: the real bottleneck vs. the obvious one

The natural hypothesis going into a latency investigation was that `hexcli/agent.py` was paying
for a fresh TCP connection on every single agent-loop step (`urllib.request.urlopen()` opens
and tears down a new socket per call). Measured on loopback, that overhead is real but
small — about **8.5ms per connect** — and reusing a keep-alive connection across the
non-streaming HTTP call sites recovers exactly that, no more.

The dominant TTFT cost turned out to be on the **other side of the HTTP boundary**, inside
`npurun` itself: every `/v1/chat/completions` request — streaming or not — calls
`engine.reset_dialog()` before generating, forcing a full prefill of the entire growing
conversation transcript from scratch on every step. There is no KV-cache reuse across
requests today. This is a deliberate, documented tradeoff in npurun's own source: a "rewind"
fast path exists that could reuse the cache when the new transcript is a strict
prefix-extension of what's already cached, but it's disabled because small client-side
roundtripping differences (whitespace, end-of-turn marker re-encoding) were observed to
corrupt the dialog handle (`ERROR_QUERY_FAILED`). The tradeoff is reliability over speed,
and it costs roughly 200ms-1s of avoidable full-reprefill latency per turn — an order of
magnitude larger than the connection-reuse savings, and it lives in Rust code outside this
project's surface area.

One specific lesson from implementing the connection-reuse fix: a shared keep-alive
connection is *not* safe to apply uniformly across every HTTP call site in this codebase.
The two streaming functions (`_ollama_stream_chat`, `_openai_stream_chat`) hand the response
body off to a background reader thread and can abandon it mid-stream — on a `[DONE]` marker
arriving before the socket reaches EOF, or on a user cancel — which can leave a shared
connection in an indeterminate state for the next reuse. That combination produced a real
hang during testing. The fix was deliberately narrow: connection pooling stays on the
synchronous, always-fully-drained JSON request/response paths; the two streaming paths keep
dedicated per-call connections, trading ~8.5ms of avoidable overhead for not reintroducing a
shared-socket race.

### The trap-prompt ceiling

`evals/extended.py`'s `trap` category exists specifically to test rule 10 (§2) — prompts that
name a tool for something answerable directly. Despite an explicit, worked-example rule
against this, `qwen3-4b-instruct-2507` still complies with the bait roughly **1 in 3 runs**
on prompts that literally name a tool in the instruction (e.g. "use `run_command` to
calculate the factorial of 5"). Other adversarial categories (`negative`, `ambiguous`,
`error_recovery`) are stable across repeated runs; this one is not, and no amount of
additional prompt wording closed the gap further. It's documented as a known ceiling of the
4B model's instruction-following rather than a harness or prompt bug — a model-capacity
limit visible only because the eval harness runs against the live model instead of mocking
its behavior.

### Structured-output reliability

`response_format: {"type": "json_object"}` is supported by npurun's OpenAI-compatible
server, but only as a **prompt-injection hint** (`augment_for_json_mode()` on the Rust
side) — not grammar-constrained or token-masked sampling. The model can still produce
invalid JSON even with this flag set. The practical implication: the agent's JSON-action
protocol cannot rely on the backend to guarantee well-formed output, so `hexcli/agent.py`'s own
parse-retry loop (up to 2 retries on malformed agent output, rule-driven anchored edits to
avoid multi-line JSON-escaping mistakes) remains the actual correctness backstop, not the
backend flag.

### Hexagon NPU recovery — FastRPC skeleton hang and BSOD

The nominal inference path is `shellai.py → npurun serve → GenieDialog_create() → CDSP`. Every
`npurun serve` invocation creates a Qualcomm FastRPC skeleton process — `QcSkExt8380` — that
bridges Windows user-mode to the Compute DSP (CDSP) via a shared DMA buffer. Under clean
shutdown this process exits with npurun. Under force-kill (Task Manager, `pkill`, or an eval
harness terminating the process) it gets re-parented to `smss.exe` and stays alive, holding the
CDSP session open.

**Hang symptom:** the next `npurun serve` invocation prints the model-manifest line, then blocks
indefinitely inside `GenieDialog_create()` (Genie SDK FFI call, Genie config has
`"allow-async-init": false`, so this is synchronous). No timeout. No error. The process just
stops progressing.

**What not to do:** stopping the `qcdpps` DSP power-proxy service while the orphaned
`QcSkExt8380` still holds a live DMA mapping triggers `DRIVER_VERIFIER_DMA_VIOLATION`
(BSOD 0xE6, subtype `DMA_ILLEGAL_MEMORY_TYPE`). This produces the characteristic Windows
driver-verifier crash screen with bugcheck code 0xE6.

**Two failure modes, two recovery paths:**

| Symptom after the bad event | Root cause | Recovery |
|---|---|---|
| `npurun serve` fails immediately with `"Failed to create device: 14001"` / `ERROR_GENERAL` | BSOD reset the CDSP but put `ACPI\QCOM0D0A\2&DABA3FF&2` (Hexagon NPU) into `CM_PROB_DISABLED` (Code 22) | Run in elevated PowerShell: `Enable-PnpDevice -InstanceId 'ACPI\QCOM0D0A\2&DABA3FF&2' -Confirm:$false` then `Restart-Service qcdpps` |
| `npurun serve` prints the manifest then hangs forever | Orphaned `QcSkExt8380` skeleton process is holding the CDSP session | **Cold boot only** — hold **Shift** while clicking Shut Down (not Restart), then power on. A normal restart leaves NPU firmware state intact due to Windows Fast Startup (hiberboot), and the orphan persists. |

The device instance ID `ACPI\QCOM0D0A\2&DABA3FF&2` is specific to this laptop; confirm with
`Get-PnpDevice | Where-Object { $_.FriendlyName -like "*Hexagon*" -or $_.FriendlyName -like "*NPU*" }`.

### Execution sandbox (`run_code`) — design and prompt engineering

A useful agentic loop for runtime debugging requires: observe the error, edit the cause, confirm
the fix runs clean. `run_code` closes the last mile — without it, the model has to trust its own
code reading to decide whether a fix worked, which introduces false positives.

**Security model:**

- `resolve_path()` calls `Path.resolve()`, dereferencing all symlinks before the workspace
  boundary check. `cwd` is similarly resolved with `Path.cwd().resolve()` so both sides of
  the `is_relative_to()` comparison are in the same canonical space — a CWD that is itself a
  symlink cannot produce a false "in-bounds" result.
- Subprocess invocation uses the list form (no `shell=True`), so the model-controlled `path`
  argument is passed as a literal argv element, not interpolated into a shell command string.
- Extension allowlist (`.py .ps1 .js .mjs .cjs`) is enforced before `Popen` is reached.
- Hard timeout with `proc.kill()` ensures a runaway script cannot block the agent loop.
- `encoding="utf-8", errors="replace"` handles scripts that emit non-UTF-8 bytes without
  raising a decode exception.
- OS-level `Popen` failures (`PermissionError`, `FileNotFoundError`, `OSError`) are caught
  and re-raised as `RuntimeError` so the agent receives a clean, human-readable error string.

**Prompt engineering — why the 5-step loop is explicit:**

A 4B model can reliably call a single tool when asked. Chaining five specific tools in a
non-obvious order across multiple turns is harder — the model tends to skip steps (reading the
code and fixing without running it first), call tools in the wrong order (`verify_syntax` before
`edit_file`), or substitute a different tool (`run_command` instead of `run_code`) when a rule
is ambiguous about scope.

Rule 15 addresses this by spelling out the exact ordered sequence as lettered sub-steps:
*(a) locate file → (b) run_code to see the error → (c) edit_file to fix it →
(d) verify_syntax to confirm the edit → (e) run_code to confirm exit code 0.*
This eliminates the multi-hop inference burden — at each step the model follows the next lettered
instruction rather than reconstructing the full sequence from first principles.

Three bug designs were tried before finding one the 4B model could reliably execute end-to-end:

| Bug | Failure mode | Why it failed |
|---|---|---|
| `KeyError` — missing dict key | No-op edit (`old_string` == `new_string`) | 4B model identifies the right line but cannot infer what the missing value should be |
| `NameError` typo in `greet.py` | Model read the file and spotted the fix visually, skipping `run_code` | Bug was top-level and trivially visible; model also hallucinated a `src/` path prefix |
| `NameError` typo (`conut`) inside a function body in `report.py` | **Passes consistently** | Python's own `"Did you mean: 'count'?"` gives the model exact replacement text; indented placement is less trivially visible; non-generic filename avoids `src/` path hallucination |

### Telemetry

`hexcli/telemetry.py` silently logs structured session data to
`.shellai/logs/session_<timestamp>_<id>.json` (atomic temp-file replace, one file per process
run) — completely separate from the UI's terminal rendering. Each turn records the prompt,
execution path (`direct` vs `agentic`), every tool call (bulky args like file `content`
redacted), per-call and total latency, tokens generated, and completion status. This is what
made the TTFT investigation in this section possible without instrumenting the live model
session by hand — and it's the same mechanism used to confirm, after the connection-pooling
change, that the full 35-case extended suite's only correctness findings were pre-existing
flaky cases (reproduced with identical failure signatures in pre-change baseline runs), not
new regressions.
