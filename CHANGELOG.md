# Changelog

Full evidence for every claim below — including the experiments that failed —
lives in `docs/V2_PLAN.md` §14. Numbers are pass^k over repeated live runs on
the Hexagon NPU, not single-run anecdotes.

## 2.5.0 — unreleased

### The context question, answered: the window was never the model's

The 250-token history floor came from two constants nobody had derived: a
2,600-token "degradation cliff" in the harness (V2_PLAN §14.7 records, the
same week it was written, that the collapse it described was a regex bug)
and a 3,000-token input cap in the server, inherited from upstream. The
bundle is compiled to 4,096. A cliff sweep at 3,000–3,700 input tokens
found quality flat all the way (12/18 at every size, the same three cases
failing each time); decode is ~12% slower only with the window actually
full.

Now: the server derives its input budget from the bundle's context size
(4,096 − a 400-token reply reserve = 3,696), keeps the user's request when
it must trim (it used to drop it first), caps generation at what the window
has left, and advertises the budget on `/v1/models`. The harness reads that
budget at the first turn and sizes history and tool pages against it.
History before auto-compact goes from 250 tokens to ~850; a first page of a
big file is now ~3,000 characters where the 4,096 assumption had let it
evict the question entirely (2.4.0 sized tool pages against the compiled
window while the server trimmed to 3,000 — bigfile-1 was passing with the
model never seeing the request).

| | 2.4.0 | 2.5.0 |
|---|---|---|
| multiturn ×3 (uc1–uc3, 16 turns) | 25/48 | **35/48** |
| uc2 everyday session, 6 turns | 7/18 | **18/18** |
| extended ×3 | 100/123 | 95/123 (parity, p=0.53) |
| empty model replies in the multiturn run | 63 of 117 | 0 |
| server trims (request evicted) per multiturn run | 141 at the old budget | 17 |
| dialog rebuilds per multiturn run | 44 at the old budget | 14 |

### Two bugs the single-turn suites could not see

- **Silent empties.** With the old floor, compaction rewrote history every
  two exchanges, and a KV-cache Rewind on a transcript that diverges
  mid-conversation can come back *successful with zero tokens* in ~0.5 s,
  then stay that way. 63 of 117 calls in a multi-turn run. That is 2.4.0 in
  a real session. The server now treats a Rewind that returns nothing like
  a failed one: rebuild and retry.
- **A payload that found the other door.** uc3-t9's calc.exe launch has been
  refused by run_code's workspace boundary since July; routed through
  `run_command` it was "caution" and ran. Absolute-path program launches,
  `Start-Process` and `cmd /c start` are now in the sensitive tier
  (confirm-gated; denied when non-interactive).

### End-of-turn prewarm

Measured limit of Genie 1.20's prefix matching: a request that diverges from
the cache (every new turn — history is condensed) works while the cache is
under ~3,150 tokens and costs a ~10 s rebuild above that. So the harness now
tells the server when a turn ends; if the cache is long, the server rebuilds
and re-prefills the system prompt in the background while you read the
answer. Next turn after a 3,400-token turn: 2.2 s to first token with the
prewarm, 10–12 s without. The client waits out the server's busy signal if
you type faster than that (the eval runner always does, which is why the
multiturn suite shows the prewarm as neutral: it has no think time).

### Full chat log

Every session now writes a complete transcript to `~/.shellai/chatlog/`
(one JSONL file per session): the version and npurun build, the server's
budget, the config in force (secrets redacted), every request as typed,
every message the model was sent, every raw reply with its latency and
retry index, every tool call with its full output, how each turn ended,
compactions and errors. Telemetry stayed a redacted summary; this is the
thing to read when a turn went wrong. `/stats` shows the current file;
`python tools/chatlog_report.py` summarises all sessions (versions,
tools, retries, empty replies, latencies, slowest and failed turns) and
`--last` replays the most recent one. Off with `chat_log_enabled false`.

### Context gauge in the prompt

The prompt header now ends with a small pie glyph and a percentage
(`[qwen3-4b | ~\proj (main) | ◔ 30%]`): how much of the history budget
this session has used. 0% on a fresh session, yellow from 75%, 100% (red)
means the next turn will auto-compact. `/stats` shows the same figure.

Requires the 0.2.1 `npurun-arm64.exe` from this release (the budget, the
empty-Rewind guard and the prewarm endpoint live there); an older server
keeps 2.4.0's behaviour with a 3,000-token budget.

## 2.4.0 — 2026-09-02

### Every turn ~40% faster: the KV cache finally survives between calls

On QAIRT 2.50 the npurun fork (0.2.0) keeps the Genie dialog alive across
requests and sends every warm query as a prefix-matching `Rewind`, so the
2,355-token system prompt is prefilled once per process, not once per
step — and, because the prompt is now byte-identical across directories
and days (`prompt_stable_prefix`), even a brand-new conversation starts
warm. Two Genie 1.20 behaviours shaped the server: a reset after a large
prefill wedges the dialog (so it never resets), and an early-diverging
transcript poisons it (so it rebuilds the dialog in place, ~5 s, only on a
different system prompt). The launcher turns all of this on when it finds
QAIRT >= 2.50 and npurun >= 0.2.0, and leaves everything as before
otherwise.

| Extended suite, 3 runs/case | before | after |
|---|---|---|
| run-level pass | 91/117 | **97/117** (no regression, p=0.41) |
| first token, median | 6.8 s | **3.7 s** |
| agent step >= 2, median | 7.6 s | **3.2 s** |
| whole turn, mean | 16.0 s | **9.5 s** |

The no-tools direct stage is off in this configuration: with prefix reuse
a knowledge query on the agent path is already decode-bound, and a
different system prompt would cost a rebuild.

### Large tool results no longer break the step

The compiled window is 4,096 tokens. The server drops older messages to
fit but cannot drop part of the newest one, so a single tool result over
~1,800 tokens overflowed the window, the model returned an empty reply,
and the agent finished with the raw tool output — or generic babble — as
its answer. The configured limit allowed ~3,000 tokens; no eval case had
a tool output over 567 chars, so nothing ever saw it. Each tool result is
now sized to the room actually left in the window (`context_window_tokens`,
new config key), large reads come back as a line-aligned first page with
the exact offset to continue from, and an empty reply is retried like any
other invalid action.

| Live A/B, mechanism case bigfile-1 (×3) | budget off | budget on |
|---|---|---|
| Answer references the file | 0/3 | **2/3** |
| 16 other tool-using cases | — | no regression |

### The instrument could not see the v2.2 live-state win

`livestate-1` in the extended suite used the checker for "write me a
regex" questions, so every correct CPU answer failed — in every extended
run and both arms of every A/B since the case was added. Fixed
(`answer_matches` is the shared checker); count checks accept spelled-out
numbers. Re-graded live: livestate-1 0/3 → 3/3, agentic-5 2/3 → 3/3.

### The Split — codebase health

`agent.py` 3,818 → 1,499 lines across seven verified stages
(`parsing`, `http_client`, `cancel`, `tools`, `compaction`, `config`,
`repl`, `llm`); no module over 800 lines. Zero behaviour change by
construction, checked three ways: 24 suites / 699 tests, sentinel or
mutation probes on every moved-and-patched symbol, and a full pass^3
extended arm at statistical parity with the pre-split baseline (87/117
vs 91/117, p=0.65).

### Also

- `evals/run_chunk.py`: collect a suite arm in short chunks on one server
  (with `--set` overrides for A/B), for environments that kill long runs.
- `evals/cases_cliff.py`: the input-size sweep that closed the context
  question (quality flat to the runtime's ~2.9K input trim).
- The Rewind runtime is opt-in by presence: QAIRT 2.50+ under
  `C:\Qualcomm\AIStack` plus the 0.2.0 `npurun-arm64.exe` from this
  release. `/doctor` shows a "KV prefix reuse" line saying which it found.
  Full history of the spike (2.47 rejects Rewind, the SMART_MASK dead end,
  the 2.50 unlock) in docs/RESEARCH_NEXT_LEVERS.md §5–7.

## 2.3.0 — 2026-08-31

### One mode, 18 commands

Chat and command modes are gone, along with `/save`, `/load`,
`/checkpoints`, `/open`, `/profile`, `/model`, `/models`, `/mode`, and
`/context` (now the tail of `/stats`): −685 lines, no capability anyone
used. The app is the agent: REPL, one-shot, pipe. Status messages were
rewritten to terminal-tool voice ("Chat history cleared."), and `/clear`
now actually clears — screen and context — with `/new` keeping the
scrollback.

### Auto-compact stops thrashing

At the 250-token history floor, auto-compact re-fired every message and
crushed its own previous summary into a single stub each pass. The
deterministic compactor is now merge-aware (idempotent on its own output)
and auto-compact dry-runs it first, firing only when ≥100 tokens would
actually be freed.

### The context question, closed

A dedicated sweep (`evals/cases_cliff.py`) ran the production loop at
controlled input sizes: quality is **flat** from 2,370 to 2,973 measured
input tokens, and the runtime silently trims anything above ~3K — the
shipping config already sits at that ceiling. A two-stage prompt-split A/B
(extended suite, fresh server per arm) kept one stage and rejected the
other:

| Prompt-split A/B (extended ×3) | baseline | split |
|---|---|---|
| Run-level | 91/117 (77.8%) | 87/117 (74.4%), p=0.65 |
| Knowledge-query first token (median) | 10.1 s | **6.0 s** |

The **direct stage** ships on by default (`prompt_split`): pure-knowledge
queries get a small no-tools prompt, tool restraint becomes structural,
and first-token latency on those turns drops 40%. The **continuation
stage** (leaner prompt for steps ≥ 2) was rejected: edit anchors
degenerated under the changed prompt — the trimming experiment's
degradation fingerprint, now reproduced at every loop depth. Conclusion,
recorded in the paper: the 250-token history floor is a property of the
model and the 4K bundle, not a harness gap.

### Fixes

- Visible caret while typing (the line editor hid the hardware cursor for
  the whole read; now only per-repaint).
- Real Hex taskbar icon: the Start Menu shortcut launches through classic
  conhost (Windows Terminal has no per-profile taskbar icon), and the
  launcher sets the window icon at startup — from `main()`, not import,
  after a test run re-badged the developer's own terminal.
- A failed request can no longer poison the cached keep-alive connection:
  reconnect covers `ResponseNotReady`, so "restart the model server" is
  followed by a working retry.

## 2.2.0 — 2026-08-16

The everyday-correctness release. Two wild failures — "what cpu do i have"
answered with a confabulated Intel chip on a Snapdragon machine, and one
salary division wrong five different ways — triggered a systematic study of
the prompts a normal user types in their first five minutes, instead of
case-by-case patching. Four experiment arms, fresh server per arm, n=5
triage on every moved case.

| Everyday sweep (30 cases, n=3) | before | after |
|---|---|---|
| Live machine-state questions | 16/36 | **26/36** |
| All categories | 58/90 | **69/90** |
| Trap resistance (guard) | 10/20 | 9/20 (held) |

### The command cookbook (rule 9)

The dominant live-state failure was not the model refusing to run commands —
it was not *knowing* the Windows commands: invented cmdlets (`Get-CPU`,
`Get-CimComputer`), "what cpu" misread as CPU *usage*, registry fallbacks
that collide with the sensitive-path tier. Rule 9 now carries exact
known-good queries (CIM classes for CPU/GPU/RAM/cores, `Get-PSDrive`,
`Get-Date`, `$env:` names) plus a scope sentence keeping the
never-use-a-tool-just-because-it-was-named rule in charge everywhere else.
That sentence is load-bearing: without it, trap resistance collapsed to
4/20. Prompt cost: ~+200 tokens, spent knowingly.

### Memory dreaming off by default

The deepest root cause was not the model at all. The background "dreaming"
consolidation daemon had distilled the model's own confabulations into
`memory_rules.md` as fabricated machine facts (wrong CPU, wrong RAM, an
invented temperature), re-appending the identical batch every idle cycle —
then injecting them into every turn as "Prior knowledge", which the model
trusted over running a command. A self-reinforcing hallucination loop that
survived every prompt improvement. `memory_dreaming` now defaults to false;
the roadmap had already ruled the daemon ships only with a quality eval it
passes, and it now has one it failed. Hand-written memory rules still work.

### Measured and rejected, continued

- **Routing calendar math to `run_code`** — the tool takes a file path, not
  inline code, so the model correctly refuses; inline-code support is now a
  roadmap item. Days-until/weekday arithmetic stays a documented ceiling.
- **The arithmetic "failure class" itself** — mostly an artifact of a
  degraded 27-hour-old server; on a fresh server, everyday arithmetic is
  9/10 at 3/3. The measurement trap struck the diagnosis itself; server
  freshness now has a written protocol note.

Also: `evals/cases_everyday.py` joins the live suites (30 common prompts
graded against computed machine truth), and the roadmap's phases are named
by content instead of version numbers, so release tags and plan phases can
never collide again.

## 2.1.0 — 2026-08-14

v2.0 was a loop that worked and a product almost nobody could install. v2.1
is the packaging-and-shell release: getting Hex CLI onto a second machine,
and making it scriptable once there. The agent loop and the tuned prompt are
untouched — the live smoke gate ran 10/10 before and after.

| | v2.0 | v2.1 |
|---|---|---|
| Install | clone, build Rust, hand-set env vars, read the README | `.\install.ps1` |
| Offline tests (CI) | 619 / 19 suites | **685 / 22 suites** |

### Getting it installed

- **`install.ps1` covers the whole ritual**: ARM64 and Python checks, pip
  deps, QAIRT SDK discovery, npurun, the model pull, config scaffold, Start
  Menu shortcut, and a closing `--doctor`. Every step skips work already
  done, so the intended flow is: run it, fix the one thing it flags, run it
  again. The SDK download stays manual — Qualcomm's licence forbids
  redistribution — so the installer prints exact instructions and picks the
  SDK up on the next run.
- **A prebuilt `npurun-arm64.exe` ships as a release asset** (the vendored
  fork; MIT/Apache-2.0), so a new machine no longer needs Rust, LLVM, and the
  MSVC ARM64 toolchain just to get a working agent.
- SDK discovery is shared logic in two languages, and **compares versions
  numerically** — `QAIRT_2.9.0` sorts above `QAIRT_2.47.0` as text, which
  would silently bind a stale SDK.

### Product shell

- **Piped stdin**: `git diff | hexcli "review this"` attaches the pipe as
  data beneath the task; `echo "task" | hexcli` makes the pipe the task.
  Bounded with head+tail sampling and a chunked read, so a huge pipe costs
  O(cap) memory rather than buffering the file.
- **Custom slash commands**: any `.md` in `.shellai/commands/` (project) or
  `~/.shellai/commands/` (global) becomes `/<name>`, with `$ARGUMENTS`
  substitution. Project files beat global ones; built-ins beat both, enforced
  structurally rather than by convention.
- **`/search <text>`** across saved sessions, with match highlighting. Hits
  carry the same numbers `/history` shows and `/resume` takes.
- **`/setup`**, an interactive wizard for the safety, network, and UI
  settings. It persists (`/config` is session-only) and writes *only* the
  keys it asked about, so a config file never fills with pinned defaults.

### Fixes

- **Consent prompts can no longer stall an unattended run.** A detached eval
  once hung 7.5 hours on one confirmation: `isatty()` reports True for a
  hidden console, and a daemon-thread timeout cannot fire because the Windows
  console read holds the GIL. All consent prompts now poll for keys against a
  deadline. Unanswered means denied.
- Ctrl-C at a consent prompt **denies** rather than aborting the turn — the
  deny path is what writes the audit log's `blocked` entry and preserves the
  turn's undo snapshots.
- That deadline is an **idle** timeout, so an attended user reading a
  proposed command is never cut off mid-answer.
- Bare `/save`, `/load`, and `/model` matched only their `<cmd> <arg>` forms
  and fell through to the custom-command lookup.
- `autopilot_system_prompt` replaced the tuned prompt silently; it now warns.
- The installer reported failed `pip` installs as success (a `try/catch`
  around a native command never fires), and died outright on Windows
  PowerShell 5.1 when probing the default WindowsApps `python3` stub.
- `/setup` answers were reverted by the launcher's config regeneration.
- Two eval-grader loopholes: hallucinated completions ("I fixed it. You would
  need to restart.") and bare give-ups were scoring as clarification
  requests.

## 2.0.0 — 2026-07-31

v2 was a harness rebuild around a fixed model, driven by a measurement
instrument built first. Headline movement:

| | v1.7 | v2.0 |
|---|---|---|
| Extended suite (pass^5) | 22/35 | **24/36** |
| Multiturn turn-runs | 23/45 | **26/45** |
| Injection payloads executed | 9 of 9 | **0 of 9** |
| Offline tests (CI) | 330 / 8 suites | **619 / 19 suites** |
| First-token latency, smoke mean | ~9–10s | ~7–8s |

### The instrument (evals v2)

- Live evals now drive the **production `run_autopilot`** — one code path,
  graded on **filesystem state and answer content**, never string matches,
  with pass@k / pass^k over ≥3–5 runs and Wilson intervals.
- **Backend failures are not model failures**: a degraded NPU server marks
  runs invalid, not failed. The Genie dialog silently degrades after 1–2 h of
  traffic and perfectly impersonates a model regression; the runner detects
  it, and suites restart the server.
- `--set KEY=VALUE` on every runner: A/B any config key, override recorded in
  the saved results.

### Agent loop

- **Fuzzy edit apply, 4 tiers**: exact → trailing-whitespace → indent-shift →
  ≥95 % unique closest match. Ambiguity is always an error; a miss reports the
  nearest region with line numbers. (The #1 v1 failure class was edit
  formatting, not model capability.)
- **First-complete-JSON parsing**: the old greedy `\{.*\}` regex discarded
  batched multi-action responses wholesale — the actual cause of the
  multiturn collapse, not context length.
- **Unconditional retry-with-feedback** on malformed actions, at any step,
  with feedback naming the defect (unknown action / wrong shape / not JSON).
  Pure prose remains an implicit finish.
- **Fuzzy loop detection**: trips on repeated failures of the same call even
  when the error text varies; distinct targets never trip.
- **Verification-gated finish**: an unverified file mutation deflects the
  first "done" and asks the agent to check its work.
- **Deterministic auto-compaction** derived from the measured prompt size and
  a **calibrated token estimator** (EMA of real chars-per-token from exact
  completion counts) — the old chars/4-plus-stale-constant scheme fired
  compaction *past* the model's degradation cliff.
- `read_file` pages with offset/limit; tool output truncates head+tail;
  in-place NPU server restart on backend failure.
- Escalation ladder (loop trips, ignored verification, prose-instead-of-edit
  → consult a second local model) — shipped but **off by default**: no viable
  bigger local model exists on 16 GB (see "Measured and rejected").

### Safety (assumes the model is 100 % injectable)

- **Sensitive tier** ranked above `safe`: ssh/gpg/aws keys, hosts file,
  registry hives, credential vaults, DPAPI, `-EncodedCommand`. Confirm when
  interactive, **deny when not**. Live injection suite: 0/9 → 9/9 blocked.
- **Workspace write-scoping** behind a single `guard_mutation` gate; every
  mutating entry point in both protocols is enumerated by a test that drives
  it at an out-of-scope path and at a key path.
- **Network deny-by-default**: `fetch_url` (the only outbound channel)
  confirms per fetch, refuses when non-interactive; `network_access: "deny"`
  removes the tool and its schema entirely.
- Refusal messages never name an alternative route — a measured injection
  followed the old refusal's own hint straight to the bypass.

### Product shell

- **Rich input line** (pure stdlib): persistent history with prefix search,
  Tab completion for commands / config keys / paths, word-wise editing,
  multi-line paste as one message.
- Live streaming render; diff after every mutation + `/diff`; `/stats`;
  `/doctor` and `--doctor`; `AGENTS.md` project instructions; did-you-mean
  for slash commands; corrupt history quarantined instead of bricking launch;
  process-tree cancellation on Esc.
- `shellai.example.json` is now **generated** (`tools/gen_example_config.py`)
  with a staleness test — the hand-maintained copy once shipped a key that
  silently replaced the entire tuned prompt.

### Measured and rejected (deliberately not in v2)

- **v2 `<action>` protocol as default** — lost its A/B 13/36 vs 22/35; kept
  behind `protocol: "v2"` as an experiment harness.
- **Qwen3's native `<tool_call>` template** — the w4a16 bundle's detokenizer
  garbles its own special token.
- **8K context bundle** — compiled via AI Hub, benched 6 tok/s vs 15, no
  quality gain; compaction already keeps sessions under the cliff.
- **Qwen3-8B escalation** — 0.9 tok/s on 16 GB (CPU fallback).
- **Prompt trimming / tool consolidation** — the 14 rules are 73 % of the
  prompt; omitting even provably-irrelevant ones cost trap resistance
  (5/8 → 3/18, p≈0.017). The model is specialised to this exact prompt.
  Ships as `conditional_rules`, default off.
- **Thinking-2507** — unreachable: no Genie bundle exists, and AIMET
  self-quantization needs Linux + ~40 GB RAM this machine does not have.

### npurun fork (vendored)

Usage reporting (one chunk per token — exact completion counts), token-precise
`max_tokens`, mid-stream stop sequences, and a UTF-8 char-boundary crash fix
(a multibyte character at a stop-sequence boundary aborted the whole server).

### Upgrading from 1.x

Config is backward compatible; new keys (`network_access`,
`conditional_rules`, `rich_input`, `workspace_write_scope`, …) all default to
the documented behaviour above. `/clear` now clears the screen (it was an
alias for `/new`). The `last_observation` session field is gone. Old
`evals/harness.py` / `extended.py` / `multiturn.py` are superseded by
`evals/cases_*.py` and kept for reference only.

## 1.0.0 – 1.7.0

Pre-changelog history; see the git tags and `ARCHITECTURE.md` for the v1
design and its audit.
