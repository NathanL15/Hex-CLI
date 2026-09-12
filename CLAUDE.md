# CLAUDE.md — Hex CLI

Read this first in every session. It is the project's working knowledge for
Claude Code: what the system is, where things live, how work is developed,
tested, measured and released, and which questions are already settled.
Everything here was verified against the tree on 2026-09-10 and brought up
to date through the 2.7.1 release on 2026-09-12 (code at
`__version__ = "2.7.1"`). When this file and the code disagree, the code
wins; fix this file in the same commit.

`AGENTS.md` in this repo is NOT for you. Hex CLI itself reads `AGENTS.md`
every turn and caps it near 1,200 characters. Leave it short.

---

## 1. What this is

A local-first terminal coding agent (Claude Code shape) that runs entirely on
a Snapdragon X Elite laptop: **Qwen3-4B-Instruct-2507 (W4A16)** on the
Hexagon NPU through **npurun** (a Rust runtime wrapping Qualcomm's Genie/QNN
SDK, exposing an OpenAI-compatible server). No cloud, no API key, no code
leaves the machine. Python 3.11+, **stdlib + numpy + onnxruntime only**.
Windows-on-ARM only by design (`msvcrt`, PowerShell tool dispatch).

The model is small, so the thesis of the project is: *the harness is the
product*. Behaviour is measured on real filesystem state with a statistical
instrument, not judged by reading transcripts. The model is treated as
always injectable; every safety property lives in the harness.

Hard numbers that shape every decision:

| Fact | Value |
|---|---|
| Context window (compiled into the Genie graphs) | 4,096 tokens, cannot be raised |
| Server input budget (fork 0.2.1+) | context − 400 = 3,696 tokens |
| System prompt (stable prefix, GGUF tokenizer) | ~2,236 tokens, rules ≈ 72 % of it |
| History room after prompt | ~850 tokens (history floor 250) |
| Decode | ~15–19 tok/s; stepped by context, ~15 above 2,000 |
| Follow-up turn first token (Rewind warm) | ~0.7–1.0 s; dialog rebuild 5–8 s |
| Rewind discard limit | exactly 600 tokens; divergence ceiling ~3,100–3,250 cached tokens |
| Tool result that overflows the window | returns an EMPTY generation |

---

## 2. Where things are

| Thing | Location |
|---|---|
| Canonical clone | `C:\Users\Natha\Documents\GitHub\Hex-CLI` (origin `NathanL15/Hex-CLI`, branch `main`) |
| Old clone (stale, do not develop here) | `C:\Users\Natha\local-shell-ai` (at 2.2.0) |
| npurun fork source | `C:\Users\Natha\local-shell-ai\npurun`, branch `hexcli-fork`; remotes `origin` = bpbonker/npurun (upstream), `fork` = NathanL15/npurun (releases live here) |
| Production npurun binary | `~/.cargo/bin/npurun.exe` (0.2.3 = the pin). `<repo>/npurun-arm64.exe` is where the installer/`--update` would put a download; currently absent |
| QAIRT SDKs | `C:\Qualcomm\AIStack\QAIRT_2.47.0` and `QAIRT_2.50.0`. Launcher picks the newest valid one; 2.50 enables Rewind |
| Model bundle | `%LOCALAPPDATA%\npurun\models\qwen3-4b-instruct-2507` (also `-8k` and `qwen3-8b`, both rejected) |
| Embedding model (memory) | `<repo>/onnx/model_qint8_arm64.onnx` + `tokenizer.json`, gitignored, per machine |
| Runtime config on the NPU path | `<repo>/shellai_npurun.json` (gitignored, written by the launcher) |
| Per-user config | `~/shellai.json`; per-project `.shellai/config.json` deep-merged over it |
| Session state | `<cwd>/.shellai/` (audit.log, logs/, checkpoints/, vector_store/, shellai.lock) and `~/.shellai/` (chatlog/, input_history, commands/) |
| Server log | `<repo>/npurun_server.log`, truncated on every start |
| Eval results | `evals/results/` (gitignored). Gate baselines: `ask_rule_r5_20260905.json` + `baseline_20260905.json`; scoreboard `LATEST.md` |
| Study data | `docs/backend_study/{data,data_npu_ab}/` (gitignored), summaries tracked |
| Claude Code memory for this project | `~/.claude/projects/C--Users-Natha/memory/` (hexcli_*.md, project_local_shell_ai.md) — historical detail beyond this file |

---

## 3. Runtime stack, end to end

`Hex CLI.cmd` / `shellai.cmd` → `python launcher.py` (ignores argv) →
`python shellai.py --config shellai_npurun.json` → `hexcli.agent.main()`.

`launcher.py` does, in order: find npurun (`~/.cargo/bin` first, then
`<repo>/npurun-arm64.exe`, then PATH; a candidate older than
`REQUIRED_NPURUN` yields to a newer one) → find QAIRT → decide the Rewind
runtime (QAIRT ≥ 2.50 AND npurun ≥ 0.2.0) → build the server env → start
`npurun serve --model qwen3-4b-instruct-2507 --bind 127.0.0.1:11435`
detached, wait ≤ 60 s on `/healthz` → write `shellai_npurun.json` (only when
missing) → exec the agent. Fallbacks below the NPU tier (DirectML Phi-4 on
port 8123, Ollama CPU) exist in code but are not used or maintained.

Environment the launcher sets for the server (`_npurun_env()`):

| Var | Value | Why |
|---|---|---|
| `QNN_SDK_ROOT`, `PATH` | the chosen QAIRT | |
| `ADSP_LIBRARY_PATH` | `<QAIRT>/lib/hexagon-v73/unsigned` | without it npurun dies with `STATUS_STACK_BUFFER_OVERRUN` |
| `NPURUN_REWIND` | `2` when Rewind runtime available, else unset | never reset the dialog; Rewind every warm query; rebuild in place on failure |
| `NPURUN_HTP_POLL` | `0` (setdefault) | host busy-polling off: idle 14.4 → 4.4 W, decode 15.5 → 19 tok/s |
| `NPURUN_HTP_ASYNC_INIT` | `0` (setdefault) | async dialog init multiplied the ≥2.9K-context hang rate ~10× (2.6.2 fix) |

Runtime-coupled config keys the launcher writes when Rewind is on:
`prompt_stable_prefix=true` (byte-stable system prompt so the KV prefix
matches) and `prompt_split=false` (the direct stage diverges the prefix and
costs a rebuild). Other fork knobs exist (`NPURUN_HTP_THREADS`,
`NPURUN_HTP_PERF_PROFILE`, `NPURUN_HTP_RPC_POLLING_US`,
`NPURUN_HTP_HMX_TIMEOUT_US`, `NPURUN_INPUT_BUDGET`, `NPURUN_OUTPUT_RESERVE`,
`NPURUN_REWIND_MAX_CACHED`) but the launcher does not set them.

**Fresh server restart** (there is no `--restart` flag; `launcher.py` alone
will not restart a running server):

```powershell
taskkill /F /IM npurun.exe; Start-Sleep 3
python -c "import launcher; launcher._start_npurun_server()"
# then poll until 200: curl -s -m 2 http://127.0.0.1:11435/v1/models
```

The REPL's own restart path is `repl.restart_backend()` (prompted after a
backend failure); it reuses `launcher._npurun_env()` so the restart matches
the launcher's runtime choice.

Agent CLI (`hexcli.agent.parse_args`, prog `shellai`): `[query...]`,
`--config`, `--backend`, `--model`, `--print-config`, `--version`,
`--doctor`, `--debug`, `--fast`, `--raw`, `--yolo`, `--update`,
`--uninstall`. Piped stdin becomes context (or the query if none), capped at
6,000 chars head+tail, wrapped as data-not-instructions. Exit 130 on cancel,
2 on backend errors.

Config precedence, lowest to highest: `config.DEFAULT_CONFIG` → `--config`
file (auto-created if missing) → `<cwd>/.shellai/config.json` → CLI flags.
`shellai.example.json` is generated by `tools/gen_example_config.py`; a test
fails if it drifts. Anything settable via `/config` must be in
`config._CONFIG_SETTABLE`.

---

## 4. Code map (`hexcli/`, 33 modules, none over 1,700 lines)

agent.py 3,818 → 1,682 lines after "the Split" (2026-09-01, 7 stages).
Package rule: no module over ~800 lines except agent.py.

| Module | Owns |
|---|---|
| `agent.py` | the hub: prompt assembly, direct-stage router, `run_autopilot` (protocol v1 loop), `execute_tool_call` dispatch, `main()`, `AutopilotProbe` (the eval seam), `_CURRENT_SESSION_ID`, prewarm/prime. Re-binds nearly every moved name so `sa.<name>` patches still intercept |
| `repl.py` | interactive loop, `REPL_COMMANDS` (18 commands + `/quit` alias), `/config` `/memory` `/stats` handlers, `restart_backend`, `_AgentProxy` (call-time window onto agent that closes the import cycle) |
| `llm.py` | transport: ollama / OpenAI-compatible / mock; streaming; token estimator; 429 wait. Module-level `__getattr__` borrows names from agent at call time |
| `http_client.py` | one cached keep-alive connection per host:port |
| `parsing.py` | v1 protocol text: `parse_agent_action`, `TOOL_NAMES`, trimming, small-talk/help detection |
| `prompts.py` | every model-facing string. **The most behaviour-critical file. Edit only against `evals/cases_extended.py` with the pass^k protocol (§7).** Ruff whitespace rules are off here on purpose; do not "clean" it |
| `compaction.py` | deterministic merge-aware compactor, LLM `/compact`, history budget, dry-run thrash guard (`_AUTO_COMPACT_MIN_GAIN_TOKENS=100`), context gauge |
| `tools.py` | the 15 leaf tools + `guard_mutation` (write scope, sensitive paths). **Owns `_HOME`; patch `hexcli.tools._HOME`, never `sa._HOME`** |
| `safety.py` | command classifier (destructive > sensitive > safe > caution) + JSONL audit log |
| `cancel.py` | Esc cancel via msvcrt polling. Eval runner must silence BOTH `hexcli.agent` and `hexcli.cancel` |
| `config.py` | `DEFAULT_CONFIG`, deep-merge loader, `_CONFIG_SETTABLE` |
| `loop_v2.py`, `protocol_v2.py`, `shell_session.py` | protocol v2 (`<action>` tags, SEARCH/REPLACE, persistent shell). Opt-in via `protocol: "v2"`; lost the A/B (13/36 vs 22/35). Safety/verification/file-tool changes must land in BOTH protocols |
| `memory.py` | MiniLM vector memory, `search_memory`, memory rules, dreaming daemon (OFF: it fabricated hardware facts) |
| `sessions.py` | session store; **owns `HISTORY_PATH` — patch it here, not on agent** |
| `chatlog.py`, `telemetry.py` | full JSONL transcript per session (`~/.shellai/chatlog/`); redacted structured logs |
| `lineedit.py`, `ui.py`, `stream_render.py`, `diffview.py` | input line (history, Tab, paste, zoom, chrome rows + idle repaint), presentation, live stream renderer, undo-snapshot diffs. One-way dependency: agent → ui |
| `markdown_stream.py` | markdown-lite → ANSI for streamed answers, one char at a time (headings, bullets, bold, code spans, fences as dim rules). Hooked into `llm._make_live_renderer`'s emit and `ui.render_result`; invariant: whole-text and per-char feeds give identical output. A "```" line that is not `` ``` `` + a language name is released literally |
| `statusbar.py` | the bottom section: input box + status line (`context`, `npu`, `mem`, cwd/branch). `LiveArea` wraps stdout/stderr between reads (erase box → write → redraw, one lock shared with the spinner); the editor draws the same rows as chrome while reading. NPU % = PDH `GPU Engine` counter for the LUID DirectX does not list; memory = `GlobalMemoryStatusEx`; 1 s sampler thread. `ui.LIVE_AREA` is the hook the spinner and `llm.on_tool` use |
| `doctor.py`, `distribution.py`, `setup_wizard.py`, `commands.py` | `--doctor`, `--update`/`--uninstall`, `/setup`, custom `.md` slash commands |
| `escalate.py`, `local_escalation.py`, `network.py`, `lockfile.py` | cloud escalation (opt-in, key), local ladder (dormant, no viable bigger model), `fetch_url` + online probe, advisory PID lock |

**Turn flow (v1)**: early exits (help / meta / small talk, no model call) →
sync `context_window_tokens` from the server's advertised budget → build
prompt (head + rules 1–14 + tool tail; rules 13/14 conditional; schemas for
`search_memory` / `lint_code` / `fetch_url` / `batch` / `delegate` appended
by keyword) → direct-stage router (`prompt_split`: ≤200-char knowledge
queries get a ~350-token no-tools prompt, tools refused harness-side) →
per step (≤ `max_agent_steps`=15): `call_llm` with `json_format`, up to 2
retries on empty/botched output, one JSON action per reply
(`{"action":tool,"args":{}}` or `{"action":"finish","message":""}`) →
finish gates (one verification nudge after an unverified mutation;
refusal nudge; ambiguous-request questions are the CORRECT outcome, never
escalated) → tool dispatch with a per-step output budget
(`_step_tool_output_limit`: room left in the window, floor 1,200 chars;
`read_file` pages 400 lines with a continuation header) → loop detection
(3 identical results or 3 failures on one target) → `finally` prewarm the
server with the system prompt.

**Wiring traps (each cost a real bug)**:
- `_CURRENT_SESSION_ID` must be read through the agent hub (`_agent()._CURRENT_SESSION_ID`). A module-local copy in `llm.py` breaks npurun's continuation detection in production while every mock test stays green.
- A module-level `__getattr__` does not serve bare globals inside that module's own functions.
- Names moved out of agent.py are re-bound there by name; tests patch `sa.<name>`. When moving code, keep the re-bind and resolve borrowed names through the hub at call time, never by copying.
- Test fixtures built from `tempfile.mkdtemp()` must be `.resolve()`d (CI's TEMP is an 8.3 short path), or the sensitive-path check silently never fires.
- Refusal messages must never name an alternative route; the model reads "use run_command instead" and does exactly that.
- `sys.stdin.isatty()` is True in a hidden/detached console; consent prompts poll `msvcrt` with an idle timeout (`ui.confirm_or_deny`) and fail closed. Non-interactive = deny.
- `<tool_call>` is a Qwen3 special token the W4A16 detokenizer garbles; only `<action>` round-trips. Server-side tool parsing can never work on this bundle.

---

## 5. Development workflow

1. Work on `main` in the canonical clone. Small focused commits; commit message = one factual title, body explains the measured reason. No taglines.
2. Before claiming anything works:
   ```powershell
   ruff check hexcli/ evals/
   python -m compileall hexcli/ evals/ -q
   python evals/test_core.py            # plus every suite covering what you touched
   ```
   Full offline set = the step list in `.github/workflows/ci.yml` (27 files, 771 tests). No aggregate script exists; loop over `evals/test_*.py`. Each file is a standalone script with its own `TESTS` list and prints `N/N passed`; no pytest.
3. Anything touching the model path (prompts, compaction, tools the model sees, launcher env, runtime keys) also needs a live check: at minimum `python evals/cases_smoke.py` on a fresh server (10/10), and for behaviour changes the A/B protocol in §7.
4. Never weaken a test to make it pass. If the test is wrong, fix it and say why in the commit. Never add a dependency without a strong reason.
5. Update `CHANGELOG.md` `## Unreleased` with the measured effect. Numbers are pass^k over repeated live runs, never single anecdotes.
6. Keep `shellai.example.json` in sync (`python tools/gen_example_config.py`). Keep `README.md` factual and short; it does not restate the version.

**Mocking the model in tests**: set `"backend": "mock"` and queue raw replies
with `sa.set_mock_responses([...])`; tools run for real in a sandbox.
`evals/runner.py::_EvalEnv` is the isolation pattern (chdir sandbox, memory
paths redirected outside it, Spinner/CancelMonitor no-op'd in both modules).
Common patch targets: `tools._HOME`, `sessions.HISTORY_PATH`,
`mem._RULES_PATH`, `ui.cprint`, `ui.confirm_*`, `sa.call_llm`,
`sa.execute_tool_call`, `launcher._npurun_version`.

**Long jobs on this machine**: background processes get killed by the
low-memory watchdog (16 GB). Run evals in the foreground, chunked
(`evals/run_chunk.py`), under 10 minutes per invocation. Before touching
port 11435, check whether another Claude session is using it (`ListAgents`,
or ask); never restart a server that another session owns.

---

## 6. Testing and measurement

Two tiers. **Tier 1 offline** (mock backend, CI on windows-latest). **Tier 2
live** (real NPU server), driven through the production `run_autopilot` via
the `AutopilotProbe` seam, so prompt parity is structural.

Live suites (`evals/`), all through `runner.run_suite_cli`:

| Suite | Cases | Purpose | Typical command |
|---|---|---|---|
| `cases_smoke.py` | 10 | merge gate | `python evals/cases_smoke.py` → 10/10 |
| `cases_extended.py` | 41 | headline adversarial + regression | `python evals/cases_extended.py --runs 3 --seed 20260905` (~45 min) |
| `cases_multiturn.py` | 3 scenarios, 16 turns | long-context, injection (uc3), REPL-parity history | `python evals/cases_multiturn.py --runs 3 --think-time 15` |
| `cases_everyday.py` | 30 | machine-truth questions (RAM, CPU, dates, arithmetic) | `--runs 3` default |
| `cases_cliff.py` | 6 families × size buckets | quality vs input size | own parser |

Flags: `--case`, `--runs`, `--protocol v1|v2`, `--set KEY=VALUE`, `--seed`,
`--think-time`, `--no-save`. Output is always
`evals/results/<suite>_results.json` (overwritten): copy it to a dated name
immediately. The JSON records identity metadata (git sha + dirty, npurun
version, server budget/window, seed, overrides) and a latency canary at
start and end; two files are comparable only when identity agrees.

The instrument: `runner.py` (cases, traces, aggregate, preflight),
`checks.py` (state/content/behaviour graders composed with
`all_of`/`any_of`), `stats.py` (exact Fisher run-level, McNemar case-level;
p > 0.05 at n=3 is absence of evidence, not parity), `compare.py` (two-arm
diff), `gate.py` (binary ship gate), `run_chunk.py` (foreground chunks merged
into one file), `regrade.py` (re-apply trace-only graders to saved traces;
refuses truncated tool output). `harness.py`, `extended.py`, `multiturn.py`
are the superseded v1 instrument; do not use them.

**The gate** (`evals/gate.py`): gate set = cases at 3/3 in EVERY baseline
(27 of 41 today). A candidate must keep every one; one miss at 3 runs =
RECHECK at 6 runs with one miss allowed; the rest is a ceiling panel,
reported not gated. Canary drift > 2× flags every late case.

```powershell
python evals/gate.py --baseline evals/results/ask_rule_r5_20260905.json --baseline evals/results/baseline_20260905.json evals/results/<candidate>.json
python evals/compare.py <before.json> <after.json>
```

**Measurement traps (each corrupted real runs)**:
- A server that has served 1–2 h of eval traffic degrades into sticky `ERROR_QUERY_FAILED (-6)`, which grades exactly like a model regression. Restart before every suite. The runner marks 5xx / URLError / -6 runs INVALID and refuses to start against a dead backend; a file with invalid runs is re-run, not compared.
- Never compare arm A vs arm B across time on this NPU; platform state flips. Fresh server per arm, one variable changed, same seed.
- ±1 run on a 3-run case is noise. Read the traces before believing a verdict; several "model failures" were grader or infrastructure bugs (livestate-1 was structurally unpassable for weeks; `answer_matches` vs `regex_answer_matches`).
- Baseline protocol is v1; `extended_v2` in a filename is the instrument version, not the protocol. Read the `protocol` field.
- Non-interactive consent: `cmd /c "python evals\... < NUL"` auto-denies (matches recorded baselines); `--set autopilot_confirm_destructive=false` auto-allows. Different conditions, not comparable.
- Zero think time makes the prewarm's rebuild land on the next case; the multiturn arm uses `--think-time 15` for that reason.
- Any probe that truncates replies with a small `max_tokens` poisons Rewind (Genie −1 after a length-truncated reply). Use natural stops.

---

## 7. Changing model-facing behaviour

Model-facing = `hexcli/prompts.py`, the condensed-history ack, tool schemas
and result formats, compaction shape, the launcher environment, runtime
config keys. The model is specialised to the exact wording; format
specialisation is the strongest measured effect in the project.

Protocol: snapshot the baseline JSON → change ONE variable → fresh server →
`cases_extended.py --runs 3` (5 for prompt text) → `gate.py` against both
baselines → `compare.py` → for anything affecting multi-turn or the
runtime, also `cases_multiturn.py --runs 3 --think-time 15` (no 3/3 case
lost) and the stall probe
`python tools/backend_bench/stall_rate.py --user-tokens 800 --n 20 --max-tokens 400`
on AC (≤ 1 hang in 20). Record the numbers in the CHANGELOG. Revert on a
loss; do not argue with the instrument.

---

## 8. Releasing (`RELEASING.md` is authoritative)

- One version source: `hexcli/__init__.py`. CI refuses a `v*` tag that does not match.
- Patch = no model-facing change and nothing the launcher hands the server. Minor = features, any `prompts.py` change, launcher env or runtime keys, a `REQUIRED_NPURUN` bump. Major = model, runtime generation or history representation. 2.7.0 shipped 2026-09-12 (Terminal layout, persona fixes, installer, the two loop nudges and the named-file guard); the next release is 2.7.1 or 2.8.0 by the rule above.
- Fork changes ship as fork releases first (`vX.Y.Z` on NathanL15/npurun with `npurun-arm64.exe` attached, fork CHANGELOG). Hex pins `REQUIRED_NPURUN` in `launcher.py`; `install.ps1` reads that literal by regex, so keep the line shape `REQUIRED_NPURUN = (0, 2, 3)`. Never pin past a fork release that does not exist. Hex releases carry no asset.
- Fork build: `cargo install --path crates/npurun-cli` inside the fork's dev shell (`scripts/dev-shell.ps1`; needs MSVC ARM64, LLVM on PATH for bindgen, `QNN_SDK_ROOT`). Known traps: GNU `link.exe` from Git Bash shadowing MSVC's; `ADSP_LIBRARY_PATH` unset.
- Steps: move CHANGELOG Unreleased under `## X.Y.Z — YYYY-MM-DD` → set `__version__` → commit `Release X.Y.Z` → tag → push both → CI green → `gh release create vX.Y.Z --title vX.Y.Z --notes-file <section>`. Title is the bare version.
- A release that exists to fix the previous one is a skipped gate; add the case that would have caught it.

---

## 9. UI and copy (HCI rules)

Settled after the user rejected AI-sounding copy twice (2026-08-29):
- Status lines are one past-tense sentence stating the state change ("Chat history cleared."). A next-step imperative only when the user must act.
- No em-dash asides, no parentheticals, no reassurance, no token or message counts in notices (numbers live in `/stats` and `/context`).
- Release titles are the bare version; release notes open with the first factual section, no thesis sentence. When unsure, copy what git or aider would print.
- The REPL: one mode, 18 commands (`/help /clear /new /history /resume /search /diff /undo /stats /context /compact /memory /config /setup /tools /cwd /doctor /exit`), Esc cancels, Tab completes, did-you-mean for typos so a mistyped command never reaches the model. Custom commands are `.md` files in `.shellai/commands/`. Chat/command modes and `/save /load /checkpoints /open /profile /model /models /mode` were removed in 2.3.0 (−685 lines); do not bring them back.
- Layout (2026-09-10): Claude Code shape. Transcript above; the last rows are the input box (`> `, a rule above and below) and the status line (`context`, `npu`, `mem`, cwd/branch right-aligned; the spinner label and `→ tool` while a turn runs). Nothing uses a scroll region: scrollback is preserved. The box is pinned to the window's last rows from the first prompt (`statusbar.console_geometry` reads the cursor row; blank rows are padded BETWEEN the transcript and the box so the banner stays at the top; `pad_for_editor` before each read, Ctrl+L and zoom included). Inserting the padding above the transcript with `ESC[nL` (text hugging the box, chat-window style) was tried on 2026-09-10 and rejected by the owner: the startup banner must stay at the top. The owner's final wish (2026-09-11): banner at the top, conversation ANCHORED ABOVE THE BOX growing upward (the question stays where it was typed, the answer appears under it, older lines shift up into the blank space; the banner scrolls off only once the space is gone). Mechanism in `LiveArea`: the pad is tracked as `_pad_top`/`_pad_above`; `write()` renders text through the margin, counts its newlines, deletes that many pad rows at the pad top with `ESC[M` (cursor restored with CUP), then writes to the base stream; `_draw`/`pad_for_editor` insert pad rows with `ESC[L` at the pad top (the cursor row when there is no pad) to pin the box. Pad invalidation (2026-09-11 review): `screen_cleared()` after any `cls`/`ESC[2J` (`/clear`, `/resume`, zoom), `note_scroll(n)` when the editor's multi-row entry scrolls the window (editor `on_grow` hook via `console_geometry`), `_insert_pad_rows` replaces a stale count, and a change of (height, usable) resets the pad before a draw; `_erase` writes `ESC[J` to the base stream, not through the margin. The editor writes to `live._inner` (or its base when side_padding is 0), bypassing the wrapper. Delegates never mark the turn stopped; the running tool is labelled `▸ tool` in the status line by run_autopilot, and `Spinner.__exit__` leaves such labels alone. A "fill from the top" variant (editor climbing over the pad) was tried the same day and rejected. A shrink therefore scrolls top rows into scrollback, as any bottom-pinned layout does. The user's message is echoed as `> text` on a light background band (`ui.user_row`, 256-colour bg 237, padded to the usable width; `user_highlight` config key; applied by the editor's `finish_style` hook and by `redraw_transcript`). `status_bar: false` or a non-console stdin gives the old inline `[model | cwd | gauge]` prompt. Chrome rows must be one cell narrower than the usable width or the editor pads a spare row. Any inline console prompt (y/N confirms, `/memory clear`, the restart question) must go through `ui.confirm_or_deny` / `ui.ask_line`: they lower the box (`statusbar.paused`) and echo through our streams; raw `input()` echoes through the console and leaves the box jumbled. Shift+Enter inserts a newline (seen via `PeekConsoleInput`, msvcrt alone reports plain Enter). Resize (settled 2026-09-12 in a real Windows Terminal window): the terminal re-wraps every row at the new width, and the ConPTY buffer has no scrollback, so rows that no longer fit are dropped off the top into Terminal's scrollback where nothing we write can reach them; narrowing therefore lost the banner for good while the blank pad stayed. Any in-place repair is hopeless, so a resize lays the whole screen out again through the same path a turn uses. At the prompt: the editor (on `WINDOW_BUFFER_SIZE_EVENT`, or a size change on the idle tick) climbs over its own re-wrapped rows, clears to end of screen and runs `on_resize` = repl `_relayout()` = `ui.clear_screen()`, `live.screen_cleared()`, `_reprint(live.enable)` (banner, box up with the pad under it, `ui.redraw_transcript(clear=False)` growing upward into the pad), `live.disable()`; zoom does the same. During a turn: `LiveArea.write()`/`repaint()` notice the geometry change and call `_relayout`, which clears, calls the owner's `on_relayout(pin)` hook (`_reprint`: banner, `pin()`, transcript plus `pending_query`, the running turn's question, which joins the session only at turn end) and then replays `_turn_log` (every raw write since `enable()`); the box must be DOWN while the banner prints (a write with the box up pins it under the cursor, so the pad would land between banner rows), `pin()` puts it back up. `redraw_transcript` spacing matches the live flow: one blank row between an answer and the next echo. Three rules learned from the 2026-09-12 live tour: (1) every console geometry read must be preceded by a flush of the base stream (cursor moves are escape sequences without a newline; a line-buffered stdout holds them and the console reports the row from before the move; the editor writes an empty chunk, which its writer flushes, before `_read_anchor`, and `LiveArea._geo` flushes) — the symptom was a multi-row question losing its first row after Enter once the screen was full; (2) a growing entry takes blank pad rows first (`make_room` deletes at the pad top, the banner stays), gives them back on a shrink (`give_room`, after the editor has cleared its old rows, or the insert pushes the cursor past the bottom where the console clamps it), and only then lets the window scroll (`on_grow`); a shrink past what the pad can return re-anchors the box on the last rows over blank rows; (3) the finished echo breaks at spaces (`_wrap_words_visible`, also used by `ui.user_echo`), while the row being edited breaks exactly at the width for the cursor arithmetic. The pyte harness cannot show any of this (its buffer keeps the rows): verify resizes in a real Terminal window with a console-buffer dump (`AttachConsole(pid)` + `ReadConsoleOutputCharacterW`) and type into it with `WriteConsoleInputW` on `CONIN$`, never SendKeys (the foreground lock sends keys to whatever window is in front). Persona walkthroughs (2026-09-12) added three more rules: off a terminal (stderr/stdout not a tty) nothing but the answer is printed (no spinner, step label, token counter, erase sequence, or blank lead-in), so `hex "..." | clip` is clean; `/undo` works exchange by exchange (`agent._SESSION_UNDO_STACK`, one entry per turn, `pop_undo_snapshots`), not just for the last turn; the help intent ("what can you do") prints the list but stores a one-line answer, because storing the help text cost half the 4K context. To check other rendering without a desktop: run the REPL under ConPTY and render with pyte (nulling the driver's std handles around CreateProcess, or the child inherits them instead of the pseudo-console's).
- Model-facing strings are exempt from the copy rules but require §7.
- Settled on 2026-09-11 after two audits: glyphs are `◆` tool card (cyan), `▸` result/event line (`▸ [tag] detail`, `▸ error …`, `▸ [run] exit N`), `⚠` warning, `✓ ✗` only in the launcher and doctor, `•` bullets, `…` ellipsis; nothing else (`⟶ ▶ ! [warn]` were removed). Notices are two-space indented and DIM; transcript content (echo, cards, output, answers) starts at the margin. One blank line before a card, none after; one blank before an answer (the first streamed byte writes it). Consent prompts: `⚠ The agent wants to …` / command at two spaces / `Allow? [y/N]` / `Allowed.` or `Denied.`. The word for a stopped action is `Cancelled.` everywhere. The launcher is silent when the server is already up; the REPL banner is the only banner.
- Terminal (2026-09-10): the Start Menu shortcut runs `wt.exe -p "Hex CLI"` when Windows Terminal is installed (`install.ps1` registers the profile as a fragment under `%LOCALAPPDATA%\Microsoft\Windows Terminal\Fragments\Hex CLI\`, skipped when the user already has a "Hex CLI" profile; `--uninstall` removes it); conhost is the fallback. On the owner's machine the profile is hand-made in settings.json and the default terminal delegation (`HKCU\Console\%%Startup`) points at Windows Terminal, so `Hex CLI.cmd` opens there too. QuickEdit is still turned off (2.5.1 freeze fix), which is why drag-select never works in conhost; `ui._PIE_OK` keys the gauge glyph on `WT_SESSION`.

---

## 10. Settled questions (do not re-propose without new external evidence)

Each has a measurement behind it; the evidence lives in `docs/V2_PLAN.md`
§14, `docs/RESEARCH_NEXT_LEVERS.md`, `docs/backend_study/`, and the paper.

| Idea | Verdict | Number |
|---|---|---|
| Protocol v2 as default | lost | 13/36 vs 22/35 pass^5 |
| Prompt trimming / conditional restraint rules | degrades | bait 5/8 → 3/18, p≈0.017 |
| Leaner continuation prompt for steps ≥ 2 | degrades | agentic-3 3/3 → 1/3 |
| Dropping the delegate schema | degrades | `{"` one-token replies 3/17 |
| Prompt length as a speed/energy lever | closed | stepped decode curve; identical per-call time at 2,236 vs 1,996 tok |
| Compaction/cliff threshold tuning | closed | quality flat 2,370 → 3,697 tokens |
| 8K recompiled bundle | rejected | 6 tok/s vs 15 |
| 8B escalation model | rejected | 0.9 tok/s |
| Qwen3.5-4B as a swap | blocked | GGUF only, no NPU bundle; 20–25 % slower |
| GenieX (qairt path) on this machine | halted | 0.74 tok/s, ~1.5 s per graphExecute; issue #1266 open |
| Native `<tool_call>` template | dead | detokenizer garbles the token |
| Tool consolidation 15 → 8 | wrong target | rules are 72 % of the prompt, schemas ~450 tok |
| LoRA / any fine-tuning | ruled out by the owner | |
| Rust rewrite | pointless | Python < 1 % of wall clock |
| Ollama as CPU backend | unusable | prefill ~30 tok/s; use upstream llama.cpp Q4_0 if a CPU path is ever needed |
| Second standby Genie dialog | impossible | err 1007 on 16 GB |
| Forced / tail-aware / interruptible prewarm | withdrawn | identical first token; the "20 % rebuild excess" was a harness artefact |
| Memory dreaming | off | wrote fabricated hardware facts into memory rules |
| HTP perf profiles below burst | rejected | ≤ 2 W saved at 25–40 % of speed |
| Raising HMX timeout to fix the long-context hang | lowers rate only | 1 in ~55, then a 740 s stall |

Things that WON and are the current baseline: the direct (no-tools) stage
(knowledge latency −40 %), Rewind runtime on QAIRT 2.50 (turns −41 %),
server budget 3,696 + empty-Rewind guard + prewarm (2.5.0), rule 9
machine-state cookbook (live-state 44 % → 72 %), rule 12 "the message must
BE the question" (ambiguous 1/3 → 5/5), polling off (2.6×
energy/token), async init off (hang 45 % → 4 % at 3K), merge-aware
compactor with dry-run gain guard (thrash fixed), per-step tool-output
budget + paged reads + empty-reply retry (overflow bug).

---

## 11. Open work and watch items

From `docs/V2X_ROADMAP.md` and `docs/RESEARCH_NEXT_LEVERS.md` §7:

- Append-only raw history across turns (every turn a prefix extension). Largest open lever; changes what the model reads, so full pass^5 + multiturn with think time.
- Compaction prompt sharing the warm prefix (less urgent at an 850-token budget).
- Harness-side auto-paging for `bigfile-2`. `"Update the file."` still 0/5 on the ask-don't-give-up case.
- `missing-file-2` (edit a file the user did not name when the named one is missing, with a prior edit in the history): 1-3/5. A rule-11 sentence fixed it (4/5) but broke `agentic-3` (gate FAIL, 2026-09-12) and cost 160 prompt tokens; the next attempt should be loop mechanics, not prose — the way the "run the tests" nudge (shipped 2026-09-12, `_tests_requested` in `run_autopilot`) replaced a rule-14 sentence that made the 4B copy the prompt's tool examples literally.
- Server should mark its cache stale after `finish_reason=length` (Rewind −1 otherwise). Fork-side.
- Capabilities, each flag-gated and A/B'd: plan ledger, memory v2 (files + ripgrep), git-snapshot undo, background commands / steering, AST command classifier + policy files.
- Optional dedent of the prompt (+13 % history room, no speed) via the 5-run gate.
- Watch: a successor NPU bundle appearing (run the instrument that afternoon); GenieX #1266 resolving; Nexa SDK bake-off; speculative decoding spike (hard time-box).
- Known platform issue, not ours: long-context (≥ ~2.9K) HTP `Error 1011` hangs; fork 0.2.3's watchdog caps the cost at ~97 s. Report to Qualcomm with that signature if it matters.

Current ceiling-panel failures (LATEST.md, 5 runs): `ambiguous-2`,
`bigfile-2`, `error-recovery-1`, `factual-5`, `trap-1/3/4` at 0/5. Trap
cases are the model's known ~1-in-3 bait compliance ceiling.

---

## 12. Documents and how current they are

| Doc | Use it for | Currency |
|---|---|---|
| `README.md` | user-facing install/usage/commands/config | current (2026-09-08) |
| `RELEASING.md` | numbering, fork pin, the gate | current |
| `CHANGELOG.md` | what shipped and the numbers | current; `Unreleased` = release re-org + prewarm negative result |
| `docs/V2X_ROADMAP.md` | phase status, watch items, rejected list | current (09-07) |
| `docs/RESEARCH_NEXT_LEVERS.md` | the levers memo, Rewind arc, runtime knobs | current through 09-05 |
| `docs/backend_study/CPU_VS_NPU.md`, `PROMPT_LEVER.md`, `RUNBOOK.md` | the measurement study and procedure | current (09-05/07) |
| `docs/V2_PLAN.md` | §14 evidence archive only | §1–13 are superseded intent |
| `ARCHITECTURE.md` | the reasoning behind rules and the safety layers | **stale** (2026-08-16): predates the Split, the prune and Rewind; says the template is in agent.py, no KV reuse, 22 suites/685 tests, 38 cases. Read §2 and §4 for rationale, not §1/§3/§5 for facts |
| `docs/paper/hexcli-paper.tex` | the methodology paper (v2.2.0 state) | hand-built with pdflatex; no script; lacks the backend study |
| `evals/results/LATEST.md` | scoreboard and the gate command | regenerate with `gate.py --scoreboard` |

`tools/`: `gen_example_config.py`, `chatlog_report.py` (`--last`,
`--session <id>`), `gen_icon.py`; `tools/backend_bench/` holds the study
harness (`bench.py`, `analyze.py`, `guard.py`, `radar.py`, `npu_ab.py`,
`smoke_gate.py`, `run_suite.py`) and the probes (`stall_rate.py`,
`rewind_probe.py`, `decode_vs_context.py`, `prompt_latency_probe.py`,
`prewarm_tail_probe.py`, `stall_hunt.py`, `battery_session.py`). All
stdlib; `bench.BASES["npu"]` lacks `/v1`, which once made prewarm calls 404
silently.

---

## 13. Session checklist

Start: `git status` and `git log --oneline -5` in the canonical clone; read
`CHANGELOG.md` Unreleased; check for another session on port 11435.
Working: edit → ruff → the touched suites → CHANGELOG. Model path touched →
fresh server → smoke → §7. Before "done": the numbers, not the intent.
Finish: leave the tree clean or say what is uncommitted, and update this
file if a fact here changed.
