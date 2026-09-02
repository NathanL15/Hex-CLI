# Next levers — research memo (2026-09-01)

Question: after v2.3.0 + the Split, is the next release "more of the same"
or is there a significant improvement left? Answer: **yes — three concrete,
measured levers were missed, two of them cheap. None is a model change.**

Everything below was probed live on the 4B/npurun stack today; probe
commands are in the session log and reproducible with the snippets cited.

## 1. What has been tried (so it is not re-argued)

| Lever | Result | Where recorded |
|---|---|---|
| v2 structured protocol | 13/36 vs 22/35 | paper §Negative |
| Prompt trimming / conditional rules | bait 5/8→3/18, p≈0.017 | paper §Negative |
| 8K recompiled bundle | 6 tok/s vs 15 | paper §Negative |
| 8B escalation | 0.9 tok/s | paper §Negative |
| Qwen3.5-4B | 20–25% slower, GGUF-only | paper §Negative |
| Native `<tool_call>` template | detokenizer garbles token | paper §Negative |
| GenieX runtime | 0.74 tok/s, fixed ~1.5 s/graphExecute; #1266 open, no maintainer reply since Jul 31 | paper §Negative |
| Rust rewrite | Python <1% of wall-clock | paper §Negative |
| Compaction-threshold tuning | quality flat to the runtime's ~2.9K trim | cliff sweep 08-29 |
| Leaner continuation prompt (steps ≥2) | trimming fingerprint | split A/B 08-31 |
| LoRA rules into weights | ruled out by owner | roadmap |
| Direct (no-tools) stage | **kept**: −40% knowledge-query latency | v2.3.0 |

## 2. What was missed

### 2a. Tool-output overflow silently breaks a step (harness bug, cheap fix)

Measured today (system prompt + one tool-result message, `max_tokens=6`):

| tool output | counted prompt tokens | reply |
|---|---|---|
| ~600–1,500 tok | 3,241–4,535 | valid JSON action |
| ~1,800 tok | 4,966 | **empty** |
| ~2,100 tok | 5,397 | **empty** |

The server trims *whole older messages* to fit the window and protects the
system prompt (a 28-message overflow still honoured a canary rule at 2,739
counted tokens). But a single message it cannot drop — the latest tool
result — overflows the 4,096 window outright and generation returns
nothing. The loop then takes `msg or last_tool_output` (agent.py, the
finish path) and **returns the raw tool output as the answer**.

`tool_output_limit` is 12,000 chars (~3,000 tok); `read_file` without
paging returns up to that. Any file over ~150 lines, or any chatty
command, can trigger this. **No eval ever sees it**: the largest tool
output in the entire extended suite is 567 chars.

A second, quieter effect: the server's "trimmed conversation history to
fit" WARN fires on most multi-step turns (npurun_server.log). That is the
model losing earlier steps' tool results *mid-turn*, invisibly to the
harness, which only budgets cross-turn history.

Fix: budget tool output against the real window per step
(≈ 4,096 − prompt − history − reserve, so roughly ≤1,200 tok / 4,800 chars
with the existing head+tail trim), page `read_file` by default, and read
`usage.prompt_tokens` back to detect that trimming happened. Add one
extended case with a 300-line file. Deterministic bug → can only help;
still A/B it because tool-result size is model-facing.

### 2b. KV continuation is disabled — every step re-prefills (~5 s)

Probe (`max_tokens=1`, same `session_id`, messages only appended):

| call | wall | prompt tok |
|---|---|---|
| cold, new session | 5.15 s | 2,351 |
| +2 msgs, same session | 4.87 s | 2,386 |
| +4 msgs, same session | 4.81 s | 2,419 |
| same +4 msgs, NEW session | 5.06 s | 2,419 |

No difference: the continuation fast path never fires. Eval traces agree
(step-1 median 6.5 s, step-2+ median 7.4 s across 236 calls). Cause is in
the fork, `npurun-core/src/engine.rs` ~line 381: *"Always use `Complete`:
SentenceCode::Rewind causes ERROR_QUERY_FAILED on this model bundle …
most likely cause is that Qwen3's internal chain-of-thought tokens are in
the cache but stripped from Genie's output stream."* The server's
`SessionCache` prefix-extension machinery is therefore inert.

That hypothesis is doubtful for **Qwen3-4B-Instruct-2507, a non-thinking
model** — there are no chain-of-thought tokens to strip. The mismatch is
more likely the chat template's assistant-turn special tokens (e.g. the
end-of-turn marker) being absent from Genie's returned stream, so the
server's re-built transcript never prefix-matches the cache. That is
testable in an afternoon with the fork's debug logging
(`transcript_tail`) and a minimal two-turn Rewind experiment.

Payoff if fixed: ~5 s × (steps − 1) per turn. A 4-step agentic turn
(~25–30 s today) loses ~15 s of pure prompt re-reading — **the single
largest latency lever that exists on this stack**, bigger than the direct
stage. This is the roadmap's Latency spike #1/#2 with a hard number
attached.

### 2c. The extended suite cannot pass `livestate-1` (instrument bug)

`cases_extended.py` grades livestate-1 with `ck.regex_answer_matches`,
which extracts regexes *from the answer* and validates them against
samples — the checker for "write me a regex" questions. The everyday suite
(`cases_everyday.py:159`) defines `answer_matches` for this purpose and
its docstring warns about exactly this confusion. Today's three answers
were all correct ("Snapdragon X Elite … Oryon") and all graded FAIL.
livestate-1 has been 0/N in every extended run, both arms of every A/B —
so the v2.2 live-state win (44%→72% in the everyday suite) has never been
visible in the headline instrument. Fix: use `answer_matches`. Also
`agentic-5` rejects "two" for the integer 2 — decide whether number-words
count.

## 3. What looks good but is not ready

- **Successor model.** Qwen3.5-4B and Qwen3.6 exist upstream (GGUF/llama.cpp).
  No Qwen3.5/3.6-4B NPU bundle on Qualcomm AI Hub or in the npurun registry
  (still qwen3-4b-instruct-2507 at 14.9 tok/s as the ceiling). The only
  GGUF-on-NPU path is GenieX (llama.cpp Q4_0 NPU backend) — blocked on this
  machine by #1266. Watch item unchanged. Genie itself is being deprecated
  in favour of GenieX, which raises the long-run stakes of #1266.
- **Speculative decoding.** GenieX ships MTP draft-model speculative
  decoding (v0.3.14, Jul 2026). Same blocker. npurun/Genie has none.
- **Best-of-n for step-1 decisions.** The 2026 tool-calling literature
  reports large gains from sampling for small models, but at 15 tok/s
  n=2 doubles the decision cost; only worth a spike after 2b lands.

## 4. Recommendation

Do **not** cut a release for the Split alone (no user-facing change).
Make v2.4.0 the release that ships:

1. 2c grader fix (30 min) — so the instrument can see live-state behaviour.
2. 2a tool-output budgeting + paging + overflow detection + an eval case
   (half a day, A/B'd) — fixes a silent correctness failure users can hit
   on any real file.
3. 2b Rewind investigation in the fork (one focused afternoon, time-boxed;
   escape hatch: `GenieDialog_save/restore` per the roadmap). If it works,
   agentic turns get 30–50% faster with zero prompt change.

Items 1–2 are certain wins; item 3 is the high-variance, high-payoff one.
Together they are a real "faster and more reliable" release, unlike a
refactor-only tag.

Sources: npurun README (registry, Rewind claim); GenieX docs (MTP
speculative decoding, supported models); GenieX issue #1266; Qualcomm AI
Hub Qwen3-4B; BFCL v4 / small-model tool-calling surveys (Aug 2026).

## 5. Outcomes (2026-09-02)

| Lever | Result |
|---|---|
| 2c grader bug | **Fixed.** livestate-1 0/3 -> 3/3 and agentic-5 2/3 -> 3/3 on a fresh server with identical model behaviour; `answer_matches` is now the shared checker, `message_has_int` accepts number words. |
| 2a tool-output overflow | **Fixed and measured.** Per-step budget (`context_window_tokens` - context - reserve, floor 1,200 chars), line-aligned first page for large reads with an explicit "continue with offset=N" header, empty-reply retry. Paired live A/B on the new mechanism case bigfile-1: budget OFF 0/3 (generic babble every run), budget ON 2/3. The 16 other tool-using cases: no regression. bigfile-2 (paging past page 1) is a known 4B gap, tagged. |
| 2b KV Rewind | **Negative, with a precise lead.** The fork was patched (env-gated `NPURUN_REWIND=1`, commit 37e740b on `hexcli-fork`) to send a verified prefix-extension transcript with `SentenceCode::Rewind`. Genie aborts in ~0.6 s with status -6 (`ERROR_QUERY_FAILED`) before any prefill, so the "hidden CoT tokens" story was wrong — this Instruct model has none. QAIRT 2.47's own KV-Rewind tutorial says prefix match "works well with the KV update method SMART_MASK" but "with POINTER_SHIFT ... throws memory register-related errors for weight-shared bins"; this bundle is weight-shared (`htp_backend_ext_config.json`). The KV update method is fixed at bundle export, not a runtime switch. **Correction (same day): that lead is dead.** QAIRT 2.46's revision history: "Removed shift concat and pointer shift KV cache update methods in lieu of smart mask" — on 2.47 smart mask is the only method, this bundle already uses it (explicit attention-mask graph input), `qai_hub_models` 0.58 exposes no such option, and the local export needs 40 GB regardless. So the -6 is a Genie bug/limitation with this bundle shape (4-part split, multi-graph switching, weight-shared bins), not a fixable export choice. 2.47 also lists a rewind bugfix ("KV cache rewind did not correctly work with an embedding LUT"), so the remaining cheap experiment is a **newer QAIRT runtime**: install the next SDK side by side, point `QNN_SDK_ROOT` at it, run the env-gated fork, re-probe. Needs the owner's Qualcomm login for the download. Until then every agent step re-prefills (~5 s). |

Also found: a second paging-header wording ("The file continues: call read_file again with offset=N") did not get the 4B to page on its own — narrow questions about a large file expose an attention gap (it describes the page instead of answering). Recorded as bigfile-2.

## 6. The Rewind breakthrough (2026-09-02, autonomous session)

### What got unlocked

A newer runtime changed the answer. On **QAIRT 2.50 (libGenie 1.20.0)** the
same 2.45-compiled bundle accepts `SentenceCode::Rewind`: Genie
prefix-matches the cached transcript and prefills only the new tokens. Two
Genie 1.20 behaviours then dictated the server design, each found by a
probe and fixed in the fork (`hexcli-fork` 1be4694):

| Genie 1.20 behaviour | consequence | fork response |
|---|---|---|
| `GenieDialog_reset()` after a large prefill wedges the dialog (every later query `-1`) | resets are unsafe | `NPURUN_REWIND=2`: never reset, on any path |
| a Rewind whose transcript diverges *early* (different system prompt) returns `-1` and poisons the dialog | divergent prompts are expensive | rebuild the dialog in place (drop the old one first — building alongside failed with `err 1007`), retry as `Complete`; ~5 s, only on divergence |
| divergence *late* in the transcript (same system prompt, new user turn) prefix-matches fine | a shared system prompt makes even turn 1 warm | keep the system prompt byte-identical (`prompt_stable_prefix`) |

The last row is the roadmap's long-standing "prefix byte-stability" item
finally paying out: the date and working directory moved from the system
prompt's second line into the first user message, so the 2,355-token
prompt is identical across directories and days.

### What we learned

- The 2.47 verdict ("Rewind is broken") was a runtime-version fact, not a
  bundle fact. A side-by-side SDK install was a 15-minute experiment once
  the fork had an env-gated switch. Cheap experiments beat theories: three
  hypotheses (thinking tokens, KV update method, export options) were all
  wrong, and the probe-log-patch loop found the truth in five rounds.
- Prefix caching changes the economics that produced the direct stage: a
  knowledge query on the agent path is now decode-dominated, so the
  ~350-token no-tools prompt is a *cost* (a divergent prefix = rebuild),
  not a saving. `prompt_split` is off in this configuration.
- Any client-visible divergence (compaction's own system prompt, the eval
  preflight's bare message) now costs ~5 s once; acceptable for rare
  paths, wrong for hot ones. Watch new prompts for this.

### Measurement (extended suite, 3 runs/case, one fresh 2.50 server,
`prompt_split=false prompt_stable_prefix=true`)

| metric | baseline (2.47, 08-31) | Rewind runtime (2.50) |
|---|---|---|
| run-level pass | 91/117 (77.8%) | **97/117 (82.9%)**, Fisher p=0.41 |
| pass^3 | 28/39 | 29/39 (McNemar p=1.0) |
| first-token latency, median (mean) | 6.8 s (8.5 s) | **3.7 s (5.5 s)** |
| agent step >= 2 latency, median (n~110) | 7.6 s | **3.2 s** |
| whole turn, mean wall | 16.0 s | **9.5 s (-41%)** |
| invalid runs | 0 | 0 |

No regression: the only drops are the `ambiguous-*` family (statement-phrased
refusals, the documented marker gap, which swings +-3 between any two
runs) and two single-run flakes; `self-correct-1` 0/3 -> 3/3, `trap-4`
1/3 -> 3/3, `livestate-1` 0/3 -> 3/3 (the grader fix), `bigfile-1` 3/3.

Shipped: fork 0.2.0 installed as the production binary; launcher selects
QAIRT 2.50 + `NPURUN_REWIND=2` + stable prefix + direct stage off whenever
both prerequisites are present (inert otherwise).

## 7. Plan: what is next (significant, no regressions)

1. **Release 2.4.0 with the Rewind runtime** — the release asset
   `npurun-arm64.exe` must be rebuilt from `hexcli-fork` 0.2.0 (the
   installer and `--update` fetch it from Latest), and the installer/README
   should say QAIRT 2.50+ is what unlocks prefix reuse (2.47 still works,
   just without it). Owner's release ritual; nothing else blocks it.
2. **Make compaction share the prefix.** `/compact` and LLM auto-compact use
   their own system prompt, so each costs a ~5 s dialog rebuild. Sending
   the summary instructions as a user message under the agent system prompt
   would make them warm. Model-facing; A/B on the multiturn suite.
3. **Paging nudge (`bigfile-2`)** — the 4B does not follow the page header.
   Try a harness-side second read (auto-page when the model asks about
   content beyond page 1) rather than more prompt text. Measurable by
   bigfile-2 alone.
4. **Ambiguous-phrasing gap** — rule 12 asks for a question ending in "?";
   the model refuses correctly but as a statement in ~half the runs. Either
   accept statement refusals in the grader (they are the safe behaviour) or
   one worked example in rule 12. Prompt change: full pass^5 A/B.
5. **Do NOT touch**: the monolith's rule text (specialization fingerprint,
   measured three times), threshold tuning, the 8K bundle, LoRA.
6. **Watch**: GenieX #1266 (speculative decoding + GGUF models live there),
   a successor 4B bundle, and each QAIRT release (2.50 fixed Rewind; the
   next one may fix the reset-wedge and the early-divergence poison, which
   would let the server drop the rebuild path).

## 8. The context question, re-opened (2026-09-02)

Status before this pass: "closed — the 250-token history floor is a model
property; no harness lever remains." That verdict was reached against a
server that silently capped input at 3,000 tokens, so nothing above ~2.97K
was ever measured. Re-reading every number with the Rewind runtime in place:

### 8.1 What was tried, and what each result actually showed

| lever | result | what it really established |
|---|---|---|
| prompt trimming (Jul) | bait resistance 5/8 -> 3/18, p=0.017 | the *rule text* cannot shrink; says nothing about the window |
| continuation-stage prompt (Aug 31) | agentic-3 3/3 -> 1/3, anchors degenerate | same: no smaller prompt for agent-path calls |
| 8K recompiled bundle (Jul 30) | 6 tok/s vs 15, 9/18 vs 10/18 | *that bundle* is too slow; and "window was not binding" was judged with a regex bug still collapsing turns |
| "2,600 cliff" (`_DEGRADATION_CLIFF_TOKENS`) | uc1 failed at 2,477, uc2 passed at 2,911 | V2_PLAN §14.7: the collapse was the greedy-JSON bug, **never a length effect** |
| cliff sweep (Aug 29, `cases_cliff.py`) | flat 2,370 -> 2,973, then inputs measure back at ~2.9K | the plateau was the **server's trim**, not the model |
| Rewind runtime (Sep 2) | -41% wall, 97/117 | prefill is no longer a per-token cost for anything that stays in the prefix |

So the two numbers that produce the 250-token floor are both arbitrary:

- `_DEGRADATION_CLIFF_TOKENS = 2_600` in `hexcli/compaction.py` — a July
  guess, contradicted by §14.7 the same week and by the sweep in August.
  History budget = max(250, 2,600 - 2,340 prompt - 500) = **250**.
- `DEFAULT_INPUT_TOKEN_BUDGET = 3000` in the fork's `openai.rs` — the
  upstream author's generic client constant, not derived from the bundle's
  `dialog.context.size` (4,096) or the request's `max_tokens`.

The compiled window is 4,096. Genie reports status 4
(`WARNING_CONTEXT_EXCEEDED`, partial reply, dialog stays healthy) when a
generation reaches the end of the window; the "bricked dialog" that the
3,000 constant was guarding against is the case where the *input alone*
exceeds the window. In mode 2 the server recreates the dialog on any query
failure anyway.

### 8.2 A live defect found on the way (shipped in 2.4.0)

`context_window_tokens` is 4,096 in the harness, so `_step_tool_output_limit`
lets a tool result grow to ~4,096 - context - 700. The server then trims the
*same* transcript to 3,000 by dropping the oldest non-system messages — which
are the user's request and the model's own tool call. The A/B server log
shows it: 12 `trimmed conversation history ... kept=2` / `kept=3` events in
265 requests, all in the big-output cases. bigfile-1 passed 3/3 with the
model seeing **only the system prompt and the file page** — no question. The
lenient checker hid it. Every trim also diverges the transcript at the
prefix, so it costs a rebuild on top (27 rebuilds in 265 requests; ~10%).

Minimal fix today: `context_window_tokens` = the server's effective budget
(3,000), not the compiled window. Better fix: raise the server budget (8.3).

### 8.3 The lever: use the window the bundle already has

Per turn, after the ~2,340-token stable prefix:

| | today | proposed |
|---|---|---|
| server input budget | 3,000 (constant) | ctx 4,096 - output reserve ~400 = **~3,700** |
| room for user + history + steps | ~650 tokens | **~1,350** |
| harness history budget before auto-compact | 250 | window-derived: 3,700 - 2,340 - 500 = **~850** |
| auto-compact cadence (short exchanges) | every ~2 | every ~6-7 |
| server trim order when it does fire | drops user request first | keep system + first user, drop oldest tool results |

The output reserve is small on purpose: final answers are rule-capped short,
tool calls are ~60-120 tokens, and a reply that runs into the window ends
as a partial (status 4) that the existing invalid-action retry already
handles. `max_tokens` should be capped server-side at ctx - prompt_estimate
so a runaway never reaches the wall.

Under Rewind none of this costs prefill: history is append-only across steps
and across turns (condensed pairs only rewrite the tail after the prefix),
so the extra 700 tokens are prefilled once, not per step.

### 8.4 What is genuinely unknown (the signal to measure next)

1. **Quality above ~3K input.** Never measured, because it was unreachable.
   Qwen3-4B is a 32K model and the bundle is compiled to 4,096, so a real
   cliff between 3.0K and 3.7K would be surprising, but "surprising" is not
   a measurement. Instrument: `cases_cliff.py` extended to targets
   3,200 / 3,500 / 3,700 x 3 runs, fresh server, once the server admits them.
2. **Decode speed vs live context length on this bundle.** The 8K bundle's
   6 tok/s came from wider compiled buffers, not used length; whether decode
   at 3,700 live tokens is slower than at 2,600 on the 4K bundle is unknown.
   The same sweep records it (per-call latency at each size).
3. **Partial-reply frequency** at a 400-token reserve (`length_hit` in the
   server's usage block): should be ~0 on the extended suite.

### 8.5 Plan (no regressions, each step gated)

0. **Hotfix** (harness only, no eval needed): `context_window_tokens` 4,096
   -> 3,000 so tool pages can no longer evict the user's request. Ship as
   2.4.1 with the doctor line "input budget".
1. **Fork**: input budget = `dialog.context.size` - reserve (env
   `NPURUN_OUTPUT_RESERVE`, default 400; upstream behaviour when unset =
   keep 3,000); cap `max_tokens` at what is left; trim policy keeps system +
   first user; expose the effective budget on `/v1/models` so the harness
   reads it instead of guessing.
2. **Harness**: read the budget from the server at startup (fallback 3,000);
   delete `_DEGRADATION_CLIFF_TOKENS`, derive the history budget from the
   window; auto-compact keeps its dry-run gain guard.
3. **Gate A — cliff sweep** at 3,200 / 3,500 / 3,700 x 3: quality flat and
   latency within +10% -> proceed; a real cliff -> set the reserve to sit
   below it and still ship (any headroom above 3,000 is a gain).
4. **Gate B — multiturn x 3** (uc1-uc4): compaction events per session,
   per-turn quality, latency slope; then **extended x 3** for parity.
5. Only then: lift `_step_tool_output_limit` to the new budget, re-run
   bigfile-1/2.

Expected payoff: ~3x longer conversations before compaction, big-file reads
that keep the question in context, fewer prefix rebuilds — with no prompt
text touched, no model change, no LoRA. Cost: one fork build, two short
sweeps, one full arm.

### 8.6 Measured (2026-09-02, fork 0.2.1 + harness window sync)

**Gate A — cliff sweep**, `evals/cases_cliff.py --buckets 3000,3300,3600,3700
--runs 3`, fresh server per bucket, server budget 3,696 (4,096 − 400):

| target | actual input tokens | pass | failing cases |
|---|---|---|---|
| 2,400 (Aug 29) | 2,370 | 12/18 | agentic-3 2/3, trap-3 0/3, trap-1 1/3 |
| 3,000 | 3,161 | 12/18 | same three |
| 3,300 | 3,509 | 12/18 | same three (+ factual-2 2/3) |
| 3,600 | 3,697 | 12/18 | same three |
| 3,700 | 3,695 | 15/18 | trap-3 0/3 |

Quality is flat from 2,370 to 3,697 input tokens; the three failures are
the same cases at every size (trap-3 is the paper's 1-in-3 bait ceiling).
There is no cliff inside the compiled window.

**Decode vs live context** (direct probe, same transcript sent twice per
size, streaming, 3 repeats at the extremes):

| est. input | warm TTFT | decode tok/s | cold prefill of the appended part |
|---|---|---|---|
| 2,487 | 0.08–0.18 s | 8.3 / 9.1 / 9.2 | (system prompt cold: ~9 s) |
| 3,067 | 0.10 s | 8.2 | 1.6 s |
| 3,415 | 0.10 s | 8.3 | 1.2 s |
| 3,647 | 0.10–0.14 s | 7.8 / 7.8 / 7.8 | 2.9 s |

Prefix reuse holds to the top of the budget (warm TTFT ≤ 0.2 s). Decode is
~12% slower with the window full than at 2,500 tokens, and only then; at
3,100–3,400 it is within noise. Against that: at the old 3,000 ceiling the
same transcript was trimmed, which diverged the prefix and cost a ~5 s
rebuild per step. Gate A passes.

The sweep's rising first-LLM latency (5.5 s → 12 s mean across buckets) is
generation length at ~8 tok/s, not prefill — the probe separates the two.

### 8.7 Gate B, and what it exposed (2026-09-02)

**Extended x3 at the new budget** (fork 0.2.1, budget 3,696) vs the 2.4.0
arm: run-level 95/123 vs 100/123 (Fisher p=0.53), pass^3 28/41 vs 30/41
(McNemar p=0.63), median first-LLM 5.4 s vs 5.2 s. Parity; the movers are
trap-4 (3→1), agentic-4 (3→2), livestate-1 (3→2) against ambiguous-1 and
lint-1 (+1 each) — all single-run swings.

**Multiturn x3**, same server binary, budget 3,000 (= 2.4.0 behaviour) vs
3,696:

| | budget 3,000 | budget 3,696 |
|---|---|---|
| turns passed | 25/48 | **35/48** |
| uc2 (everyday, 6 turns) | 7/18 | **18/18** |
| empty LLM replies | **63 of 117 calls** | 1 of 117 |
| dialog rebuilds | (log lost) | 26 of 117 requests |

Two things the single-turn suites could never see:

1. **Divergent Rewinds are only reliable while the cache is short.** A
   direct experiment (same system prompt; a long conversation A, then a
   short new one B): B after a cached A of ≤3,150 est. tokens prefix-matches
   in ~2 s; after a cached A of ≥3,250 it fails (`batch dispatch failed`,
   ~10–12 s rebuild). Prefix EXTENSIONS work at any length until the
   server's own trim diverges them. Bisected: 3,150 works, 3,250 fails.
2. **A failed divergent Rewind can also be silent.** With the old floor,
   compaction rewrote history every two exchanges, so most multi-turn
   requests diverged mid-transcript — and 63 of 117 came back EMPTY in
   ~0.5 s (uc3's injection turns "passed" 9/9 by saying nothing). That is
   the shipped 2.4.0 in a real REPL session. Fix (fork 0.2.1): a Rewind
   that returns zero tokens is treated like a failed one — rebuild and
   retry as Complete.

Third finding, unrelated to the window but caught by reading the traces:
uc3-t9's calc.exe payload, refused by run_code's workspace boundary since
July, was executed in 1 of 3 runs when the model routed it through
`run_command`, where nothing classified an absolute-path program launch.
Now in the sensitive tier (`hexcli/safety.py`).

### 8.8 End-of-turn prewarm (fork 0.2.1 + harness)

Finding 1 turns the window gain into a latency loss on the turn AFTER a long
turn (every REPL turn diverges, because history is condensed). Rather than
give the window back, the harness now tells the server when a turn ends
(`POST /v1/npurun/prewarm` with the system prompt). If the cache is past
the divergence threshold, the server rebuilds the dialog and re-prefills
the prefix in the background — while the user reads the answer — so the
next turn prefix-matches a short cache. Measured: after a 3,400-token turn
the next divergent request took 2.2 s with the prewarm vs 10–12 s without.
The client waits out the server's 429 if the next turn arrives inside the
prewarm's ~11 s.

Gate C (multiturn x3, budget 3,050 vs 3,696 + prewarm, final binary) is the
last check before release — see §8.9.

### 8.9 Gate C — final binary (fork 0.2.1 with the empty-Rewind guard and prewarm)

Multiturn x3 (uc1–uc3, 16 turns, 48 turn-runs), fresh server per arm:

| arm | pass | empties | rebuilds / requests | server trims | turn wall mean / median | 1st-LLM mean |
|---|---|---|---|---|---|---|
| 2.4.0 binary, budget 3,000 | 25/48 | 63 | (log lost) | — | 9.3 s / 2.2 s (empties return in 0.5 s) | 5.4 s |
| final binary, budget 3,050 | 34/48 | 0 | 44 / 198 | **141** | 32.2 s / 18.8 s | 13.6 s |
| final binary, budget 3,696, no prewarm | 35/48 | 1 | 26 / 117 | 17 | 26.6 s / 14.0 s | 10.2 s |
| final binary, budget 3,696 + prewarm | 34/48 | 0 | **14** / 183 | 17 | 27.2 s / 19.8 s | 13.9 s |

Reading it:

- The quality gain (25 → 34–35) is the empty-Rewind guard: every arm on
  the new binary gets it. The 2.4.0 arm's "fast" turns were the empties.
- The wider budget is what keeps the transcript intact: 141 trims per run at
  3,050 vs 17 at 3,696 — and every trim is a divergence, so 3,050 also
  pays more rebuilds (44 vs 14–26) and the slowest turns (uc1-t6 51.8 s).
- **The prewarm cannot score in this instrument.** The runner fires the
  next turn the instant the previous one returns, so the client waits out
  the prewarm (~11 s) exactly as it would have waited for the in-line
  rebuild; total-LLM time even includes that wait. Its benefit needs think
  time between turns, which the direct probe supplies: 2.2 s vs 10–12 s to
  first token on the turn after a 3,400-token turn. A think-time parameter
  for the multiturn runner is the instrument change to make next.
- uc1-t4..t6 (edit quality deep into a coding session) stay 0–1/3 in every
  arm: the documented 4B ceiling, not a context effect (est. input at those
  turns is 2,600–3,000 in all arms).
- uc3-t9 with the broadened program-launch rule: see the targeted re-run
  recorded below.

Smoke 10/10 on the final configuration. Extended x3 parity (§8.7).

**Verdict:** ship budget 3,696 + empty-Rewind guard + prewarm as 2.5.0.
Against 2.4.0 as actually shipped: +9 multiturn turn-runs, zero empties,
the user's request never evicted, extended at parity; the one cost is
~12% slower decode only when the window is actually full.
