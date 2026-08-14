# Changelog

Full evidence for every claim below — including the experiments that failed —
lives in `docs/V2_PLAN.md` §14. Numbers are pass^k over repeated live runs on
the Hexagon NPU, not single-run anecdotes.

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
