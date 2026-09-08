# The prompt-length lever, re-measured under the Rewind runtime (2026-09-06)

Question: the radar lists "prompt tokens per task" as the largest open lever
(2,409 tokens per task against a 1,500 target, with a −40 % wall-time ceiling
from a 1,200-token prompt). Three earlier prompt cuts regressed. What does a
shorter prompt actually buy on the 2.6.1 stack, what is safe to cut, and what
should change instead?

Everything below was measured on AC power, on the quarantined machine, with
the shipped 2.6.1 configuration (fork 0.2.3, QAIRT 2.50, `NPURUN_REWIND=2`,
host polling off, async dialog init on) unless a row says otherwise. Raw data:
`data_npu_ab/decode_vs_context.jsonl`, `prompt_latency_probe.jsonl`,
`rewind_probe.jsonl` (tag `discard_fine`), `stall_rate.jsonl`; probes in
`tools/backend_bench/` (`decode_vs_context.py`, `prompt_latency_probe.py`,
`stall_rate.py`, `rewind_probe.py --ks`).

## 1. What was already known

| attempt | result | what it established |
|---|---|---|
| drop situationally irrelevant rules (Jul) | bait resistance 5/8 → 3/18, p ≈ 0.017 | the rule text is load-bearing |
| rules 10 and 12 conditional (Jul 31) | trap-4 5/8 → 2/8, ambiguous-1 3/8 → 1/8 | the restraint rules work beyond their trigger case |
| leaner prompt for steps ≥ 2 (Aug 31) | agentic-3 3/3 → 1/3, degenerate edit anchors | no smaller prompt for agent-path calls at any step |
| Rewind runtime (Sep 2) | −41 % whole-turn wall | the prefix is no longer re-read per call |
| decode vs context (Sep 5) | 19.1 tok/s at 44 tokens → 13.6 at 2,550 | "the 2.3K prompt costs ~30 % of decode" — relative to an empty context |

The "30 %" number is what put prompt length on the radar. It compares the
production prompt with a context no agent prompt can reach.

## 2. What the prompt is made of

Bundle tokenizer counts of the stable-prefix prompt (server estimate
`chars/4` = 2,386; the server's `usage.prompt_tokens` = 2,331):

| part | tokens | share |
|---|---|---|
| head | 37 | 2 % |
| rules 1–14 | 1,603 | 72 % |
| tool schemas (tail) | 480 | 21 % |
| delegate schema (always appended in the outer loop) | 118 | 5 % |
| **total** | **2,236** | |

Largest rules: 9 (live-state cookbook) 248, 12 (ambiguous edits) 219,
10 (tool bait) 189, 14 (run_code sequence) 174, 2 (edit anchors) 141,
13 (verify_syntax) 139, 11 (error recovery) 122.

Two reductions change no words: stripping the indentation saves 124 tokens
(2,112); dropping the delegate schema saves 118 (the `delegate` tool has never
been called in any eval trace or any of the 22 logged sessions). Both
together: 1,996. Anything below that means cutting rules.

## 3. What a shorter prompt buys in time

### 3.1 Decode rate against live context (128-token prose, polling off)

| context tokens | 250 | 500 | 750 | 1,000 | 1,250 | 1,500 | 1,750 | 2,000 | 2,250 | 2,500 | 2,750 |
|---|---|---|---|---|---|---|---|---|---|---|---|
| decode tok/s | 20.2 | 17.8 | 19.0 | 16.5 | 16.9 | 17.0 | 17.0 | 15.1 | 14.7 | 14.8 | 14.8 |

The curve is stepped, not linear: ~20 tok/s only under ~500 tokens, ~17 from
500 to 1,750, ~15 from 2,000 up (and, from the September 2 probe, ~8 % lower
again near 3,600). A Hex call sits in the 2,000+ step at every stage of every
task (median context 2,391 at step 0, 2,536 by step 3 in the 5-run baseline).
To leave that step for the whole task the prompt would have to be under
~1,300 tokens — 900 tokens of rules.

### 3.2 Hex-shaped calls (eight real queries, prefix warm, medians)

| prompt | tokens | step-0 first token | decode | step-0 total | step-1 total (after a 400-token tool result) |
|---|---|---|---|---|---|
| A production | 2,236 | 0.71 s | 12.7 tok/s | 3.16 s | 4.06 s |
| B dedented | 2,112 | 0.70 s | 12.7 | 3.17 s | 3.77 s |
| C dedented, no delegate schema | 1,996 | 0.70 s | 12.4 | 2.36 s\* | 5.43 s\* |
| D reference: C minus rules 13, 14 | 1,701 | 0.56 s | 14.5 | — | 3.60 s |
| E reference: C minus rules 9, 13, 14 | 1,466 | 0.56 s | 14.4 | — | 4.89 s |

Same query, same 39-token edit_file reply: A 3.63/3.80 s, B 3.79/3.81 s,
C 3.79/3.79 s. **At every size that keeps the rules, the per-call time is
identical.** The reference cuts (D, E) gain 0.14 s of first token and ~13 %
of decode at step 0 only; by step 1 the context is back over 2,000 tokens and
the decode rate is A's again. On a 30-token call that is ≈ 0.4 s, once per
task, for prompts already measured to break restraint.

\* C's totals differ because its outputs differ, not its speed: with the
delegate schema removed the model answered `{"` and stopped in 3 of 17
step-0 calls, and its step-1 replies became 60–100-token prose ("The provided
text appears to be corrupted…") where A/B answered in 19–39 tokens. The tail
of the prompt is part of what the model is tuned to; B's 32 outputs were
action-for-action the same as A's.

### 3.3 Where a call's time goes

From the 5-run baseline traces (406 calls): median call 3.4 s for a median
24–34 generated tokens; step-0 calls carry 60 % of LLM latency; final
messages are 23 tokens median. Per call ≈ 0.7 s fixed (NPU wake-up with
polling off, plus the new tokens' prefill) + generated tokens / 12.7. The
fixed part is 23 % of a median call and is the polling trade-off already
decided in 2.6 (0.6 s per call for 11 W of idle).

The one large item is neither: **20 % of first calls (41 of 208) carry a
rebuild-sized excess (mean +9.7 s)** — 369 of the 1,819 s of LLM time in the
whole run. §7 traces those to the eval harness itself (zero think time, so
the end-of-turn prewarm's rebuild lands on the next case's first call, plus
the runner's single-message latency canaries, which diverge the cache at
every chunk boundary), not to anything a REPL user pays. Corrected the same
day; the earlier reading of this number as "the next lever" was wrong.

## 4. Verdict on the lever

- **As a speed lever, prompt length is closed.** Below the rule line there is
  nothing to gain; at the rule line the gain is ≈ 0.4 s per task and the
  restraint rules are the price. The radar's "−40 % from a 1,200-token
  prompt" ceiling was extrapolated from the empty-context decode rate and is
  withdrawn.
- **As an energy lever it is closed for the same reason**: energy per task
  follows time per task.
- **What tokens still buy is room**: server budget 3,696 − 2,386 estimated
  prompt = 1,310 tokens for the user turn, history, and tool pages. The
  dedent (B) adds ~175 estimated tokens of room (+13 %) with outputs
  identical to production in the probe. It is the only candidate that
  survives, its value is modest, and it is model-facing, so it still needs
  the 5-run gate before it ships. Not a priority.
- **Do not**: drop the delegate schema (measured degeneration), make rules
  conditional (a mid-prompt divergence costs a rebuild under Rewind, on top
  of the July result), or cut rules.

## 5. Found on the way: the stall is a context-length problem, and 2.6 made it worse

Three requests hung during the probes — on AC power, which the battery study
had ruled out as the trigger — at 2,946, 2,971 and 3,187 input tokens. A
dedicated probe (`stall_rate.py`: Hex's prompt kept warm, each cycle a fresh
conversation extended by an 800-token tool result to ~3,065 tokens, natural
stops, server restarted after each hang instead of waiting out the 6-minute
wedge):

| configuration | cycles | hung (60 s watchdog) |
|---|---|---|
| polling on, async init on | 24 | 8 (33 %) |
| polling off, async init on (**2.6.1 default**) | 14 | 7 (50 %) |
| polling off, **async init off** | 25 | 1 (4 %) |
| polling off, async init off, 2.5K context | see §5.1 | |

The 09-05 five-run baseline (fork 0.2.1: no async init, polling on) made 406
calls including ten at 3.3K without a hang. `allow-async-init` went on by
default in 0.2.2 for a 1.4 s faster dialog rebuild, gated by a smoke run and
46 rebuild cycles that never sat at 3K. It multiplies the hang rate roughly
tenfold at the top of the window. Polling does not matter.

Two smaller runtime rules, both reproducible:

- **Rewind after a length-truncated generation fails** (Genie −1, the server
  resets, the next request rebuilds). Every probe with a small `max_tokens`
  hit it; Hex hits it whenever a reply ends on the window (status 4).
- **The discard limit is exactly where the server constant sits**: a new
  conversation that discards 600 cached tokens is warm (0.73 s); 700 fails
  and rebuilds (6.3 s). `REWIND_MAX_DISCARD_TOKENS = 600` is correct.

## 6. What should change

1. **Hotfix (no fork rebuild):** launcher sets `NPURUN_HTP_ASYNC_INIT=0`.
   Cost: rebuilds 2.9 → 4.3 s and cold start +1.4 s, on paths that already
   cost seconds. Benefit: the hang at long context goes from one in two to
   one in twenty-five. Gate: smoke suite + the 3K stall arm under the
   launcher's own environment, then release as 2.6.2.
2. **Rebuild avoidance instead of prompt cutting** (the 20 % of first calls):
   at end of turn, when the turn's tail exceeds the 600-token discard limit,
   prewarm with `force` so the next conversation starts warm; that turns a
   ~10 s in-line rebuild into a background one during think time.
3. **Server:** after `finish_reason = length`, mark the cache stale and
   rebuild on the next request instead of failing the Rewind, resetting, and
   retrying (three round trips today).
4. **Radar:** prompt-tokens axis target and the wall/energy ceilings corrected
   (this document is the evidence); add a "hang rate at 3K" robustness axis
   so the next runtime change is gated on it.
5. **Optional, low value:** the dedent, through the full 5-run gate, only for
   the history room.

## 7. Follow-up (2026-09-07): the "rebuild avoidance" lever, measured and withdrawn

Recommendation 2 above proposed forcing the end-of-turn prewarm when a turn's
tail exceeds the 600-token discard limit. It was built (a tail-aware
`_prewarm_backend`, config `prewarm_force_tail_tokens`), unit-tested, and
measured against the shipped prewarm on Hex's real turn shape
(`tools/backend_bench/prewarm_tail_probe.py`: request + tool steps, the
prewarm under test, 15 s of think time, then the next turn's request; four
reps per cell, fresh server per run, 2.6.2 stack):

| turn shape | shipped prewarm: next turn first token | forced/tail-aware: next turn first token |
|---|---|---|
| tail ≈ 520 tokens (one small step) | 0.72–0.88 s | 0.72–0.90 s |
| tail ≈ 665–724 (the supposed gap band) | 0.72–1.02 s | 0.73–0.87 s |
| tail ≈ 1,350 (three steps) | 0.71–0.73 s | 0.71–0.73 s |
| tail ≈ 1,350 with 1,000 tokens of history, prewarm prefills the full prefix | 0.72–0.89 s | **8.7 s** (rebuild) |

The shipped prewarm already warms every shape: below ~690 tail tokens the next
turn Rewinds the tail, above it the server rebuilds on its own in the
background. Prefilling the history into the prewarm is actively harmful: a
prewarmed prefix past Genie's ~3,100-token divergence ceiling cannot be
matched by the next turn at all, while re-reading 1,000 tokens of history on
the warm path costs ~0.15 s. The change was reverted; nothing ships.

What the multi-turn instrument showed on the way (control arm, 3 runs × 3
scenarios, 196 requests): 15 in-line rebuilds, of which 10 were the runner's
own single-message latency canaries diverging the cache at chunk boundaries
and 5 were mid-turn server trims (the window filling up); none were a turn
whose tail the prewarm failed to cover. The eval's 12 s think time is also
shorter than a background rebuild (drop + create 5–8 s, prefill 4.7 s), so
first-call latencies in that suite include waiting for the prewarm — a cost a
person reading the previous answer does not pay. Two more instrument facts:
`bench.BASES["npu"]` has no `/v1`, so every earlier probe's prewarm call had
been answering 404 silently (fixed in the three probes; their conclusions
stand, since none depended on the prewarm), and the eval runner's canaries
should carry the system prompt if the suite is ever used to measure cache
behaviour.

Verdict: the runtime's cache policy is already right for Hex's turn shapes.
The latency floor per call is the 0.7 s wake-up plus the model's own tokens,
and the only remaining runtime lever is the rebuild itself (5–8 s to drop and
create a dialog with async init off), which is the price of the 2.6.2 hang
fix.

