# Hex CLI

`shellai` is a local terminal agent for Windows on ARM, running on the Hexagon NPU.

It now works in three styles:

1. **Autopilot mode** - full local agent mode with automatic PowerShell execution for OS and file tasks.
2. **Chat mode** - answer in prose first and only suggest a command when it helps.
3. **Command-only mode** - force a single PowerShell command and then choose **Execute / Copy / Abort**.

It supports two local backends:

1. **Ollama** for the simplest setup today.
2. **OpenAI-compatible local endpoints** for Snapdragon NPU-backed runtimes (npurun uses Qualcomm's Genie SDK via Rust FFI; the Phi-4-mini fallback uses ONNX Runtime DirectML).

## Files

- `shellai.py` - main CLI (config, sessions, backends, tools, agent loop)
- `shellai_ui.py` - presentation layer (colors, spinner, banners, rendering) imported by `shellai.py`
- `shellai_telemetry.py` - silent structured session logger (see "Telemetry" below) imported by `shellai.py`
- `shellai_memory.py` - on-device semantic memory / vector store (see "Semantic memory" below) imported by `shellai.py`
- `shellai.cmd` - stable `shellai` alias (backward-compat)
- `Hex CLI.cmd` - primary Windows / Start Menu launcher, runs `launcher.py`
- `launcher.py` - detects best available backend (npurun NPU → Phi-4-mini DirectML → Ollama CPU) and starts `shellai.py` pointed at it
- `npurun/` - clone of [bpbonker/npurun](https://github.com/bpbonker/npurun), the Rust NPU runtime used for the primary Hexagon NPU path (not vendored — see setup below)
- `shellai.example.json` - config template
- `eval_harness.py` - 9-case smoke test (required gate before any `_AUTOPILOT_TEMPLATE` or tool-dispatch change)
- `eval_extended.py` - 35-case deep regression suite (trap, negative, error-recovery, ambiguous, runtime-correct)
- `eval_multiturn.py` - multi-turn adversarial suite: context scaling, routing flip, deep-context injection

## Quick start

From `C:\Users\Natha\local-shell-ai`:

```powershell
Copy-Item .\shellai.example.json .\shellai.json
python .\shellai.py --print-config
```

Then run autopilot mode:

```powershell
python .\shellai.py
```

Or ask a one-shot question:

```powershell
python .\shellai.py "what can you do?"
```

Or force command-only mode:

```powershell
python .\shellai.py --command-only "list the ten largest files here"
```

Other flags:

```powershell
python .\shellai.py --version       # print version and exit
python .\shellai.py --debug ...     # full tracebacks on error instead of a clean error box
python .\shellai.py --fast ...      # skip token-by-token streaming for quicker turnaround
python .\shellai.py --raw ...       # disable ANSI colour/styling, plain stdout only
```

Or use the launcher:

```powershell
.\shellai.cmd "show git status"
```

## Start Menu launcher

This install also supports an app-style launcher named **Hex CLI**.

- Start Menu name: **Hex CLI**
- Backing launcher: `C:\Users\Natha\local-shell-ai\Hex CLI.cmd`

You can open it from Start and use it like a local CLI agent with statuses, streaming text, shell execution, and file actions.

## PowerShell global alias

Create your PowerShell profile if it does not exist:

```powershell
if (!(Test-Path $PROFILE)) { New-Item -ItemType File -Path $PROFILE -Force | Out-Null }
notepad $PROFILE
```

Paste this in the profile:

```powershell
$ShellAiRoot = "$HOME\local-shell-ai"   # update if you move the folder

function shellai {
    python "$ShellAiRoot\shellai.py" @Args
}

function ?? {
    shellai @Args
}

Set-Alias sai shellai
```

Reload the profile:

```powershell
. $PROFILE
```

Examples:

```powershell
?? "show active network connections"
sai "find all .log files larger than 50 MB"
shellai "open the Downloads folder"
```

## Backend configuration

The tool auto-creates `shellai.json` on first run. The most important keys are:

```json
{
  "backend": "ollama",
  "model": "qwen2.5-coder:3b",
  "timeout_seconds": 240,
  "max_output_tokens": 96,
  "chat_max_output_tokens": 220,
  "autopilot_max_output_tokens": 220,
  "max_agent_steps": 8,
  "tool_output_limit": 12000,
  "stream_delay_ms": 8,
  "history_retention_days": 30,
  "telemetry_enabled": true,
  "ollama": {
    "host": "http://127.0.0.1:11434"
  },
  "openai_compatible": {
    "base_url": "http://127.0.0.1:8000/v1",
    "api_key": "local"
  }
}
```

`max_output_tokens` keeps command-only replies short, while the chat/autopilot token limits give the interactive modes enough room to behave like a real assistant. `history_retention_days` controls automatic cleanup of old chats.

On the CPU-backed Ollama path, the first response can still take a couple of minutes on Windows on ARM while the model loads and generates.

### Ollama setup

Ollama is the easiest local path, but on Windows ARM it is still the **CPU path**, not the Hexagon NPU path.

Example:

```powershell
& "$HOME\AppData\Local\Programs\Ollama\ollama.exe" pull qwen2.5-coder:1.5b
python .\shellai.py --backend ollama --model qwen2.5-coder:1.5b "show running processes"
```

Recommended Ollama models on Snapdragon X Elite 78:

| Use case | Model | Why |
| --- | --- | --- |
| Fastest coding-focused CPU path | `qwen2.5-coder:1.5b` | Very responsive, low RAM draw |
| Best balance for local CLI suggestions | `qwen2.5-coder:3b` | Better command quality while staying light |
| If you want a larger option | `deepseek-coder:6.7b` | Better reasoning, but more latency and battery cost |

## Snapdragon X Elite NPU path (current setup: npurun)

`launcher.py` automatically prefers this path. It runs **[npurun](https://github.com/bpbonker/npurun)**, an open-source Rust runtime built for Snapdragon X-series, which wraps Qualcomm's Genie SDK behind an Ollama-class CLI and an OpenAI-compatible HTTP server.

Model: **`qwen3-4b-instruct-2507`** (w4a16, ~2.5 GB). Chosen over the alternatives in npurun's registry (`phi-3.5-mini`, `qwen-2-5-7b`, `qwen-2-5-vl-7b-instruct`, `llama-v3-1-8b-instruct`) because it leads on agentic/tool-calling benchmarks (BFCLv3 65.9, MultiIF 66.3), matches or beats `Qwen2.5-7B-Instruct` despite half the parameter count, has a much larger context window (262K vs 32K tokens), and is the only model in the registry with confirmed-working performance on this exact hardware (~15 tok/s).

### One-time setup

1. Clone npurun and build it (Rust + LLVM + MSVC ARM64 build tools required):
   ```powershell
   git clone https://github.com/bpbonker/npurun
   cd npurun
   cargo install --path crates\npurun-cli
   ```
   On this machine, builds must run with MSVC's `link.exe` first on PATH (Git Bash's coreutils `link.exe` shadows it and breaks the build) — see `npurun/scripts/dev-shell-local.bat` for a working build shell.
2. Download the **QAIRT SDK** from the Qualcomm developer portal (requires a free Qualcomm account; not redistributable) and install it, e.g. at `C:\Qualcomm\AIStack\QAIRT_2.47.0`.
3. Set `QNN_SDK_ROOT` to that path, and **`ADSP_LIBRARY_PATH`** to `<QNN_SDK_ROOT>\lib\hexagon-v73\unsigned`. Without `ADSP_LIBRARY_PATH`, npurun crashes with `STATUS_STACK_BUFFER_OVERRUN` inside libGenie.
4. Pull the model: `npurun pull qwen3-4b-instruct-2507`

`launcher.py` checks for the npurun binary + QAIRT SDK and auto-pulls the model on first run if it's missing, then starts `npurun serve` on `127.0.0.1:11435` and points `shellai` at it via `shellai_npurun.json`.

### Fallback paths

If npurun/QAIRT isn't set up, `launcher.py` falls back to:

1. **Phi-4-mini via DirectML** (Adreno GPU) — requires ONNX Runtime GenAI + a conda environment; `launcher.py` detects and launches automatically if the model is present.
2. **Ollama on CPU** — simplest, but no NPU/GPU offload on Windows ARM today.

| Goal | Best choice |
| --- | --- |
| True NPU use, best agentic quality | npurun + `qwen3-4b-instruct-2507` (default) |
| No QAIRT SDK set up yet | Phi-4-mini via DirectML |
| Fastest zero-setup fallback | Ollama + `qwen2.5-coder:1.5b` or `qwen2.5-coder:3b` (CPU only) |

## Syntax verification and script execution

Autopilot mode has two complementary tools for code correctness:

**`verify_syntax(path, language)`** — non-destructive syntax check (never executes the file)
for Python (`ast.parse`), JSON (`json.loads`), PowerShell
(`[System.Management.Automation.Language.Parser]::ParseFile`), and JS/TS-family files
(`node --check`, skipped gracefully if `node` isn't on PATH). The autopilot system prompt
requires the model to call it immediately after any `edit_file`/`write_file` touching a code
file, and to retry the edit up to 3 times on failure — catching syntax mistakes before
reaching disk-confirmed "done".

**`run_code(path, args, timeout)`** — sandboxed script execution (.py .ps1 .js/.mjs/.cjs)
for runtime-bug diagnosis and fix verification. The workspace boundary is enforced via
resolved-path comparison (symlinks fully dereferenced on both sides); the extension allowlist
and list-form subprocess invocation (no `shell=True`) prevent interpreter injection. The
autopilot system prompt enforces an explicit 5-step self-correction loop for runtime bugs:
*(a) locate file → (b) run to see the error → (c) edit to fix it → (d) verify_syntax →
(e) run again to confirm exit code 0*.

## Telemetry

`shellai_telemetry.py` silently logs structured session data to
`.shellai/logs/session_<timestamp>_<id>.json` — one file per process run, written via an
atomic temp-file replace, completely separate from `shellai_ui.py`'s terminal rendering (no
telemetry code ever touches stdout/stderr). Disable it by setting `"telemetry_enabled": false`
in `shellai.json`. Each turn records: the prompt, execution path (`direct` vs `agentic`), every
tool call (with bulky args like file `content` redacted to a length placeholder), per-call and
total latency, tokens generated, and completion status (`completed` / `cancelled` / `error`).
`.shellai/` is gitignored.

## Semantic memory

`shellai_memory.py` gives autopilot mode persistent recall of past sessions without bloating
the prompt's context window. It's a pure-NumPy cosine-similarity vector index — no ChromaDB,
FAISS, or LangChain — over local sentence embeddings from
`sentence-transformers/all-MiniLM-L6-v2`'s ARM64-quantized ONNX export
(`onnx/model_qint8_arm64.onnx`, int8, ~23MB), run via `onnxruntime` on CPU (chosen because
neither the Ollama nor npurun backend currently exposes a working embeddings endpoint).
Embedding/tokenizer loading happens lazily on first actual use via a process-wide singleton, so
REPL startup latency is unaffected; a cold load takes ~0.2s, warm inference ~3ms.

Whenever an agentic turn finishes with at least one tool call, a short summary of the prompt
plus its tool sequence and touched file paths is embedded and appended to
`.shellai/vector_store/` (`vectors.npz` + `metadata.json`, both written via atomic temp-file
replace, capped at 500 entries with FIFO eviction). The `search_memory(query, top_k)` tool lets
the model query that store by cosine similarity (results below a 0.15 similarity floor are
dropped as noise). The autopilot system prompt requires the model to call `search_memory` first
whenever the user explicitly references a prior session ("earlier", "last time", "the file I
fixed before") — but this never overrides the existing rules for bare ambiguous requests (still
0 tool calls, just a clarifying question) or trap prompts naming an unneeded tool. Disable memory
entirely with `"memory_enabled": false` in `shellai.json`; like telemetry, `.shellai/` is
gitignored and every public method in the module swallows its own exceptions, so an offline or
failed model load degrades to a silent no-op rather than blocking a turn.

## Testing

The autopilot system prompt (`_AUTOPILOT_TEMPLATE` in `shellai.py`) is the actual production
tool-routing logic — it's validated against the live local endpoint, not just read for sanity.

### Fast CI/CD smoke test — `eval_harness.py`

```powershell
python .\eval_harness.py                # run all 9 cases, save + print report
python .\eval_harness.py --case casual-1 # run a single case by id
python .\eval_harness.py --no-save       # skip writing eval_results.json
```

This is the **required gate before merging any change to `_AUTOPILOT_TEMPLATE` or the tool
dispatch in `shellai.py`**. It hits the live OpenAI-compatible endpoint with the same JSON-action
system prompt and parsing `run_autopilot()` uses in production, and drives a real (sandboxed)
tool-execution loop for the agentic cases — file contents are verified on disk, not trusted from
the model's own claims. 9 cases across casual / factual / agentic. Must pass with:
- **0 tool hallucinations** — casual/factual questions get 0 tool calls.
- **Strict adherence to the literal-output constraint** — counts and facts in agentic results
  must match the literal tool output (e.g. an actual file count), never an estimate.
- **0 findings** in the printed report.

### Deep regression & edge-case suite — `eval_extended.py`

```powershell
python .\eval_extended.py                  # run the full 35-case matrix
python .\eval_extended.py --case trap-1     # run a single case by id
python .\eval_extended.py --no-save         # skip writing eval_extended_results.json
```

Builds on `eval_harness.py`'s fixtures and runner (imports it, doesn't duplicate it) and adds
categories for pressure-testing tool-routing beyond the fast smoke test:

| Category | What it checks |
| --- | --- |
| `regression` | Fresh phrasings of bugs already fixed once (knowledge vs. live-state, literal file counts, single-line-anchor edits) — guards against silent reintroduction. |
| `negative` | "Do nothing" conversational turns — strict assertion of EXACTLY 0 tool calls and 0 tool-name tokens anywhere in the raw output. |
| `trap` | Prompts that explicitly instruct the model to use a tool for something answerable directly (e.g. "use write_file to tell me a poem") — model should refuse the bait. |
| `error_recovery` | A fabricated tool-error turn (`[Permission Denied]`, `[File Not Found]`) is pre-seeded into the conversation; the model must pivot strategy, not crash or hallucinate success. |
| `ambiguous` | Underspecified requests ("fix my code") — model should ask for specifics rather than guess and act. |
| `self_correct` | A `.py` file is seeded with a deliberate syntax error; model must fix it and call `verify_syntax` to confirm before finishing. |
| `semantic_memory` | A past session is pre-seeded directly into `.shellai/vector_store/`; the model must call `search_memory` to recall the right file path/tool sequence rather than guessing or claiming no memory exists. |
| `runtime_correct` | A `.py` file with a deliberate runtime bug is seeded; model must use `run_code` to observe the error, `edit_file` to fix it, `verify_syntax` to confirm the edit, and `run_code` again to confirm exit code 0. |

Run this after any prompt change for deeper confidence than the fast smoke test gives alone; it's
slower and intentionally adversarial, not a merge gate.

**Known model limitation:** on `trap` cases that literally name a tool in the instruction (e.g.
"use run_command to calculate the factorial of 5"), `qwen3-4b-instruct-2507` still complies with
the bait roughly 1 in 3 runs despite an explicit rule against it — a ceiling of the 4B model's
instruction-following, not a harness or prompt bug. Other categories are stable across repeated
runs.

## Usage

Autopilot REPL:

```powershell
python .\shellai.py
```

Built-in controls:

```text
/help
/history
/new
/resume 2
/clear
/mode autopilot
/mode agent
/mode chat
/mode command
/exit
```

History is stored locally in `C:\Users\Natha\local-shell-ai\history.json`. Sessions get an automatic title from your first prompt and show **Summary**, **Modified**, and **Created** in the history list. Chats older than 30 days are deleted automatically based on their last modified time.

Press **Esc** while the CLI is thinking or while an autopilot command is still running to cancel the current step and return to the prompt.

One-shot agent question:

```powershell
python .\shellai.py "what can you do?"
```

Interactive command suggestion:

```powershell
?? "find all Python files modified today"
```

Copy only:

```powershell
?? --copy "compress everything in Downloads older than 30 days"
```

Execute immediately:

```powershell
?? --command-only --execute "show my IPv4 address"
```

## Notes

- The CLI uses only the Python standard library.
- The command always stops for **Execute / Copy / Abort** unless you pass `--copy` or `--execute`.
- If your NPU runtime already exposes an OpenAI-style endpoint, you only need to switch the config from `ollama` to `openai`.
