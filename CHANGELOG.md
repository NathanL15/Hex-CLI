# Changelog

Full evidence for every claim below — including the experiments that failed —
lives in `docs/V2_PLAN.md` §14. Numbers are pass^k over repeated live runs on
the Hexagon NPU, not single-run anecdotes.

## Unreleased

### Input box and status line

The REPL has the Claude Code layout: the transcript scrolls above, and the
last rows of the window are an input box with a status line under it:

```
──────────────────────────────────────────────────────────────────
> what is 2+2
──────────────────────────────────────────────────────────────────
⠹ thinking (Esc to cancel)   context ◔ 32%   npu 96%   mem 10.1/15.6 GB
```

New module `hexcli/statusbar.py`; `lineedit.py` gained chrome rows and an
idle repaint; `ui.py`, `repl.py`, `llm.py` and `agent.py` wire it in.

* **How it stays at the bottom.** No scroll region and no full-screen
  redraw, so scrollback is untouched. While the line editor is active it
  draws the box as its own chrome and repaints on a one-second idle tick
  when the text changes. Between reads a wrapper on stdout/stderr erases
  the box before every transcript write and redraws it after, borrowing the
  margin layer's column bookkeeping to put the cursor back; the spinner's
  ticks go through the same lock. The box is pinned to the window's last
  rows from the first prompt: the console's cursor position
  (`GetConsoleScreenBufferInfo`) says how many rows lie below the
  transcript, and that many blank rows are inserted at the top of the
  window until the conversation is long enough to reach the box on its
  own. Ctrl+L and a zoom redraw keep it there.
* **Metrics.** `npu` is Windows' own NPU counter: `GPU Engine` utilisation
  for the adapter DirectX does not list, the source Task Manager's NPU
  graph reads, through `pdh.dll` with ctypes. Measured on this machine:
  0 % idle, 90–97 % during decode, attributed to npurun's pid; the adapter
  LUID changes across boots, so it is discovered at start-up. `mem` is
  physical memory in use system-wide (`GlobalMemoryStatusEx`). Both are
  sampled once a second on a daemon thread that never writes to the
  terminal. `context` is the existing fill gauge; the working directory
  and branch sit at the right edge when they fit.
* **The spinner moved into the status line**, as did the tool announcement
  (`→ read_file`). The streaming path never had a spinner because it would
  have fought the streamed text for the row; with the bar up it gets one,
  so the step label shows before the first token arrives, and the
  transcript's `thinking...` line is dropped. The prompt is `> `; the
  `[model | cwd | gauge]` header line is gone, the bar carries both.
* **No more answer printed twice.** Every turn ended with a `── Result`
  box repeating the text that had just streamed. The box is now skipped
  when the final model call streamed exactly the message the turn returned
  (whitespace aside). It still appears when they differ: nothing streamed
  (mock backend, no tty, a dropped stream retried without streaming), the
  turn ended on a tool result or a harness message instead of a model
  message (`Done.`, step limit, loop stop, refusal), or the parser
  recovered a message from output the renderer could not follow.
* The `(~N tokens generated)` line after every answer is gone; the count
  lives in `/stats`. Hitting the step limit now says so without a number.
* Off a console (pipes, CI, `--raw`) or with `status_bar: false` the old
  inline prompt is used unchanged. Checked under a pseudo-console (ConPTY
  plus a VT emulator, scratch tooling, not committed): plain turn, tool
  turn, `/help`, paste, Esc. 14 offline tests in `evals/test_statusbar.py`
  and 3 in `test_lineedit.py`.

### Review pass over the day's changes

Two code reviews over the diff and a live edge-case pass, fixed together:

* A delegate sub-agent hitting its own five-step cap printed `⚠ Stopped`
  mid-turn and suppressed the turn's answer box; delegates no longer mark
  the turn. Each delegate call printed two `◆ delegate` cards; one now.
* `▸ [run] exit N` was glued onto command output that lacked a trailing
  newline. The status label now names the running tool while it runs, and
  the elapsed clock counts the whole turn rather than restarting each
  model call.
* Pad bookkeeping: a multi-row entry that scrolled the window, `/clear`,
  `/resume`, Ctrl+L and a window resize during a turn all left the pad's
  recorded position stale, so later line deletes could remove conversation
  rows instead of blank ones. The editor now reports how far the window
  scrolled, screen clears reset the pad, a stale pad is replaced rather
  than added to, and a change in window size resets it before the next
  draw. The erase sequence goes straight to the console so the margin
  layer never replays it inside a reflowed word.
* An input of exactly the usable width left a styled empty row under the
  echo. Wide characters (CJK) are measured in cells in the status line,
  the chrome and the editor's wrapping.
* Markdown: a closing fence with trailing spaces or CRLF now closes; bold
  and code spans continue over a soft line break and end at a blank line.
  A `**` followed by a space no longer opens bold (`next** x` is literal),
  so a stray marker cannot bold the rest of an answer.
* The editor's scroll report counted only the first row a growing entry
  pushed past the bottom; every later row is reported too, and the status
  bar redraws on a window-size change even when its text is unchanged.
* Resizing the window in Windows Terminal lost the banner: the terminal
  re-wraps every row at the new width, and the rows that no longer fit
  are dropped into its scrollback, out of the program's reach. A resize
  (or zoom) at the prompt now lays the screen out again: banner at the
  top, the conversation anchored above the box, blank rows between, with
  the banner scrolling off only when the conversation needs the room.
  Verified in a real Windows Terminal window at five sizes.
* The same during a turn: a resize while the model is thinking or the
  answer is streaming lays the screen out again and replays the turn's
  output so far (the question, tool cards, the partial answer), then
  streaming carries on at the new width. Verified live in Windows
  Terminal, resizing once before the first token and once mid-answer.

### Live tour of the whole flow (2026-09-12)

Every command and key driven in a real Windows Terminal window, with the
console buffer dumped after each step. Fixed what it turned up:

* A multi-row question (Shift+Enter, or one line wrapping) lost its first
  row after Enter once the screen was full. The box's cursor moves carry
  no newline, so a line-buffered stdout still held them when the editor
  asked the console where the cursor was; its anchor sat too high, the
  scroll it reported fell short, and a later pad delete took the echo's
  first row. Every geometry read now flushes first.
* A long question's echo broke mid-word ("a sem / aphore"); it now breaks
  at spaces, and the transcript redraw echoes it the same way.
* Clearing or shortening an entry that had grown left the box a row or
  two above the bottom with blank rows under it. The box drops back to
  the last rows.
* A growing entry scrolled the whole window, banner included, even while
  blank rows still sat under the banner. It now takes those rows first
  (the banner stays put) and gives them back when the entry shrinks; the
  window scrolls only once the rows are gone.

Seen and left alone: on a nonsense or one-word question the model
sometimes echoes its own prompt scaffolding ("Request: …", the workspace
line). That is the prompt's shape, which changes only through the A/B
gate, not the terminal.

### "Run the tests" is now enforced by the loop, not the prompt (2026-09-12)

Two model behaviours from the live tour were turned into eval cases and
taken through the A/B gate (`evals/results/persona/` has every log).

* `tests-claim-1`: "fix the median and run the tests". Baseline 0/5, the
  test file never executed, the answer said it would pass. A rule-14
  sentence made it worse in a new way: the 4B copied the prompt's own
  tool examples ("Get-Process | Sort CPU", "script.py") — 1/5. Replaced
  by a harness nudge: when the request asks to run tests and no run tool
  executed a test this turn, the finish is sent back once, naming the
  test file. With it the tests ran in 5/5 attempts (pass 1/5: the
  remaining misses are wrong fixes, reported honestly with the exit
  code). Gate on the nudge-only arm: PASS (self-correct-1 5/6 on
  re-check); 30/44 pass^k, 167/221 run-level; scoreboard updated.
* `missing-file-1/2`: "in missing.py change alpha to beta" when the file
  does not exist. Alone: 5/5 already. With a prior edit in the history
  (`missing-file-2`, the live shape): 1/5 — the model edits another file
  and reports success. A rule-11 sentence lifted it to 4/5 but broke
  agentic-3 (5/5 → 3/5, 4/6 on re-check): gate FAIL, reverted. The loop
  now guards it instead: when the request asks to change a file it
  names, that file is not there, and the model reaches for a different
  file (or creates the named one without being asked to), the call is
  refused with the reason and the directory listing, and the model is
  told to say the file was not found. missing-file-2 3/5 → 5/5. The
  first cut of the guard read "correct" out of "saved correctly" and
  refused agentic-1's create (0/5); whole-word intent matching and a
  create-intent exemption fixed that (5/5), with agentic-1's wording
  pinned in the guard's tests.
* A second loop step for the same request shape: when the tests ran and
  failed and the model finishes anyway, the failing output is sent back
  once with "fix, run again, report". tests-claim-1 1/5 → 3/5; the
  remaining misses are fixes the 4B cannot get right in two tries.
  Gate on the guard arm (with this nudge, which no gated case can
  trigger): 30/44 pass^k, run-level 176/220 vs 167/221 on the nudge arm.

### Walkthroughs by kind of user (2026-09-12)

The same live method, this time as the people who meet the tool: someone
scripting it, a developer in a repo, someone whose server died, someone
coming back to old sessions, a first-time reader of the docs.

* Scripting: a pipe or `hex "..." > file` got the spinner, the step
  label, the token counter and erase sequences on stderr, and the answer
  wrapped in blank lines on stdout. Off a terminal, nothing but the
  answer is printed.
* Developer: `/undo` restored files for the last turn only; the second
  `/undo` removed the exchange but left its file change. Every turn now
  keeps its own snapshots, so exchanges undo one by one. A tool error's
  first line no longer ends on a bare colon.
* Server died: the in-session restart started the server with a 2.47
  SDK default when the launcher had chosen 2.50; the launcher now hands
  the REPL the server's environment, and the fallback picks the newest
  install.
* Returning: "what can you do?" stored the whole help text as an answer
  and cost half the context; it now stores one line. Session titles
  keep the person's words ("What is 2+2", not "What Is 22").
* Docs: README's opening transcript shows today's screen, every
  command-line flag has a help line, and the scripting behaviour is
  written down.
* Installer, run in a fresh clone: it warned "architecture X64" from an
  x64 shell on an ARM64 machine (it checked the shell's architecture, not
  the machine's); it reported a 2.47 SDK from an old `QNN_SDK_ROOT` while
  the launcher uses 2.50 (same newest-wins rule now, with a note); it
  never fetched the MiniLM embedding model, so memory was silently off
  after a fresh install (downloaded now, ~23 MB); its summary said
  "Start Hex CLI from the Start Menu" with `-NoStartMenu`. The doctor
  checked the example config's port 8000 instead of the launcher's
  server, and passed an `ADSP_LIBRARY_PATH` that pointed at another
  SDK's libs; both fixed.
* Ctrl+C at `/memory clear` or in `/setup` printed `Cancelled.` twice;
  `/setup` over a pipe works again. The non-streaming spinner and the
  delegate token counter no longer print alongside the status line.
* The launcher's failure exits held a classic-console window open with a
  prompt so the message can be read; Windows Terminal keeps the pane.
* Uninstall removes the emptied Terminal fragment folder; one-shot mode
  skips the answer box after a stop like the REPL does; `/history` fits the
  window width; the banner tail shortens in a narrow window; the notice
  after `/clear` lines up with the others.

### The shortcut starts in the home folder

It used to start inside the Hex CLI checkout, so every session's first
message carried the repo's own `AGENTS.md` as project instructions, and
the model folded those working rules into unrelated answers ("what is an
NPU" came back with "the NPU server must be restarted fresh for each
evaluation" and the `REQUIRED_NPURUN` pin). The shortcut and the Terminal
profile now start in the user's home folder, like a shell; `/cwd <path>`
or launching from a project directory picks up that project's
instructions. The prompt itself is unchanged.

### Your messages get a light band

The echoed `> question` rows carry a subtle background band across the
usable width, the way Claude Code marks the user's turns, so turn
boundaries are easy to find while scanning. Continuation rows of a
multi-line message are banded too, and the transcript redraw after a
zoom or `/resume` uses the same style. `user_highlight: false` turns it
off; it is never applied without colour or outside the boxed layout.

### The conversation grows upward from the box

The conversation is anchored just above the input box, like a chat window:
your question stays where you typed it, the answer appears under it, and
every new line shifts what is above it up into the empty space under the
banner. The banner keeps the top of the window until that space is used
up, then scrolls away. Mechanically the blank rows that pin the box are
now consumed from their top with line deletes (`ESC[M`) as text arrives,
and inserted at the cursor row (`ESC[L`) when the box must be pinned
lower, so nothing above the pad ever moves. (An intermediate version
printed the question at the top under the banner; the owner wanted the
seamless bottom-anchored flow instead.) The editor writes its own rows
straight to the console so its cursor moves never count as transcript.

### A compact window

The shortcut opens Windows Terminal at 92 columns by 28 rows
(`wt.exe --size 92,28 -p "Hex CLI"`) instead of the default 120 by 30, and
the installer's profile fragment sets the look: One Half Dark, Cascadia
Mono 11, even padding, a bar cursor, slight transparency, and a title that
the shell cannot overwrite.

### Copy, glyphs and flow, end to end

Two audits over every string a person sees (about 120 findings) and the
full interaction flow, then one pass to fix them. Model-facing text is
untouched.

* **One banner.** The launcher used to print its own two-section header and
  name the model and engine four times before the REPL's banner. It is now
  quiet when the server is already up, shows a spinner only while starting
  it, and stops with the log path on failure instead of falling through to
  the unmaintained DirectML and Ollama tiers. The REPL banner reads
  `qwen3-4b-instruct-2507 on the Hexagon NPU · /help · press Esc to cancel`; the
  old `backend: openai` told NPU users they were on OpenAI.
* **`/help` regrouped** into Session, Status, Setup and Keys, wrapped to 76
  columns, Shift+Enter and Ctrl+L added, the DirectML/Ollama "NPU note"
  gone. `/tools` shortened to one line per tool.
* **One glyph set.** `◆` tool card (cyan, no more magenta), `▸` result line,
  `⚠` warning, `✓ ✗` only in the launcher and doctor. The delegate's `⟶`,
  the history list's `▶`, the launcher's `!` and the `[warn]` tag are gone.
  `$` command echo is dim with the command bold; command output is dim and
  ends with `▸ [run] exit N`, so evidence no longer looks like the answer.
* **Rhythm.** One blank line before a tool card, none after; one blank line
  before every answer (the first streamed byte adds it); the answer box
  lost its `── Result` title so streamed and boxed answers look alike; a
  turn that stopped on a loop or the step limit shows one `⚠ Stopped: …`
  line and no box of raw tool output; tool failures are one dim red
  `▸ error` line under the card instead of a red frame.
* **Confirms.** `⚠ The agent wants to …`, the command indented two spaces,
  `Allow? [y/N]`, then `Allowed.` or `Denied.` so the transcript records
  the outcome. The all-caps "unless YOU asked" aside is gone.
* **Notices** are two-space indented, dim, one past-tense sentence:
  `Chat history cleared.`, `Last exchange removed.`, `Memory cleared.`,
  `Cancelled.` everywhere (was also `Aborted.`), `Unknown command /hlep.
  Did you mean /help?`. The backend-failure flow reads `Model server error:
  HTTP 500.` then `Restart the model server? [Y/n]` and, after a restart,
  `Press Up, then Enter to resend.`; `python launcher.py` advice that only
  works from the repo became `Relaunch Hex CLI`. The HTTP 404 case no
  longer suggests `ollama pull` on the NPU path. `/stats` ends with the same
  block as `/context` instead of a second format of the same numbers.
  `/resume` reprints the conversation it reopened.
* **Status line** labels are `thinking`, `responding`, `▸ tool` with the
  turn's elapsed seconds; the cancel hint is in the banner only, so the
  location stays at the right edge during a turn. The empty input shows a
  dim `ask, or / for commands` hint.
* **Doctor, setup, update, uninstall** copy brought in line: no em-dash
  asides, `All checks passed.`, `N failed, M warnings.`, `Saved …`,
  `Cancelled. Nothing written.`; the wizard now reads through the same
  polled prompt as the confirms, so the box is lowered for its questions.
  Default session title is `New session`.

### Answers render their markdown

Streamed answers printed their markup raw: `## Heading`, `**bold**`,
backtick spans and ``` fences with the backticks showing. New module
`hexcli/markdown_stream.py` turns that subset into terminal styling one
character at a time, so streaming keeps its word-by-word feel: headings
bold with the hashes dropped, `- ` / `* ` bullets as `•`, `**bold**` bold,
`` `code` `` cyan, and a fence as a dim rule carrying the language with the
code left exactly as written (no gutter, so it copies cleanly). Markers are
held only while ambiguous and released literally otherwise, so `2 * 3` and
`C#` survive. Feeding the same text whole or per character gives identical
output; `evals/test_markdown_stream.py` pins that. The non-streamed
`Result` box goes through the same renderer, so both paths match.

### Shift+Enter, and two prompts that bypassed the console handling

* `Shift+Enter` inserts a new line in the entry (the key reader already
  peeks console events, so the modifier is visible; `msvcrt` alone would
  hand it over as Enter). `\` then Enter still works.
* `/memory clear` and the "restart the model server?" question used raw
  `input()`, whose echo goes through the console itself: the status box
  was left jumbled after Enter and the margin's column went stale. Both
  now use `ui.ask_line`, the same polled read the confirms use, with the
  box lowered for the question. A no-human answer (not a console, Ctrl-C,
  idle timeout) never restarts the server.
* The status box is up before the banner, so the first thing on screen is
  the finished layout.

### Status-bar flow polish

Six interaction issues found by driving the whole flow under a pseudo-console:

* **Resize no longer corrupts the box.** There was no resize handler, so
  changing the window size left the rules at mismatched widths and one row
  without its margin. A console window-size event (or a size change caught
  on the idle tick) now clears only the box's own rows, computed from how
  the terminal re-wrapped them at the new width, and redraws; the
  transcript above is left exactly as the terminal reflowed it. (A first
  version reprinted the saved chat instead, which made the banner and every
  notice vanish on resize.)
* A version that inserted the padding above the transcript, so the text
  hugged the box like a chat window, was tried and reverted the same day:
  the banner belongs at the top. The blank rows sit between the transcript
  and the box, and a burst of resize events from a window drag is handled
  once, at the final size.
* **One prompt during a confirmation.** A `y/N` confirm used to render on
  top of the still-live input box, so two prompts showed at once and it was
  unclear where to type. The box is taken down for the duration of any
  `confirm_*` read and restored after.
* **The banner fits narrow windows.** Below about 48 columns the fixed
  44-wide `HEX CLI` frame split into fragments; it now shrinks to the
  window, and drops to a plain heading when even that will not fit.
* **The status line keeps moving during a long tool run.** It repainted
  only on a spinner tick or a transcript write, so a quiet subprocess froze
  the numbers; the sampler now refreshes them once a second, and the
  repaint skips itself when nothing changed.
* **The label reads `responding` once tokens arrive**, not `thinking` for
  the whole answer.
* Redundant box repaints are skipped when the rendered box is unchanged.

### Windows Terminal by default

The Start Menu shortcut launched `conhost.exe` on purpose (2.2.0, for the
taskbar icon). Since 2.5.1 turned QuickEdit off there to stop clicks from
freezing output, the classic console has had no drag-to-select at all,
which is how the question "why can't I select text" came up. The installer
now registers a "Hex CLI" Windows Terminal profile as a fragment (never
touching the user's settings.json, and skipped when a profile of that name
already exists) and points the shortcut at `wt.exe -p "Hex CLI"` when
Windows Terminal is installed; conhost stays the fallback. Selection and
copy work in Windows Terminal whatever the console mode, the context gauge
draws its pie glyph there, and redraws of the status bar are smoother. The
uninstaller removes the fragment. Trade-off: the taskbar shows the
Terminal icon, not Hex's; the tab shows Hex's.

Selection did not work there at first either: the mode Hex sets (QuickEdit
off, mouse input still on, the console default) is exactly the one Windows
Terminal reads as "the application wants the mouse", so drags went to Hex
and only Shift+drag selected. `disable_quick_edit` and the launcher now
clear `ENABLE_MOUSE_INPUT` as well; Hex reads no mouse events.

### Releases, re-organised

Ten releases in 38 days, and the last five were set by the npurun fork's
binary and by same-day fixes: 2.5.0 fixed 2.4.0's silent empty replies seven
hours later, 2.6.1 changed 24 lines to carry fork 0.2.3, and 2.6.2 reverted
2.6.0's async init. Three structural causes, each fixed:

* **The fork has its own repository and releases.** Its source lived in one
  local clone, and the README's "build from `npurun/` here" could not be
  followed from a checkout. It is now
  [NathanL15/npurun](https://github.com/NathanL15/npurun), branch
  `hexcli-fork`, tagged v0.2.0–v0.2.3 with each version's binary on its
  release and a changelog that finally lists the July fork work. Hex CLI
  releases carry no asset from here on; `install.ps1` and `hexcli --update`
  download from the fork.
* **The required fork build is enforced.** `REQUIRED_NPURUN` in
  `launcher.py` (0.2.3). `--doctor` fails on an older build, the launcher
  warns at start-up, the installer replaces an older build instead of
  keeping it, `--update` skips the download when the binary is already
  current, and discovery prefers whichever candidate meets the version, so
  a stale cargo build cannot shadow a fresh download. Before this, a machine
  on 0.2.1 running 2.6.2 had polling on, no prime, no watchdog and the
  async-init override ignored, and nothing said so.
* **One version source.** `hexcli/__init__.py` holds `__version__`;
  `pyproject.toml` reads it and the README no longer restates it. The
  hand-edited copy in `agent.py` had stayed at 2.5.1 through 2.6.0–2.6.2:
  `--version` printed 2.5.1 and every chat log since 09-05 is stamped
  2.5.1. CI now runs on tag pushes and refuses a tag that does not match
  the version.
* **`RELEASING.md`** records the numbering rule and the gate. A change to
  the binary, the launcher's environment or the prompt runs the multiturn
  arm with think time and the 3K stall probe before it is tagged, because
  both regressions above lived where the extended suite does not look.

### The end-of-turn prewarm is already right (negative result)

A tail-aware, forced end-of-turn prewarm was built, unit-tested and measured
against the shipped one on Hex's real turn shape
(`tools/backend_bench/prewarm_tail_probe.py`): identical next-turn first
token (0.7–0.9 s) at every tail length, and prefilling the session history
into the prewarm costs a rebuild (8.7 s) because a prefix past Genie's
~3,100-token divergence ceiling cannot be matched. Reverted; the "20 % of
first calls pay a rebuild" figure in the 2.6.2 study was a harness artefact
(zero think time and single-message latency canaries), corrected in
`docs/backend_study/PROMPT_LEVER.md` §7 and the radar ceilings. Probe fix:
the bench prewarm calls were missing `/v1` and had been 404-ing silently.

## 2.6.2 — 2026-09-07

### NPU hangs at long context: async dialog init off by default

2.6.0 turned Genie's `allow-async-init` on for a 1.4 s faster dialog rebuild. A
dedicated probe (`tools/backend_bench/stall_rate.py`: Hex's prompt plus an
800-token tool result, about 3,065 input tokens, on AC power) hung 9 of 20
requests with it on and 1 of 25 with it off, 0 of 20 under the launcher's new
environment. The hang the 2.6.0/2.6.1 notes attributed to battery power is a
long-context failure that async init multiplies; host polling does not affect
it. The launcher now sets `NPURUN_HTP_ASYNC_INIT=0`; a dialog rebuild costs
4.3 s again instead of 2.9 s, cold start 5 s instead of 3.6 s. Smoke suite
20/20. `NPURUN_HTP_ASYNC_INIT=1` in the environment restores the old behaviour.

### The prompt-length lever, measured and closed

`docs/backend_study/PROMPT_LEVER.md`. The decode rate is stepped, not linear,
against context (~17 tok/s from 500 to 1,750 tokens, ~15 from 2,000 up), and
the prefix is cached, so a shorter system prompt buys no time at any size that
keeps the rules: the production prompt, a dedented copy (−124 tokens) and a
copy without the never-used delegate schema (−240) answered the same queries in
the same time to the hundredth of a second. Removing the delegate schema
degraded outputs. The radar's prompt-token and wall-time ceilings are corrected
and a "hangs per 100 requests at 3K context" axis added. New probes:
`decode_vs_context.py`, `prompt_latency_probe.py`, `stall_rate.py`,
`rewind_probe.py --ks` (the Rewind discard limit is exactly 600 tokens; 700
rebuilds).

## 2.6.1 — 2026-09-06

### A stalled NPU request now fails in a minute instead of six

On battery power the NPU occasionally fails a long-context request (see 2.6.0)
and the query did not return until Hex's own 300 s timeout. npurun 0.2.3 ends the
request when its 60 s watchdog fires: Hex sees an error and can retry, and the
stalled turn costs ~97 s instead of ~400 s. Requests that arrive while the
wedged query still holds the NPU get "busy" immediately. Requires the 0.2.3
`npurun-arm64.exe`.

## 2.6.0 — 2026-09-06

Requires the 0.2.2 `npurun-arm64.exe` from this release (the runtime overrides,
the forced prewarm and the async dialog init are all in the binary; the 0.2.1
binary ignores the new variables and simply behaves as before).

### The NPU server no longer spins three cores while idle

The Genie bundle ships `poll: true`, which makes npurun's host threads
busy-poll the NPU around the clock: 2.7 cores and +11 W with nobody typing,
+11 W while decoding, and the SoC at ~70 °C. hexcli-fork npurun 0.2.2 turns
host polling off by default (`NPURUN_HTP_POLL=0`, set explicitly by the
launcher). Measured on the X Elite, 2026-09-05, same bundle: idle 4.4 W
instead of 14.4 W, decode 19 tok/s instead of 15.5 at 9.8 W instead of
21 W (0.55 J per token instead of 1.44), SoC ~40 °C, and no loss under a
saturated CPU. Cost: first-token latency +0.1–0.3 s. Quality gate: extended
suite 62/82 with polling off vs 61/82 with it on, same seed, same evening;
smoke 19/20 twice. Set `NPURUN_HTP_POLL=1` before launching to get the old
behaviour back. Full study: `docs/backend_study/`.

### The first turn no longer pays the 2,300-token prefill

While the banner prints, Hex now hands the server its system prompt
(`/v1/npurun/prewarm` with `force`, npurun 0.2.2), so the first request
extends a warm cache: 0.85 s to first token instead of 4.2 s (or ~9 s when
the server still held another conversation). Measured on the way: Genie's
Rewind reuses a shared prefix and can drop at most ~600 cached tokens of
tail; the cache tops out near 3.3K tokens (extension fails at ~3.5K); with
`allow-async-init` a dialog rebuild costs 2.9 s instead of 4.3 s, which the
fork now turns on by default. A second, spare Genie dialog to hide rebuilds
entirely is not possible on 16 GB: the second context binary fails to load
(error 1007) once the first holds its 5.8 GB. Decode speed falls from 19 to
13.5 tok/s between an empty context and 2K tokens, so the length of the
system prompt itself is now the largest remaining latency lever.

### On battery: two hours back, and a hang that was always there

Unplugged, the resident server now idles at the machine's own 7.4 W (6.9 h)
instead of 10.5 W (4.8 h), and a four-turn conversation costs 190 mWh instead
of 242. The same session found that on battery, long-context requests
occasionally block for ~380 s with an NPU graph error; the untouched 0.2.1
configuration does it too (1 in 31 requests), so it is a platform issue on DC
power, not a 2.6 regression. Details and counts in
`docs/backend_study/CPU_VS_NPU.md` §13.

## 2.5.1 — 2026-09-04

### The "thinking… until Ctrl+C" freeze was the console, not the model

Sometimes, most often on the first message of a session, the answer never
appeared until Ctrl+C — and then it appeared all at once. Root cause, measured
in a window launched exactly like the Start Menu shortcut (`conhost.exe
cmd.exe /c "Hex CLI.cmd"`): classic conhost with QuickEdit on (the registry
default). A click inside the window — the click that focuses a freshly
opened window — starts a selection, the title turns to "Select …", and every
console write blocks until a key is pressed. The model kept generating and
the reader thread kept draining the socket; the main thread sat inside the
first `stdout.write`. Ctrl+C is "copy" while text is selected, so conhost
cleared the selection and released the writes without any interrupt
reaching Python — which is why no "Cancelled." ever printed and why every
one of those turns shows as `completed` in the chat log. The streaming client
itself was measured clean: 0 ms between `data: [DONE]` and return.

* The launcher's console setup and the REPL start both clear
  `ENABLE_QUICK_EDIT_MODE` on stdin (0x1f7 → 0x1b7, verified in a
  shortcut-launched window); the REPL restores the original mode at exit so
  a shared cmd window is not changed permanently. Windows Terminal ignores
  the flag.
* The streaming request now waits out a 429 + Retry-After like the keep-alive
  pool already did. The end-of-turn prewarm holds the inference slot for
  ~20 s (rebuild + prefill, server log 03:52:31→03:52:51), and a query typed
  inside that window failed outright with "HTTP Error 429" (chat log
  2026-09-04 03:52:50).

Also observed while diagnosing, not changed: the first request of a new
session diverges from whatever the server cached last; when that cache is
long the Rewind fails (4 s) and the dialog is recreated (12.7 s) before a
token is generated — 47 s for a two-message turn on 2026-09-03 23:43. The
prewarm only fires above 3,100 cached tokens, so a cache left just under the
threshold still pays this on the next session's first turn.

### Ask, don't give up — rule 12 now shows the question

Rule 12 already said "call finish with ONLY a clarifying question", and the
corrected grader showed the model mostly answering "Request was ambiguous.
Unable to proceed." instead. The rule now carries a worked example of the
finish call with the question in it, and states that saying the request is
ambiguous or that it cannot proceed is not a question. Screen at 5 runs:
ambiguous-1 1/3 → 5/5, ambiguous-3 1/3 → 5/5, ambiguous-2 ("Update the
file.") 0/3 → 0/5 — the model reads "the file" as a real target and acts.
Full suite at pass^5 on a fresh server (seed 20260905), gated against both
baselines: 27-case gate PASS after the recheck rule re-ran three cases that
missed once at five runs (error-recovery-2, factual-1, self-correct-1 — all
6/6). Run level 158/205 vs 93/123 (Fisher p = 0.79); ceiling panel gained
agentic-4, ambiguous-1, livestate-1, regression-knowledge-1 (all 5/5) and
ambiguous-3 (3/5), lost lint-1 (3/3 → 3/5) and trap-4 (1/3 → 0/5).
`ask_rule_r5_20260905.json` is the new first baseline for the gate.

One of error-recovery-2's five runs failed with "backend returned no
choices" — an empty reply, the Rewind artefact, surfacing as a model failure
instead of an invalid run; watch item. (The server also vanished at 11:30
during the run's final canary: a concurrent session had restarted it for a
CPU-vs-NPU benchmark, and the six-run recheck at 11:36 then shared that
server with the benchmark. The recheck cases were 6/6, so the verdict
stands, but the recheck's latencies are not clean numbers — one server per
arm is the rule for a reason.)

### Side padding, and Ctrl+Plus / Ctrl+Minus

* `side_padding` (default 2): stdout and stderr are wrapped so every row —
  printed, streamed token by token, spinner redraw, or the input line —
  starts `pad` columns in and ends `pad` columns short of the right edge.
  The wrapper does the wrapping itself at `width - 2*pad`: the first cut
  left the terminal to wrap long paragraphs, and its continuation rows
  came back at column 0 ("only the first sentence is indented"). Wrapping
  is word-aware even for streamed text — a row that fills mid-word erases
  the partial word (`ESC[nD ESC[K`) and reprints it on the next row; words
  over 30 cells break where they fall. The line editor uses the same
  usable width with explicit newlines, so the two never disagree. Verified
  in a shortcut-launched conhost window by reading the screen buffer back:
  120 columns, every row within 2–117, streamed and whole output identical.
  Spinner and live-render clears moved from 60 spaces to `ESC[K`, and the
  REPL enables VT processing itself (the launcher already did for the
  shortcut window).
* Ctrl+Plus / Ctrl+Minus at the prompt grow or shrink the classic console
  font by 2 px (8–40) and the size is remembered in
  `~/.shellai/console_font` for the next launch. Those chords produce no
  character, so `getwch` never saw them; the key reader now peeks the
  console input queue ahead of msvcrt, consumes the chord (and the bare
  Ctrl key-down that precedes it), and hands everything else on unchanged.
  Windows Terminal keeps its own zoom and never forwards the chord.
  The window keeps its size on screen: conhost keeps the cell count and
  grows the window when the font grows, so after the font change the
  column and row counts are refitted to the pixel size the window had
  before the first zoom (anchored once, so round trips land on the same
  cells). Then the conversation is cleared and reprinted through the
  margin layer — conhost's own reflow of old rows restarts continuation
  rows at column 0, which is the "loses its formatting" report. Measured:
  960×480 px stayed within a few pixels from font 16 through 20 and back.
* Ctrl+V pastes as one block. A classic console injects the clipboard as
  keystrokes; the reader used to take them one at a time, redrawing after
  each (the visible "typing"), and a carriage return with nothing queued
  behind it counted as Enter — a block ending in a newline sent itself.
  Now a burst of three or more queued keys is drained (20 ms grace for the
  console to finish injecting) and inserted whole: CR/CRLF become
  newlines, tabs four spaces, one trailing newline is dropped, and nothing
  submits until Enter. Verified by injecting `def f():\r\tpass\r` into the
  console input buffer: one `<paste>` token, `def f():\n    pass`.

### Eval instrument: significance in the repo, shuffled order, a binary gate

The eval council (2026-09-04, `/council` on "is the eval architecture
correct?") found the harness structurally sound — it measures the shipped
Rewind runtime — but its graders too lenient and its statistics too weak to
prove "no regression". Applied, in the council's order:

* `evals/stats.py`: Fisher exact (run level) and McNemar exact (paired
  pass^k) moved out of a scratchpad into the repo; `compare.py` prints them,
  with the reminder that p > 0.05 at 3 runs per case is absence of evidence.
  Re-run on the prompt-split arms it reproduces the 2026-08-31 verdict
  (91/117 vs 87/117, Fisher p = 0.646, McNemar p = 0.688).
* Every results payload now records what it was measured on: git SHA and
  dirty flag, npurun version, the server's advertised budget and window,
  QAIRT root, Rewind mode, the case-order seed, the model-call count, and a
  latency canary at the start and end of the run (`[SERVER-DRIFT]` finding
  when the end is 2× the start). Case order is shuffled under the seed —
  fixed order handed the last cases a tired server in every A/B.
* Graders: a bare `?` and "let me know" no longer count as asking ("Done.
  Let me know if you need anything else." passed); questions must be shaped
  at the user (wh-word or auxiliary + you/I, filename dots allowed); a message
  opening with "Done"/"Completed"/"All set" is a completion claim; the
  live-state CPU/GPU patterns are word-bounded (bare `arm` matched "warm");
  bigfile-1 must name something from the page it received and nothing it
  never read (`answer_grounded_in_tool_output`).
* `cases_multiturn.py --think-time SECONDS`: a pause between scenario turns
  like a person reading the answer. Zero, the old and only behaviour, fires
  the next turn instantly — the one situation the end-of-turn prewarm cannot
  help with, so every earlier multiturn number measured the prewarm as a
  cost.
* `evals/gate.py`: the ship gate is BINARY on the cases at 3/3 in every
  baseline given (27 of 41 across the v2.4 and v2.5 arms); one miss at 3
  runs is a recheck (6 runs, one miss allowed), the flaky rest is a ceiling
  panel that is tracked, never gated. `--scoreboard` writes
  `evals/results/LATEST.md` so the current number is findable. Applied
  retroactively with a single baseline, the v2.5.0 arm would have failed on
  three cases that were 3/3 in v2.4.0 — which is how weak "3/3 once" is as
  evidence of reliability, and why the gate wants two baselines.
* `evals/regrade.py` re-applies the current graders to saved traces, so a
  grader fix costs no NPU time — except where a grader reads tool output:
  saved traces cap it at 2,000 chars, and it refuses those cases (bigfile-1
  regraded 3/3 → 0/3 on a truncated page before that check existed).
* The canary carries a nonce and is sent twice with the faster kept. The
  first version measured the cache, not the server: a repeat of the
  preflight's text came back in 0.05 s, and the first request after a long
  transcript paid a 9 s divergent-Rewind rebuild.
* **The prewarm, measured with a person's pause (council step 3).** Two
  arms of `cases_multiturn.py --runs 3 --think-time 15 --seed 20260905`,
  fresh server each, prewarm ON (the shipped default) vs OFF
  (`prewarm_after_turn=false`). Quality: ON 31/48 turns, OFF 33/48,
  Fisher p = 0.83. First-response latency: mean ON 11.2 s, OFF 9.6 s; OFF
  faster on 9 of 16 turns, sign test p = 0.80, median difference −0.6 s —
  no evidence either way, and the mean gap is two turns. Where the
  rebuild finished inside the pause the next turn was 3–7 s faster (uc2-t6
  3.2 vs 8.0 s, uc3-t8-model 3.3 vs 10.8 s); where it did not, the turn
  waited on it (uc1-t4 25.0 vs 12.2 s, uc1-t6 32.9 vs 6.1 s). Server logs:
  ON 38 prewarms + 10 failed Rewinds + 49 dialog creations; OFF 34 failed
  Rewinds + 36 dialog creations. The rebuild costs ~20 s (13 s of it Genie
  dialog creation) and real pauses in the chat log run 11–77 s, median
  ~20 s, so it finishes about half the time. The default stays ON: the
  lever that would make it a clean win is a server that ABORTS an
  in-flight prewarm when a request arrives instead of making the request
  wait (roadmap watch item); flipping the default would be within noise.
  Results: `multiturn_prewarm_{on,off}_20260905.json` + server logs.
* What the corrected graders say (5 cases re-run live 2026-09-05, then
  regraded with the final patterns; `baseline_20260905.json` is
  `window_r3.json` with those five replaced): ambiguous-1/2/3 are 1/3, 0/3,
  1/3 — the model answers "Request was ambiguous. Unable to proceed."
  rather than asking, which the bare-`?` grader had scored as asking (2/3,
  0/3, 2/3); livestate-1 2/3 (one run named an Intel i7 without running a
  command); bigfile-1 3/3 under the grounded grader. "Ask, don't give up"
  is now a roadmap item.

Tests: 3 new in `evals/test_core.py` (busy-wait retry, deadline give-up,
QuickEdit cleared) + 3 (margin stream, word-aware wrapping incl. streamed
input, transcript redraw), 7 new in `evals/test_lineedit.py` (margin
wrap, auto-wrap unchanged at margin 0, ANSI-aware wrap, zoom tokens,
paste burst, short-burst replay, zoom anchor reset), 6 new in
`evals/test_runner.py` (clarification shapes, grounded answers, word-bounded
live-state, exact tests, gate + recheck rule, think time). 25 suites / 734
tests.

## 2.5.0 — 2026-09-02

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
means the next turn will auto-compact. `/stats` shows the same figure, and
`/context` shows just the numbers that decide it: history against the
budget, system prompt size, the server's per-call budget, compactions so
far, and how many more tokens until the next one.

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
