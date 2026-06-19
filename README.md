# Hex CLI

`hexcli` is a local terminal agent for Windows on ARM, running on the Snapdragon Hexagon NPU via **npurun**. It uses a ReAct-style JSON action loop with on-device semantic memory and a full tool suite — entirely in Python stdlib + NumPy/ONNX (no LangChain, no cloud required).

Current version: **1.7.0**

---

## Modes

| Mode | What it does |
|---|---|
| **Autopilot** | Full agentic loop — dispatches tools, executes PowerShell, reads/writes files, corrects its own errors, and loops until the task is done. |
| **Chat** | Prose answer first; only suggests a command when it genuinely helps. |
| **Command-only** | Returns a single PowerShell command, then prompts **Execute / Copy / Abort**. |

---

## Quick start

```powershell
Copy-Item .\shellai.example.json .\shellai.json
python .\shellai.py                        # autopilot REPL
python .\shellai.py "what can you do?"     # one-shot question
python .\shellai.py --command-only "list the ten largest files here"
```

Or via the Start Menu launcher: **Hex CLI**

PowerShell alias (add to `$PROFILE`):

```powershell
$ShellAiRoot = "C:\path\to\Hex-CLI"
function shellai { python "$ShellAiRoot\shellai.py" @Args }
function ??      { shellai @Args }
Set-Alias sai shellai
```

---

## Project layout

```
hexcli/              Python package — the agent runtime
  agent.py           config, sessions, tools, autopilot loop, mock backend
  ui.py              colours, spinner, REPL help text
  memory.py          semantic vector store, global/project split, dreaming daemon
  safety.py          command classifier + audit log
  escalate.py        cloud escalation (Anthropic API fallback) + secret redaction
  telemetry.py       silent structured session logger
  lockfile.py        single-instance guard

evals/               offline test suite + live eval harnesses
  test_v11.py        CoT stripping, undo, lint_code (requires no LLM)
  test_v12.py        safety classifier, audit log, error-loop detection
  test_v13.py        workspace snapshot, network, tool injection, batch
  test_v14.py        lockfile, /config, /memory, /profile, delegate
  test_v15.py        global/project memory, dreaming daemon, rules injection
  test_v16.py        secret redaction, escalation, checkpoints, config merge
  test_core.py       pure-function unit tests: deep_merge, session CRUD, parse_agent_action, etc.
  test_agent_loop.py integration tests using the mock backend (no NPU required)
  fixtures/          JSON fixture files for mock-backend tests
  harness.py         9-case live smoke test (requires running LLM endpoint)
  extended.py        35-case adversarial regression suite
  multiturn.py       multi-turn adversarial suite

.github/workflows/
  ci.yml             GitHub Actions CI — syntax, ruff, all offline test suites

shellai.py           entry-point shim
launcher.py          backend detection: npurun NPU → Phi-4-mini DirectML → Ollama CPU
shellai.example.json config template
```

---

## REPL commands

```text
/help                   show all commands
/history                list recent sessions
/resume <n>             resume session by list position
/new                    start a new session (keeps checkpoints)
/clear                  clear screen
/mode autopilot         switch to full agentic mode
/mode chat              switch to prose-first mode
/mode command           switch to command-only mode
/undo                   revert file edits from the last agent turn
/memory search <query>  query the semantic memory store
/memory show            show recent memory entries
/memory clear           clear project memory for the current directory
/memory prune           enforce the max-rules cap now
/profile                show current session's tool-call profile
/config get <key>       print a config value
/config set <key> <v>   update a config value at runtime
/save <name>            save a named checkpoint of the current session
/load <name>            restore a previously saved checkpoint
/checkpoints            list all checkpoints for the current directory
/delegate <query>       run a sub-query in a fresh session and return the result
/exit                   quit
```

---

## Tool suite

The autopilot loop has access to these tools:

| Tool | What it does |
|---|---|
| `run_command` | Execute a PowerShell command and capture its output |
| `read_file` | Read a file's content (blocks SSH/GPG key dirs and Windows credential stores) |
| `write_file` | Write or overwrite a file (captures undo snapshot) |
| `edit_file` | Replace an exact string in a file (captures undo snapshot) |
| `list_directory` | List files and subdirectories at a path |
| `search_memory` | Cosine-similarity search over the session memory store |
| `verify_syntax` | Non-destructive syntax check for .py, .json, .ps1, .js/.ts |
| `run_code` | Execute a .py/.ps1/.js script in a sandboxed subprocess |
| `lint_code` | Run ruff/pylint/eslint on a file and return findings |
| `http_get` | Fetch a URL with optional headers (no auth headers in untrusted turns) |
| `http_post` | POST JSON to a URL |
| `batch_exec` | Run a sequence of commands in a single turn |
| `delegate` | Run a sub-task in a fresh agent session |

---

## Features by version

### v1.1 — Quality of life
- **`/undo`** — reverts all file writes/edits from the previous agent turn. Snapshots are captured automatically before every `write_file`/`edit_file` call.
- **`lint_code` tool** — calls `ruff`/`pylint`/`eslint` and returns structured findings.
- **CoT stripping** — `<think>…</think>` blocks are stripped from model output before JSON parsing. Works transparently with reasoning models (Qwen3, etc.).

### v1.2 — Safety & reliability
- **Safety classifier** — every `run_command` output is classified as `safe`, `caution`, or `destructive` (first-match wins over ordered regex patterns). Destructive commands prompt for confirmation unless `autopilot_confirm_destructive` is false.
- **Audit log** — every `run_command` call is appended to `.shellai/audit.jsonl` with session ID, classification, command, and exit code.
- **Error-loop detection** — if the agent produces three identical `(tool, output)` pairs in a row, it stops automatically rather than looping indefinitely.

### v1.3 — Capability expansion
- **`verify_syntax` + `run_code`** — self-correction loop for code files: verify after edit, run to confirm zero exit code.
- **`search_memory` tool** — explicit in-prompt recall via cosine similarity.
- **`http_get` / `http_post`** — outbound network access from the agent loop.
- **`batch_exec`** — run a list of commands atomically in one turn.
- **`delegate`** — spawn a nested sub-agent for complex subtasks.
- **Workspace snapshot** — each autopilot turn prepends `[mode | model | cwd | turns]` context to the user message.

### v1.4 — Operational
- **Lockfile** — `hexcli/lockfile.py` prevents two REPL instances from fighting over the same session store. Stale locks (process dead) are auto-cleared.
- **`/config get|set`** — live config mutation without restarting the REPL. All settable keys are type-checked.
- **`/memory` subcommands** — `search`, `show`, `clear`, `prune`.
- **`/profile`** — prints the current session's per-tool call count.

### v1.5 — Memory intelligence
- **Global vs project memory** — turns with `key_paths` (file edits) go to a cwd-scoped store (`.shellai/vector_store/`, max 500 entries); general turns go to a cross-project global store (`~/.shellai/global_vector_store/`, max 1,000 entries). `search_memory` queries both and merges by cosine score.
- **Dreaming daemon** — a background thread (`start_dreaming`) wakes every 30 s; after 5 min of idle time it loads the 20 most-recent global entries, calls the LLM under `_NPU_INFERENCE_LOCK`, and appends distilled bullet rules to `~/.shellai/memory_rules.md` (capped at 50 via FIFO eviction).
- **Memory rules injection** — each autopilot turn's workspace snapshot appends the last 5 rules from `memory_rules.md` as a "Prior knowledge:" block (~60-token budget).

### v1.6 — Reliability & escape hatch
- **Cloud escalation** — when error-loop detection fires and `ANTHROPIC_API_KEY` (or `anthropic_api_key` in config) is set, the agent offers to escalate to Claude Haiku. All context is redacted before the outbound call (API keys, passwords, tokens, connection strings, SSH/AWS paths).
- **`/save` / `/load` checkpoints** — named workspace snapshots stored in `.shellai/checkpoints/<name>.json`. Independent of `/new`; survives session resets.
- **Per-project config** — `.shellai/config.json` in the working directory deep-merges over the global `shellai.json`. Useful for per-repo model or token-limit overrides.

### v1.7 — Quality gate
- **Mock backend** — `backend: "mock"` + `set_mock_responses([...])` lets any test drive the full autopilot loop offline with no LLM endpoint.
- **CI workflow** — `.github/workflows/ci.yml` runs syntax compile, ruff, and all offline test suites on every push.
- **Core coverage backfill** — `evals/test_core.py` covers all v1.0-era pure functions (deep_merge, session CRUD, history retention, parse_json_object/parse_agent_action edge cases, `_check_sensitive_path`, safety classifier, memory rule helpers).
- **Agent loop tests** — `evals/test_agent_loop.py` tests the full `run_autopilot` loop end-to-end: tool dispatch, undo snapshots, error-loop detection, safety gating, step budget, history injection.

---

## Configuration reference

All keys live in `shellai.json` (global) or `.shellai/config.json` (per-project override). The per-project file deep-merges over the global one.

| Key | Default | Description |
|---|---|---|
| `backend` | `"ollama"` | LLM backend: `"ollama"`, `"openai"`, `"mock"` |
| `model` | `"qwen2.5-coder:3b"` | Model name for the active backend |
| `temperature` | `0.1` | Sampling temperature |
| `timeout_seconds` | `240` | Request timeout for non-streaming calls |
| `max_output_tokens` | `96` | Token limit for command-only mode |
| `chat_max_output_tokens` | `220` | Token limit for chat mode |
| `autopilot_max_output_tokens` | `220` | Token limit per autopilot step |
| `compact_max_output_tokens` | `512` | Token limit for session compact summaries |
| `max_agent_steps` | `8` | Maximum tool-call steps per autopilot turn |
| `tool_output_limit` | `12000` | Max bytes returned from any tool call |
| `stream_delay_ms` | `8` | Delay between streamed tokens (ms) |
| `use_streaming` | `true` | Enable token-by-token streaming output |
| `history_retention_days` | `30` | Delete sessions older than this |
| `memory_enabled` | `true` | Enable/disable the semantic memory store |
| `telemetry_enabled` | `true` | Enable/disable session telemetry logs |
| `autopilot_confirm_destructive` | `true` | Prompt before running destructive commands |
| `shell_hint` | `""` | Override the shell executable path |
| `ollama.host` | `"http://127.0.0.1:11434"` | Ollama server base URL |
| `openai_compatible.base_url` | `"http://127.0.0.1:8000/v1"` | OpenAI-compatible endpoint |
| `openai_compatible.api_key` | `"local"` | API key for the OpenAI-compatible endpoint |
| `anthropic_api_key` | `""` | Anthropic API key for cloud escalation fallback |
| `escalation_model` | `"claude-haiku-4-5-20251001"` | Claude model used when escalating |

---

## Safety model

The safety classifier in `hexcli/safety.py` assigns every `run_command` call one of three risk levels:

| Level | Meaning | Default behaviour |
|---|---|---|
| `safe` | Read-only: `Get-*`, `ls`, `dir`, `git status/log/diff`, `echo`, `pwd`, `type`, `cat`, `pip list`, etc. | Runs without confirmation |
| `caution` | All other commands | Runs without confirmation in autopilot (config: `autopilot_confirm_destructive: false`) |
| `destructive` | `Remove-Item`, `rm`, `rd`, `del`, `git reset --hard`, `git push -f`, `git clean -f`, `Format-Volume`, `diskpart`, `-Force -Recurse` together, etc. | Prompts for confirmation when `autopilot_confirm_destructive: true` |

Every command is appended to `.shellai/audit.jsonl` regardless of risk level, for post-hoc review.

---

## Memory architecture

```
hexcli/memory.py
│
├── Project store  (.shellai/vector_store/)       — cwd-scoped, max 500 entries
│     Indexed when: a turn touches files (key_paths non-empty)
│
├── Global store   (~/.shellai/global_vector_store/)  — cross-project, max 1,000 entries
│     Indexed when: general turns (no file edits)
│
└── Memory rules   (~/.shellai/memory_rules.md)   — capped at 50 bullets
      Written by: dreaming daemon (background, after 5 min idle)
      Read by:    workspace_snapshot() → injected into every autopilot turn
```

**Embedding model:** `sentence-transformers/all-MiniLM-L6-v2` ARM64-quantized ONNX (`onnx/model_qint8_arm64.onnx`, ~23 MB, int8). Runs via `onnxruntime` on CPU. Cold load ~0.2 s; warm inference ~3 ms.

**Search:** query both stores, merge candidates by cosine score, deduplicate by content hash, return top-k (floor: 0.15 similarity).

**`_NPU_INFERENCE_LOCK`:** a `threading.Lock()` serialising all LLM calls. `call_llm()` holds it for the full streaming duration. The dreaming daemon acquires with `timeout=5` and skips the consolidation step if the main thread is mid-inference.

---

## Testing

### Offline suites (no LLM required)

```powershell
python .\evals\test_v11.py        # CoT stripping, undo, lint_code
python .\evals\test_v12.py        # safety classifier, error-loop detection
python .\evals\test_v13.py        # workspace snapshot, network, tool injection
python .\evals\test_v14.py        # lockfile, /config, /memory, /profile
python .\evals\test_v15.py        # global/project memory, dreaming, rules injection
python .\evals\test_v16.py        # redaction, escalation, checkpoints, config merge
python .\evals\test_core.py       # pure-function unit tests
python .\evals\test_agent_loop.py # full autopilot loop with mock backend
```

All suites combined: **200+ tests**, all offline.

### Live eval harnesses (require running LLM endpoint)

```powershell
python .\evals\harness.py         # 9-case smoke test — required gate before prompt changes
python .\evals\extended.py        # 35-case adversarial regression suite
python .\evals\multiturn.py       # multi-turn adversarial suite
```

### CI

Every push runs the full offline suite via `.github/workflows/ci.yml`:
- `python -m compileall hexcli/ evals/ -q` — syntax compile gate
- `ruff check hexcli/` — lint
- All offline test suites (`test_v11` through `test_core` and `test_agent_loop`)

---

## Backend setup

### npurun (Snapdragon Hexagon NPU — recommended)

1. Clone and build npurun:
   ```powershell
   git clone https://github.com/bpbonker/npurun
   cd npurun
   cargo install --path crates\npurun-cli
   ```
   Build must run with MSVC's `link.exe` first on PATH — see `npurun/scripts/dev-shell-local.bat`.

2. Install the [QAIRT SDK](https://developer.qualcomm.com/software/ai-stack) (free Qualcomm account required) and set:
   ```powershell
   $env:QNN_SDK_ROOT = "C:\Qualcomm\AIStack\QAIRT_2.47.0"
   $env:ADSP_LIBRARY_PATH = "$env:QNN_SDK_ROOT\lib\hexagon-v73\unsigned"
   ```
   Without `ADSP_LIBRARY_PATH`, npurun crashes with `STATUS_STACK_BUFFER_OVERRUN` inside libGenie.

3. Pull the model:
   ```powershell
   npurun pull qwen3-4b-instruct-2507
   ```

`launcher.py` auto-detects npurun + QAIRT, starts `npurun serve` on `127.0.0.1:11435`, and pulls the model on first run.

**Model:** `qwen3-4b-instruct-2507` (w4a16, ~2.5 GB). Chosen for its agentic benchmark results (BFCLv3 65.9, MultiIF 66.3), 262K context window, and ~15 tok/s throughput on Snapdragon X Elite.

### Fallback paths

| Priority | Backend | Notes |
|---|---|---|
| 1 | npurun + `qwen3-4b-instruct-2507` | Hexagon NPU, best quality |
| 2 | Phi-4-mini via ONNX Runtime DirectML | Adreno GPU; requires conda + ONNX RT GenAI |
| 3 | Ollama on CPU | Zero setup; CPU only on Windows ARM |

Recommended Ollama models:

| Model | Use case |
|---|---|
| `qwen2.5-coder:1.5b` | Fastest, lowest RAM |
| `qwen2.5-coder:3b` | Best CPU balance |
| `deepseek-coder:6.7b` | Better reasoning, higher latency |

---

## Notes

- **stdlib only** — no third-party packages beyond `numpy` and `onnxruntime` (both used only in `memory.py`).
- Every public method in `memory.py` swallows its own exceptions — a failed embedding is a silent no-op, never a REPL crash.
- `.shellai/` is gitignored (telemetry, memory, checkpoints, audit log, project config).
- Secret redaction runs before any outbound API call (escalation). Patterns: `sk-*`, `Bearer *`, `password=*`, `api_key=*`, `token=*`, connection strings, `~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.gpg` paths.
