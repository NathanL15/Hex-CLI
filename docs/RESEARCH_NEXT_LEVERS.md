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
