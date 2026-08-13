# Hex CLI

A terminal agent that runs **entirely on your machine** — Qwen3-4B on the
Snapdragon Hexagon NPU via [npurun](https://github.com/bpbonker/npurun), no
cloud, no API key, nothing leaving the box. Python stdlib + NumPy/ONNX; no
LangChain.

Current version: **2.0.0**

```
you ▸ the median calc in processor.py is wrong for even-length lists — fix it
  → read_file
  → edit_file
~ processor.py  (+3 −1)
@@ -12,4 +12,6 @@
-    return sorted(data)[len(data) // 2]
+    mid = len(data) // 2
+    if len(data) % 2 == 0:
+        return (sorted(data)[mid - 1] + sorted(data)[mid]) / 2
  → run_code
Fixed: even-length lists now average the two middle values. Verified — the
test file prints 3.5 for [1, 2, 5, 6].
```

---

## Modes

| Mode | What it does |
|---|---|
| **Autopilot** (default) | Full agentic loop — runs tools, edits files, verifies its own work, loops until done. |
| **Chat** | Prose answer; suggests a command only when it genuinely helps. |
| **Command-only** | Returns one PowerShell command, then prompts Execute / Copy / Abort. |

---

## Quick start

```powershell
git clone https://github.com/NathanL15/Hex-CLI
cd Hex-CLI
.\install.ps1        # checks the machine, fetches npurun + the model, runs --doctor
```

The installer walks every setup step and skips whatever is already done, so
re-run it after fixing anything it flags. The one step it cannot do for you
is the QAIRT SDK download (Qualcomm does not allow redistribution) — it
prints exact instructions and picks the SDK up on the next run.

Once installed:

```powershell
python -m hexcli.agent --doctor      # re-check the install any time
python -m hexcli.agent               # autopilot REPL
python -m hexcli.agent "what changed in this repo today?"
python -m hexcli.agent --command-only "list the ten largest files here"
git diff | python -m hexcli.agent "review this diff"      # piped input becomes context
echo "summarize README.md" | python -m hexcli.agent       # ...or the task itself
```

Piped stdin is capped with head+tail sampling (the window is 4K tokens) and
is labelled as data, not instructions.

**Config is optional.** The built-in defaults are complete; a `shellai.json`
only needs the keys you want to override. `shellai.example.json` shows every
available key with its default value.

PowerShell alias (add to `$PROFILE`):

```powershell
function hex { python -m hexcli.agent @Args }
```

---

## Setup

`install.ps1` automates all of this. The manual steps below are the
reference for what it does (and for setting up without it). Four things,
once; `--doctor` verifies each one.

**1. Python deps** — `pip install numpy onnxruntime` (`ruff` is optional and
enables the `lint_code` tool).

**2. QAIRT SDK** — free Qualcomm developer account; not redistributable.
Install to `C:\Qualcomm\AIStack\QAIRT_2.47.0`, then set both variables:

```powershell
setx QNN_SDK_ROOT "C:\Qualcomm\AIStack\QAIRT_2.47.0"
setx ADSP_LIBRARY_PATH "C:\Qualcomm\AIStack\QAIRT_2.47.0\lib\hexagon-v73\unsigned"
```

Without `ADSP_LIBRARY_PATH`, npurun dies with `STATUS_STACK_BUFFER_OVERRUN`.

**3. npurun + a model** — a prebuilt ARM64 binary of our npurun fork ships as
`npurun-arm64.exe` on the [GitHub Releases](https://github.com/NathanL15/Hex-CLI/releases)
page (MIT/Apache-2.0), and `install.ps1` downloads it automatically. To build
from source instead: the fork carries fixes the tooling depends on (usage
reporting, token-precise `max_tokens`, mid-stream stop sequences, a UTF-8
crash fix), so build from `npurun/` here, not upstream:

```powershell
cd npurun
cmd /c "scripts\dev-shell-local.bat cargo install --path crates\npurun-cli"
npurun pull qwen3-4b-instruct-2507      # ~2.5 GB
```

**4. Embedding model** — powers semantic memory (~23 MB). Skipping it silently
disables memory, which is why `--doctor` checks for it:

```powershell
curl -L -o onnx/model_qint8_arm64.onnx https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_arm64.onnx
curl -L -o onnx/tokenizer.json https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json
```

Then `python launcher.py` starts the NPU server and the REPL together.

---

## Using it

### Slash commands

| | |
|---|---|
| `/help` | full command list |
| `/new` · `/clear` | new session · clear the screen |
| `/history` · `/resume <n>` | list · reopen a past session |
| `/diff` | what the agent changed this turn |
| `/undo` | revert the last exchange, restoring any files it wrote |
| `/stats` | turns, time, tokens, tool usage |
| `/context` · `/compact` | context budget · compress history now |
| `/save <name>` · `/load <name>` | session checkpoints |
| `/memory [status\|list\|search\|clear\|prune]` | inspect the memory store |
| `/config [key [value]]` | view or set config at runtime |
| `/tools` · `/model <name>` · `/mode <mode>` · `/cwd [path]` | |
| `/doctor` | diagnose the installation |
| `Esc` | cancel the running step (kills the whole process tree) |

### The input line

Persistent history, completion, and real editing — no dependency, just
`msvcrt`. Falls back to plain `input()` when stdin is not a terminal, so pipes
and CI are unaffected.

| | |
|---|---|
| `↑` · `↓` | history. With text already typed, it searches by that prefix |
| `Tab` | complete slash commands, `/config` keys, `/mode` values, file paths |
| `←` `→` · `Ctrl+←` `Ctrl+→` | by character · by word |
| `Home` · `End` | start · end of the current line |
| `Ctrl+W` · `Ctrl+U` · `Ctrl+K` | kill the word before · to line start · to line end |
| `Esc` | clear the line |
| paste | multi-line pastes stay one message instead of submitting line by line |
| `\` then `Enter` | continue on a new line deliberately |

History lives in `~/.shellai/input_history`. `rich_input: false` turns all of
this off.

### Project instructions

Drop an **`AGENTS.md`** in a project and the agent reads it every turn —
conventions, commands, gotchas. Keep it short: it is capped at ~1,200
characters, because every character competes with your actual request for the
model's limited context.

### Tools

`run_command` · `read_file` · `edit_file` · `write_file` · `append_file` ·
`list_directory` · `search_files` · `find_files` · `verify_syntax` ·
`run_code` · `lint_code` · `search_memory` · `fetch_url` · `batch` ·
`delegate` — `/tools` prints full signatures.

Worth knowing:
- **`edit_file` matches fuzzily.** Exact first, then whitespace- and
  indentation-tolerant, then a high-confidence closest match. Ambiguous
  matches are always an error, never a guess; a miss reports the closest
  region with line numbers so the next attempt can succeed.
- **`read_file` pages** through large files via `offset`/`limit`.
- Every mutation prints a **diff** and is captured for `/undo`.

---

## Safety

Four risk levels, checked in order — **destructive > sensitive > safe > caution**:

| Level | Examples | Behaviour |
|---|---|---|
| destructive | `Remove-Item`, `git reset --hard`, `format-*`, `iex` | confirm before running |
| **sensitive** | ssh/gpg/aws keys, hosts file, registry hives, credential vaults, `-EncodedCommand` | confirm before running; **denied automatically when non-interactive** |
| safe | `Get-*`, `ls`, `git status` | runs |
| caution | everything else | runs |

Plus:
- **Writes are confined to the working directory.** Reads are not — the agent
  can still consult docs and libraries elsewhere. Widen with
  `workspace_write_allow`; disable with `workspace_write_scope`.
- **Key and credential paths are hard boundaries** for file tools, regardless
  of confirmation.
- **Network access is deny-by-default.** `fetch_url` — the agent's only
  outbound channel — asks before every fetch and is refused automatically
  when non-interactive. `network_access: "deny"` removes the tool entirely;
  `"allow"` trusts it. An injected fetch is an exfiltration channel, so this
  does not depend on the model resisting either.
- Every classified command is appended to `.shellai/audit.log`.
- Text inside files and tool output is treated as data, never as instructions.

The sensitive tier exists because measured injection tests showed the model
complying with planted instructions while the old three-level classifier
happily allowed `Get-Content …\drivers\etc\hosts` — it matched the blanket
"`Get-*` is safe" rule. **The defence deliberately does not depend on the
model resisting**, because at 4B it frequently doesn't.

---

## Memory

Two on-device stores (project + global) over MiniLM embeddings with cosine
similarity — no external services. An idle "dreaming" pass consolidates recent
turns into durable rules that ride along in later prompts. `/memory` inspects
it; `memory_enabled: false` turns it off.

---

## Configuration

`shellai.json` (global) and `.shellai/config.json` (per project) are deep-merged
over the defaults, so you only specify what you change. `/config` lists and sets
values at runtime; `shellai.example.json` documents every key.

The ones most worth knowing:

| Key | Default | Effect |
|---|---|---|
| `protocol` | `"v1"` | agent protocol; `"v2"` is experimental |
| `max_agent_steps` | `15` | tool calls per turn |
| `live_streaming` | `true` | render answers as they arrive |
| `rich_input` | `true` | history, Tab completion, multi-line paste |
| `show_diffs` | `true` | print a diff after each mutation |
| `workspace_write_scope` | `true` | confine writes to the working directory |
| `autopilot_confirm_sensitive` | `true` | gate credential/key access |
| `network_access` | `"ask"` | confirm each `fetch_url`; `"deny"` / `"allow"` |
| `require_verification` | `true` | nudge the agent to check its own edits |
| `escalation_local_model` | `""` | bigger local model to consult when stuck |
| `memory_enabled` | `true` | semantic memory |

---

## Testing

CI (windows-latest) runs the compile gate, `ruff check hexcli/ evals/`, and
**19 offline suites (591 tests)** — no LLM required, all against a mock backend:

```powershell
python evals/test_core.py           # core coverage
python evals/test_agent_loop.py     # the loop, end to end
python evals/test_product_shell.py  # diffs, AGENTS.md, doctor, example config
python evals/test_lineedit.py       # input line: history, completion, paste
```

The input line injects its key source and output sink, so all 65 of its cases
run with no terminal at all.

**Live evals** (need the NPU server) grade real model behaviour:

```powershell
python evals/cases_smoke.py                    # fast gate
python evals/cases_extended.py --runs 5        # pass^5 over 36 cases
python evals/cases_multiturn.py --runs 3       # deep-context scenarios
python evals/compare.py <before.json> <after.json>
```

Cases are graded on **filesystem state and answer content**, not string
matching, and run N times to report pass@k and pass^k — a 4B model is
stochastic, so single runs mean very little.

> **Restart the NPU server before each suite.** After 1–2 hours of continuous
> traffic the Genie dialog degrades and returns errors for everything, which
> looks exactly like a model regression. The runner detects this and marks
> those runs invalid rather than failed, but a fresh server keeps results
> comparable.

`evals/harness.py`, `extended.py`, and `multiturn.py` are the superseded v1
instrument, kept for reference only.

---

## How it works

`docs/V2_PLAN.md` is the design record: measured hardware numbers, the protocol
experiment and why it was rejected, the eval methodology, and the findings
behind each safety layer. `ARCHITECTURE.md` covers module layout.

Hardware reality on a Snapdragon X Elite: **~15 tok/s decode, ~700 tok/s
prefill, 4,096-token compiled context.** Those numbers shape every design
decision here — terse output, aggressive compaction, and one action per turn
are consequences, not preferences.

---

## Notes

- Windows on ARM only (uses `msvcrt`, PowerShell, and the Hexagon NPU).
- Fully offline at runtime. The only network paths are the one-time setup
  downloads and the opt-in cloud escalation, which is disabled by default.
