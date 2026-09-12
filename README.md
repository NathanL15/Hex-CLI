# Hex CLI

Hex CLI is a coding agent for the Windows terminal, similar to aider or
Claude Code, that runs a local model on the Snapdragon Hexagon NPU. It reads
and edits files, runs commands, and checks its own work. The model is
Qwen3-4B, served by [npurun](https://github.com/bpbonker/npurun). After
setup it works offline.

```
you ▸ the median calc in processor.py is wrong for even-length lists, fix it
  → read_file
  → edit_file
~ processor.py  (+3 −1)
@@ -12,4 +12,6 @@
-    return sorted(data)[len(data) // 2]
+    mid = len(data) // 2
+    if len(data) % 2 == 0:
+        return (sorted(data)[mid - 1] + sorted(data)[mid]) / 2
  → run_code
Fixed: even-length lists now average the two middle values. Verified, the
test file prints 3.5 for [1, 2, 5, 6].
```

## Requirements

- Windows 11 on ARM with a Snapdragon X series chip
- Python 3.11 or newer
- The Qualcomm QAIRT SDK, 2.50 or newer (free developer account needed)

Tested on a Snapdragon X Elite. It will not run on x86 machines or on Macs.

## Install

```powershell
git clone https://github.com/NathanL15/Hex-CLI
cd Hex-CLI
.\install.ps1
```

The installer checks the machine, downloads npurun and the model, and runs
`--doctor` when it finishes. It skips steps that are already done, so run it
again after fixing anything it reports.

It cannot download the QAIRT SDK for you, because Qualcomm does not allow
redistribution. It prints instructions for that step and picks the SDK up on
the next run. See [Setup by hand](#setup-by-hand) if you want to do the
steps yourself.

The Start Menu shortcut opens Hex in Windows Terminal when it is installed,
through a "Hex CLI" profile the installer registers. It starts in your home
folder; `/cwd <path>` moves into a project, whose `AGENTS.md` then applies. Text selection, copy
and paste work there as in any other tab. Without Windows Terminal the
shortcut uses the classic console, where drag-select is off because Hex
disables QuickEdit (a click would otherwise freeze output); use the window
menu's Edit, Mark to copy there. To have `Hex CLI.cmd` itself open in
Windows Terminal, set it as the default terminal in its Settings, Startup.

## Usage

```powershell
python launcher.py                   # start the NPU server and the REPL
python -m hexcli.agent               # REPL only, if the server is already running
python -m hexcli.agent "what changed in this repo today?"
git diff | python -m hexcli.agent "review this diff"
echo "summarize README.md" | python -m hexcli.agent
python -m hexcli.agent --doctor      # check the install
python -m hexcli.agent --update      # pull latest source and refresh npurun
```

Piped input is added to the request as context, or used as the request if
there is no argument. Long input is trimmed to fit the context window.

To get a `hex` command, add this to your PowerShell `$PROFILE`:

```powershell
function hex { python -m hexcli.agent @Args }
```

### Commands

| Command | Does |
|---|---|
| `/help` | list all commands |
| `/clear` | clear the screen and the chat |
| `/new` | start a new chat, keep the scrollback |
| `/history` | list past sessions |
| `/resume <n>` | reopen a past session |
| `/search <text>` | find past sessions by content |
| `/diff` | show what the agent changed this turn |
| `/undo` | revert the last exchange, including any files it wrote |
| `/stats` | turns, time, tokens, context usage |
| `/context` | how full the context is and when it will compact |
| `/compact` | compress the history now |
| `/memory` | inspect the memory store |
| `/config [key [value]]` | view or set a config value for this session |
| `/setup` | config wizard, writes the config file |
| `/tools` | list the agent's tools |
| `/cwd [path]` | show or change the working directory |
| `/doctor` | check the install |
| `Esc` | cancel the running step |

You can add your own commands. Put a `.md` file in `.shellai/commands/` in
the project, or `~/.shellai/commands/` for all projects, and `/<filename>`
sends its content as the prompt. `$ARGUMENTS` in the file is replaced with
whatever follows the command.

```powershell
# .shellai/commands/review.md contains: Review $ARGUMENTS for bugs and style issues.
/review src/parser.py
```

### Editing keys

| Key | Does |
|---|---|
| `↑` `↓` | history. With text typed, searches by that prefix |
| `Tab` | complete commands, config keys, and file paths |
| `Ctrl+←` `Ctrl+→` | move by word |
| `Home` `End` | start or end of line |
| `Ctrl+W` `Ctrl+U` `Ctrl+K` | delete the word before, to line start, to line end |
| `Esc` | clear the line |
| `Ctrl+V` | paste a block. Nothing is sent until you press Enter |
| `Shift+Enter` | new line inside the entry |
| `\` then `Enter` | continue on a new line |
| `Ctrl+Plus` `Ctrl+Minus` | text size in the classic console |

History is saved in `~/.shellai/input_history`. When stdin is not a
terminal the agent falls back to plain `input()`, so pipes and CI work.

The input box stays on the last rows of the window, and the conversation
scrolls up above it. The status line under it
shows how full the context is, the NPU load (the counter behind Task
Manager's NPU graph), memory in use, and the working directory and branch.
Set `status_bar` to `false` for the old inline prompt.

### Project instructions

If a project has an `AGENTS.md`, the agent reads it every turn. Keep it
under about 1,200 characters. The model has a 4K token context and your
request has to fit in there too.

### Tools

`run_command`, `read_file`, `edit_file`, `write_file`, `append_file`,
`list_directory`, `search_files`, `find_files`, `verify_syntax`, `run_code`,
`lint_code`, `search_memory`, `fetch_url`, `batch`, `delegate`. Run `/tools`
for the full signatures.

`edit_file` tries an exact match first, then a whitespace-tolerant match,
then a close match if there is exactly one. If the match is ambiguous it
returns an error rather than guessing. `read_file` reads large files in
pages. Every file change prints a diff and can be reverted with `/undo`.

## Safety

Commands are classified before they run:

| Level | Examples | Behaviour |
|---|---|---|
| destructive | `Remove-Item`, `git reset --hard`, `format-*`, `iex` | asks first |
| sensitive | ssh/gpg/aws keys, hosts file, registry hives, credential stores, `-EncodedCommand` | asks first, denied when non-interactive |
| safe | `Get-*`, `ls`, `git status` | runs |
| caution | anything else | runs |

File writes stay inside the working directory unless you widen the scope
with `workspace_write_allow`. Reads can go anywhere. Key and credential
paths are blocked for all file tools.

`fetch_url` is the only tool that touches the network. It asks before every
fetch, and is refused when non-interactive. Set `network_access` to
`"deny"` to remove the tool or `"allow"` to skip the prompt.

Each classified command is appended to `.shellai/audit.log`. Text inside
files and tool output is treated as data, not instructions. A 4B model does
not reliably resist prompt injection, so none of the above depends on it
doing so.

## Configuration

Config is optional. `shellai.json` in the home directory and
`.shellai/config.json` in a project are merged over the defaults, so you
only need to write the keys you change. `shellai.example.json` lists every
key with its default.

| Key | Default | Effect |
|---|---|---|
| `max_agent_steps` | `15` | tool calls per turn |
| `live_streaming` | `true` | show the answer as it arrives |
| `rich_input` | `true` | history, Tab completion, multi-line paste |
| `side_padding` | `2` | left margin in columns |
| `status_bar` | `true` | input box and status line at the bottom |
| `user_highlight` | `true` | light band behind your messages in the transcript |
| `show_diffs` | `true` | print a diff after each file change |
| `workspace_write_scope` | `true` | keep writes inside the working directory |
| `autopilot_confirm_sensitive` | `true` | ask before touching keys and credentials |
| `network_access` | `"ask"` | `"deny"` or `"allow"` |
| `require_verification` | `true` | ask the agent to check its own edits |
| `prompt_split` | `true` | answer plain questions without the tool loop |
| `escalation_local_model` | `""` | a larger local model to consult when stuck |
| `memory_enabled` | `true` | semantic memory |
| `chat_log_enabled` | `true` | write full session logs |
| `protocol` | `"v1"` | `"v2"` is experimental |

## Logs and memory

Each session is written to `~/.shellai/chatlog/` as a JSONL file: every
request, every message sent to the model, every reply with its latency,
every tool call with its output. Secrets in the config are redacted.
`/stats` prints the path of the current file.

```powershell
python tools/chatlog_report.py                  # summary across all sessions
python tools/chatlog_report.py --last           # replay the most recent session
python tools/chatlog_report.py --session 1a2b   # replay one session by id prefix
```

Memory is two local stores, one per project and one global, built on MiniLM
embeddings. When idle, the agent condenses recent turns into short notes
that are included in later prompts. `/memory` shows what is stored.

## Setup by hand

These are the steps `install.ps1` performs. `--doctor` checks each one.

**1. Python packages**

```powershell
pip install numpy onnxruntime
```

`ruff` is optional and enables the `lint_code` tool.

**2. QAIRT SDK**

Install version 2.50 or newer to `C:\Qualcomm\AIStack\QAIRT_<version>`.
2.47 also works, but 2.50 lets the server keep the prompt cache between
calls, which makes turns about 40% faster.

The launcher uses the newest install it finds in that folder. To pin one,
set both variables:

```powershell
setx QNN_SDK_ROOT "C:\Qualcomm\AIStack\QAIRT_2.50.0"
setx ADSP_LIBRARY_PATH "C:\Qualcomm\AIStack\QAIRT_2.50.0\lib\hexagon-v73\unsigned"
```

If `ADSP_LIBRARY_PATH` is missing, npurun crashes with
`STATUS_STACK_BUFFER_OVERRUN`.

**3. npurun and a model**

The NPU server is a fork of npurun, at
[NathanL15/npurun](https://github.com/NathanL15/npurun) on the
`hexcli-fork` branch. Each release there has a prebuilt `npurun-arm64.exe`.
Hex CLI expects one specific build, set by `REQUIRED_NPURUN` in
`launcher.py`. `--doctor` fails on an older build and `--update` replaces it.

To build from source, use the fork. It has the prompt cache rewind, usage
reporting, exact `max_tokens`, and request watchdog that Hex CLI relies on.

```powershell
git clone -b hexcli-fork https://github.com/NathanL15/npurun
cd npurun
cmd /c "scripts\dev-shell-local.bat cargo install --path crates\npurun-cli"
npurun pull qwen3-4b-instruct-2507      # about 2.5 GB
```

**4. Embedding model**

About 23 MB. Memory is disabled without it.

```powershell
curl -L -o onnx/model_qint8_arm64.onnx https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_arm64.onnx
curl -L -o onnx/tokenizer.json https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json
```

## Limitations

- A 4B model. It handles small, well-defined edits and questions about a
  codebase. It is not a match for a hosted frontier model on large or vague
  tasks.
- 4,096 tokens of context. History is compacted often, and long files are
  read in pages.
- About 15 tokens per second on a Snapdragon X Elite. A follow-up turn takes
  a second or two. The first turn of a new conversation takes longer while
  the server rebuilds the prompt cache.
- Windows on ARM only. Nothing here is portable to other platforms without
  a different backend.

## Development

CI runs `ruff` and 25 offline test suites against a mock backend, so no NPU
is needed for those:

```powershell
python evals/test_core.py
python evals/test_agent_loop.py
python evals/test_product_shell.py
python evals/test_lineedit.py
```

The live evals need the NPU server. They check what the model actually did,
by looking at the filesystem and the answer, and run each case several
times because the model is not deterministic.

```powershell
python evals/cases_smoke.py                        # quick check
python evals/cases_extended.py --runs 3            # 41 cases
python evals/cases_multiturn.py --runs 3 --think-time 15
python evals/compare.py <before.json> <after.json>
python evals/gate.py --baseline <base.json> <candidate.json>
```

Restart the NPU server before each suite. After an hour or two of steady
use it starts returning errors for everything, which looks like a model
regression. The runner detects this and marks those runs invalid.

`ARCHITECTURE.md` describes the module layout. `docs/V2_PLAN.md` has the
hardware measurements, the eval method, and the reasoning behind each
safety layer. `RELEASING.md` covers how a release is cut.

## License

MIT. See [LICENSE](LICENSE).

npurun is Apache 2.0. The QAIRT SDK is Qualcomm's and has its own terms.
