# Hex CLI

A coding agent for the terminal that runs on your own machine. It uses
Qwen3-4B on the Snapdragon Hexagon NPU through
[npurun](https://github.com/bpbonker/npurun). No cloud, no API key, nothing
leaves the box.

Plain Python plus NumPy and ONNX Runtime. No agent framework.

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

Windows on ARM only. Tested on a Snapdragon X Elite.

## Install

```powershell
git clone https://github.com/NathanL15/Hex-CLI
cd Hex-CLI
.\install.ps1
```

The installer checks the machine, downloads npurun and the model, and runs
`--doctor` at the end. It skips steps that are already done, so you can run
it again after fixing anything it reports.

One step it cannot do for you: the QAIRT SDK. Qualcomm does not allow
redistribution, so the installer prints instructions and picks the SDK up on
the next run. See [Setup by hand](#setup-by-hand) for details.

## Run

```powershell
python launcher.py                   # starts the NPU server and the REPL
python -m hexcli.agent               # REPL only, if the server is already up
python -m hexcli.agent "what changed in this repo today?"
git diff | python -m hexcli.agent "review this diff"
echo "summarize README.md" | python -m hexcli.agent
python -m hexcli.agent --doctor      # check the install
python -m hexcli.agent --update      # pull latest source and refresh npurun
```

Piped input becomes context for the request, or the request itself if no
argument is given. It is trimmed to fit the 4K token window and marked as
data, not instructions.

To get a `hex` command, add this to your PowerShell `$PROFILE`:

```powershell
function hex { python -m hexcli.agent @Args }
```

## Setup by hand

This is what `install.ps1` does. Use it if you want to set things up
yourself or need to fix a step. `--doctor` checks all four.

**1. Python packages**

```powershell
pip install numpy onnxruntime
```

`ruff` is optional. It enables the `lint_code` tool.

**2. QAIRT SDK**

Needs a free Qualcomm developer account. Install version 2.50 or newer to
`C:\Qualcomm\AIStack\QAIRT_<version>`. 2.47 works, but 2.50 lets the server
keep the prompt cache between calls, which makes every turn about 40% faster.

The launcher uses the newest install it finds under that folder. To pin one,
set both variables:

```powershell
setx QNN_SDK_ROOT "C:\Qualcomm\AIStack\QAIRT_2.50.0"
setx ADSP_LIBRARY_PATH "C:\Qualcomm\AIStack\QAIRT_2.50.0\lib\hexagon-v73\unsigned"
```

Without `ADSP_LIBRARY_PATH`, npurun crashes with `STATUS_STACK_BUFFER_OVERRUN`.

**3. npurun and a model**

The NPU server is a fork of npurun at
[NathanL15/npurun](https://github.com/NathanL15/npurun), branch
`hexcli-fork`. Every release there has a prebuilt `npurun-arm64.exe`.

Hex CLI is written against one specific build (`REQUIRED_NPURUN` in
`launcher.py`). The installer downloads that build, `--doctor` fails on an
older one, and `--update` replaces it.

To build from source, build the fork rather than upstream. It carries the
features Hex CLI depends on: prompt cache rewind, usage reporting, exact
`max_tokens`, and a watchdog that ends stalled requests.

```powershell
git clone -b hexcli-fork https://github.com/NathanL15/npurun
cd npurun
cmd /c "scripts\dev-shell-local.bat cargo install --path crates\npurun-cli"
npurun pull qwen3-4b-instruct-2507      # about 2.5 GB
```

**4. Embedding model**

About 23 MB. It powers memory. Without it memory is off, and `--doctor`
will tell you.

```powershell
curl -L -o onnx/model_qint8_arm64.onnx https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_arm64.onnx
curl -L -o onnx/tokenizer.json https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json
```

## Using it

### Commands

| | |
|---|---|
| `/help` | full command list |
| `/clear` | clear the screen and the chat |
| `/new` | new chat, keep the scrollback |
| `/history` | list past sessions |
| `/resume <n>` | reopen a past session |
| `/search <text>` | find past sessions by content |
| `/diff` | what the agent changed this turn |
| `/undo` | revert the last exchange, including files it wrote |
| `/stats` | turns, time, tokens, context usage |
| `/context` | how full the context is and when it compacts |
| `/compact` | compress the history now |
| `/memory` | inspect the memory store |
| `/config [key [value]]` | view or set a config value for this session |
| `/setup` | config wizard, saves to the config file |
| `/tools` | list the agent's tools |
| `/cwd [path]` | show or change the working directory |
| `/doctor` | check the install |
| `Esc` | cancel the running step |

### Custom commands

Put a `.md` file in `.shellai/commands/` (per project) or
`~/.shellai/commands/` (global). `/<filename>` sends its content as the
prompt. `$ARGUMENTS` in the file is replaced with whatever follows the
command. Without it, the arguments are added to the end.

```powershell
# .shellai/commands/review.md contains: Review $ARGUMENTS for bugs and style issues.
/review src/parser.py
```

Project commands override global ones. Built-in commands override both.
Files are read on every use, so edits apply right away.

### The input line

History, completion, and editing keys, with no extra dependency. When stdin
is not a terminal it falls back to plain `input()`, so pipes and CI still
work.

| | |
|---|---|
| `↑` `↓` | history. With text typed, searches by that prefix |
| `Tab` | complete commands, config keys, and file paths |
| `Ctrl+←` `Ctrl+→` | move by word |
| `Home` `End` | start or end of line |
| `Ctrl+W` `Ctrl+U` `Ctrl+K` | delete the word before, to line start, to line end |
| `Esc` | clear the line |
| `Ctrl+Plus` `Ctrl+Minus` | text size in the classic console (Windows Terminal has its own zoom) |
| `Ctrl+V` | paste a block whole. Nothing is sent until you press Enter |
| `\` then `Enter` | continue on a new line |

History is saved in `~/.shellai/input_history`. Set `rich_input` to `false`
to turn all of this off.

### Project instructions

Put an `AGENTS.md` in a project and the agent reads it every turn. Keep it
short. It is capped at about 1,200 characters, because the model only has
4K tokens of context and your request has to fit too.

### Tools

`run_command`, `read_file`, `edit_file`, `write_file`, `append_file`,
`list_directory`, `search_files`, `find_files`, `verify_syntax`, `run_code`,
`lint_code`, `search_memory`, `fetch_url`, `batch`, `delegate`. `/tools`
prints the full signatures.

- `edit_file` tries an exact match first, then ignores whitespace, then
  takes a close match if there is only one. If a match is ambiguous it
  errors instead of guessing. On a miss it reports the closest region with
  line numbers.
- `read_file` reads large files in pages with `offset` and `limit`.
- Every file change prints a diff and can be reverted with `/undo`.

## Safety

Every command is classified into one of four levels before it runs:

| Level | Examples | What happens |
|---|---|---|
| destructive | `Remove-Item`, `git reset --hard`, `format-*`, `iex` | asks first |
| sensitive | ssh/gpg/aws keys, hosts file, registry hives, credential vaults, `-EncodedCommand` | asks first, denied when non-interactive |
| safe | `Get-*`, `ls`, `git status` | runs |
| caution | everything else | runs |

Also:

- Writes stay inside the working directory. Reads can go anywhere, so the
  agent can still look at docs and libraries. `workspace_write_allow` widens
  this and `workspace_write_scope` turns it off.
- Key and credential paths are off limits to the file tools, no matter what.
- Network access is off by default. `fetch_url` is the only outbound tool.
  It asks before every fetch and is refused when non-interactive.
  `network_access` can be set to `"deny"` to remove the tool or `"allow"` to
  trust it.
- Every classified command is logged to `.shellai/audit.log`.
- Text inside files and tool output is treated as data, not instructions.

The sensitive level exists because injection tests showed the model
following planted instructions, and the old classifier let
`Get-Content ...\drivers\etc\hosts` through as a safe `Get-*` command. A 4B
model often does not resist injection, so the protection does not rely on
it.

## Chat log

Every session is written to `~/.shellai/chatlog/`, one JSONL file per
session. It records the versions in use, the config (secrets redacted),
each request, each message sent to the model, each reply with its latency,
each tool call with its full output, and how every turn ended. `/stats`
prints the current file's path.

```powershell
python tools/chatlog_report.py                  # summary across all sessions
python tools/chatlog_report.py --last           # replay the most recent session
python tools/chatlog_report.py --session 1a2b   # replay one session by id prefix
```

Logs stay on your machine. `chat_log_enabled` turns this off and
`chat_log_dir` moves it.

## Memory

Two local stores, one per project and one global, built on MiniLM
embeddings. An idle pass turns recent turns into short rules that are
included in later prompts. `/memory` inspects it. `memory_enabled` turns it
off.

## Configuration

Config is optional. `shellai.json` (global) and `.shellai/config.json` (per
project) are merged over the defaults, so you only write the keys you want
to change. `shellai.example.json` lists every key with its default.

| Key | Default | Effect |
|---|---|---|
| `max_agent_steps` | `15` | tool calls per turn |
| `live_streaming` | `true` | show answers as they arrive |
| `rich_input` | `true` | history, Tab completion, multi-line paste |
| `side_padding` | `2` | left margin in columns |
| `show_diffs` | `true` | print a diff after each file change |
| `workspace_write_scope` | `true` | keep writes inside the working directory |
| `autopilot_confirm_sensitive` | `true` | ask before touching keys and credentials |
| `network_access` | `"ask"` | `"deny"` or `"allow"` |
| `require_verification` | `true` | ask the agent to check its own edits |
| `prompt_split` | `true` | skip the tool loop for plain questions |
| `escalation_local_model` | `""` | a bigger local model to consult when stuck |
| `memory_enabled` | `true` | semantic memory |
| `protocol` | `"v1"` | `"v2"` is experimental |

## Testing

CI runs `ruff` and 25 offline suites against a mock backend. No NPU needed.

```powershell
python evals/test_core.py
python evals/test_agent_loop.py
python evals/test_product_shell.py
python evals/test_lineedit.py
```

Live evals need the NPU server. They grade real model behaviour on
filesystem state and answer content, not string matching, and run each case
several times because a 4B model is not deterministic.

```powershell
python evals/cases_smoke.py                        # quick check
python evals/cases_extended.py --runs 3            # 41 cases
python evals/cases_multiturn.py --runs 3 --think-time 15
python evals/compare.py <before.json> <after.json>
python evals/gate.py --baseline <base.json> <candidate.json>
```

Every results file records the git SHA, npurun version, context budget, and
case order seed, plus a latency check at the start and end of the run. If
the server slowed down during the run the file is flagged as not comparable.

The gate is pass or fail. It takes the cases that passed every run in every
baseline and requires the candidate to keep each one. A single miss triggers
a recheck with more runs.

Restart the NPU server before each suite. After an hour or two of
continuous use it starts returning errors for everything, which looks like a
model regression. The runner detects this and marks those runs invalid.

## How it works

`ARCHITECTURE.md` covers the module layout. `docs/V2_PLAN.md` is the design
record: hardware measurements, the eval method, and the reasoning behind
each safety layer.

The hardware sets the rules. On a Snapdragon X Elite the model decodes at
about 15 tokens per second, prefills at about 700, and has a 4,096-token
context. That is why the output is terse, the history compacts often, and
the agent does one thing per turn.

## License

MIT. See [LICENSE](LICENSE).

npurun is Apache 2.0. The QAIRT SDK is Qualcomm's and has its own terms.
