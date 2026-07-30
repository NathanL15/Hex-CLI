# Hex CLI v2 — Comprehensive Revamp Plan

> **Status update, 2026-07-30 (after implementation round 1).** Phases 0 and 1
> are built and measured. The headline finding overturns §5 of this plan:
> **the v2 protocol lost the A/B and was not adopted; its protocol-independent
> mechanisms were back-ported into v1 instead.** See
> [§14 Implementation findings](#14-implementation-findings-2026-07-30) for the
> data, which supersedes the v2-protocol recommendations below. Everything in
> §8 (eval methodology) held up and is now shipped.

*Prepared 2026-07-29. Grounded in: the full v1.7.0 audit (330/330 offline tests, live NPU evals), a fresh live re-verification run, direct speed measurements on the Hexagon NPU, a deep architectural review of the v1 codebase and eval suites, and web research into the mid-2026 model/runtime/agent-design landscape. Every load-bearing number below was measured on this machine or verified against a cited source; unverified claims are flagged.*

---

## 1. Executive summary

v1 proved the concept: a fully-offline terminal agent on the Hexagon NPU that passes its smoke suite 9/9 and holds up CI. The audit exposed exactly where the ceiling is, and it is **not primarily the model**:

- **The harness starts every turn at the model's degradation cliff.** The system prompt alone is ~2,100 tokens (the code believes it's ~1,000), the model degrades near ~2,600 input tokens, and the prompt is *unstable across turns* (date, cwd, keyword-conditional tool schemas) so no caching scheme could ever reuse it.
- **Every turn re-prefills the entire conversation** (measured: ~700–790 tok/s prefill, α≈2.22 superlinear multi-turn latency, 5s → 78s by turn 5). KV-cache Rewind is confirmed broken on the current model bundle, and the compiled bundle has a hard **4,096-token context wall**.
- **The #1 failure class (JSON escaping on multi-line edits) is a format-design bug, not a model bug.** Putting file content inside JSON strings is something every serious 2026 coding agent has abandoned.
- **The eval suite that produced v1's quality numbers has validity holes** (harness prompt ≠ production prompt, single-run grading of a stochastic model, string-match verifiers, latency measurements confounded by retries). It found real bugs, but it cannot measure whether v2 is better.

**The v2 thesis: keep the 4B-class model, rebuild the harness around it.** Concretely: (1) a state-based pass^k eval suite *first*, so improvements are measurable; (2) an edit format and tool-call protocol the model can actually emit reliably; (3) a byte-stable prompt prefix, append-only history, and an 8K-context recompiled bundle to break the latency curve; (4) a dual-model setup — Qwen3-4B-Instruct-2507 for the fast path, Qwen3-4B-Thinking-2507 for planning/retries (+9.3 BFCL points, same size, same speed); (5) a deterministic safety layer that assumes the model is 100% injectable; (6) streaming, plan ledgers, and verification-gated `finish` for reliability and UX.

---

## 2. Where v1 actually stands (measured, 2026-07-29)

### 2.1 Verified performance numbers

| Metric | Value | Source |
|---|---|---|
| Decode speed | ~15–17 tok/s | `npurun bench` (14.9), fresh cap-probe (~17), independent paper (14–15) |
| Prefill speed | **~700 tok/s** (measured today); ~787 tok/s independent | Prefill sweep: 0.91s→4.66s across ~30→2,900-token prompts, slope 0.040 s per ~28-token block; [on-device RAG paper](https://arxiv.org/html/2606.11257v1) |
| TTFT (short prompt) | ~0.5–1.1s | rewind_bench.py + today's warmup |
| System prompt size | ~2,100 est. tokens (~8.4 KB formatted) | Code review (v1 code *claims* ~1,000 at agent.py:2619) |
| Multi-turn latency scaling | Superlinear, α≈2.22 (5s → ~78s by turn 5) | multiturn.py live run (exponent inflated by retry confound — see §8, but the re-prefill cost is real) |
| Compiled context window | **4,096 tokens** (hard wall) | qualcomm/Qwen3-4B bundle spec; npurun issue #13 |
| Live smoke suite (today) | 9/9 functionally correct; agentic 1st-latency 5.3–6.4s | Fresh harness re-run at HEAD |
| Extended adversarial (audit) | 6 hard fails / 35 cases, single-run | v1.7.0 audit |
| Trap-bait compliance | ~1 in 3 (stochastic) | Documented + reproduced |
| RAM budget | **16 GB total** (≈15.6 GiB usable) | Measured. Hard ceiling on model choices. |

Two fresh findings from today's probes: npurun **ignores `max_tokens`** and appears to enforce a ~60s generation wall-clock cap (both 64- and 256-cap essay requests ran 60.05s and returned 4,200+ chars with `finish_reason: "stop"`), and it **returns no `usage` field**, which forces all token accounting to chars/4 estimates — both worth fixing at the server for eval integrity alone.

### 2.2 Root causes behind the six audit hard-fails

| Failure | Real root cause | v2 fix |
|---|---|---|
| Multi-line edit corruption (self-correct-1: literal `\` written into file) | File content inside JSON strings; exact-whole-string `edit_file` with no fuzz, no uniqueness check (agent.py:1409–1425) | §5.1 SEARCH/REPLACE format |
| 9-edit retry spiral (runtime-correct-1) | Error-loop detector only trips on 3 *identical* (tool, output) pairs (agent.py:2496); slightly-varying failures never trip it | §5.4 fuzzy loop detection + verification gates |
| Memory recall fail (memory-1) | Partly a **harness artifact**: eval prompt omitted the `search_memory` schema entirely (conditional injection keyed on query keywords; eval built the prompt without the query) | §8 eval v2; §5.2 always-visible tools |
| Trap compliance, injections | 4B instruction-following ceiling; no deterministic backstop | §7 harness-enforced safety + thinking-model escalation |
| Multi-turn collapse at t4–t6 (0 tools, JSON failures) | Input at 2,500–2,800 tokens = past the model cliff; stale compaction thresholds fire too late (agent.py:2619–2623) | §6 context budget rebuild |
| Latency blowup | Full re-prefill of an unstable ~2.1K prefix + growing history every turn | §6 stable prefix + 8K bundle + KV experiments |

### 2.3 What v1 already has (don't rebuild)

Session resume, checkpoints, undo with file restore, auto-compaction, project/global memory + idle consolidation, safety confirm + JSONL audit log, opt-in cloud escalation with redaction, read-only `batch`, `delegate` sub-agent, `run_code` sandbox, telemetry, Esc cancel, per-project config, advisory lock, self-update, mock backend + 330-test offline CI (green).

---

## 3. Hardware & runtime constraints (fixed inputs to every decision)

- **Snapdragon X Elite, Windows 11 ARM64, 16 GB RAM.** Decode is memory-bandwidth-bound (~135 GB/s shared): a 4B w4a16 (~2.5 GB weights) tops out ~15–17 tok/s at the stack's measured ~30% bandwidth efficiency; an 8B would run **~8–9 tok/s**. Prefill is compute-bound and the NPU's strength (~700–790 tok/s for 4B, ~18× CPU).
- **Latency identity to internalize:** every 150 output tokens ≈ 10 seconds of user waiting; every 1,000 context tokens ≈ 1.4s of prefill *per turn* until KV reuse exists. Terse formats are UX features, not style preferences.
- **KV Rewind is broken on the qwen3-4b bundle** (always `ERROR_QUERY_FAILED`, June 2026 testing; local server code explicitly always resets the dialog). Whether *other* bundles (Llama-3.2-3B, Phi-4-mini) accept Rewind is untested — a headline v2 experiment.
- **Genie SDK (QAIRT 2.47, current as of mid-2026) has unexposed capabilities we can use:** `GenieDialog_save/restore` (KV persistence to disk → warm session restarts), CPU-draft + NPU-target **speculative decoding** (shipped example configs), `kv-quantization`, LoRA, and long-context compilation (a 32K example config exists; 8K–16K Qwen3-4B bundles are buildable via the [llm_on_genie tutorial](https://github.com/qualcomm/ai-hub-apps/tree/main/tutorials/llm_on_genie)).
- **The local npurun is effectively a private fork**: ~260 uncommitted lines (stop sequences, abort lifecycle, session-cache scaffolding); upstream is single-maintainer with zero new commits since May 2026.

---

## 4. Model plan

### 4.1 Primary: stay 4B, go dual-mode

**Qwen3-4B-Instruct-2507 (fast path — current, keep) + Qwen3-4B-Thinking-2507 (escalation path — new, self-compile w4a16).**

Rationale (from the mid-2026 landscape survey):
- The Thinking variant of the *same 4B model* scores **BFCL-v3 71.2 vs 61.9, IFEval 87.4, TAU2-Airline 58.0** — the single largest verified quality jump available on this hardware, at identical size (~2.5 GB), speed, and license (Apache 2.0). It directly targets v1's worst behaviors: trap-baiting (reasoning spots the trap), error recovery (re-reads the error before retrying), and planning.
- Thinking is only affordable if **budgeted**: 500–3,000 thinking tokens at 15 tok/s = 30s–3+min. Policy: Instruct handles routine tool calls; Thinking is invoked for plan generation, retry-after-failure, and suspicious-input turns, with a hard ~512–1,024 thinking-token cap. (The 2507 releases split thinking into a separate model — no `/no_think` switch — so this is a two-bundle setup, ~5 GB total on disk, loaded one at a time under 16 GB RAM.)
- Thinking-2507 is **not precompiled on AI Hub** — but it's architecturally identical to Qwen3-4B, so the documented w4a16 export path applies. This is a real build task, not a download.

### 4.2 Explicitly rejected / deferred

| Option | Verdict | Why |
|---|---|---|
| Qwen3-8B (precompiled, AI Hub) | Fallback only | ~8–9 tok/s decode, ~4.8 GB, and 2026 evals rank it *below* 4B-2507 on agentic tasks. Parameters are the wrong place to spend the latency budget. |
| Qwen3.5 Small (4B/9B, Mar 2026) | **Watchlist for v2.x** | Strongest sub-10B family on paper (262K ctx, hybrid thinking), but its DeltaNet architecture has no mature Hexagon kernel — community NPU decode is ~2 tok/s vs 64 tok/s CPU. Only a 2B GenieX bundle exists. Revisit when AI Hub ships 4B+. |
| Qwen3-30B-A3B / Nemotron 30B-A3B as local "architect" | **Infeasible** | ~16–17 GB at 4-bit vs 16 GB total RAM. (One research thread assumed 32 GB — corrected.) |
| Phi-4-mini | Secondary fallback | Tied-#1 on a Feb 2026 local tool-calling bench, MIT, precompiled — a good format-robustness insurance policy, weaker general agent. |
| Speculative decoding (Qwen3-0.6B draft) | **v2.x experiment** | Genie has an EAGLE-style path and CPU-draft example configs, but no shipped X Elite recipe, and npurun doesn't expose it. Potential 1.5–2.5× decode (15 → ~25–35 tok/s). |

### 4.3 Auxiliary models

- **EmbeddingGemma-300M** for memory embeddings — best sub-500M embedder, validated *on this exact NPU* at 3,325 tok/s and 12× energy efficiency vs CPU; replaces MiniLM-L6-v2 if embedding memory survives the v2 memory redesign (§6.4 — grep-first may make this optional).
- **Qwen3Guard-0.6B** (or Granite Guardian small) as a cheap prefilter over *tool outputs* before they re-enter context — a direct, deterministic-ish mitigation for prompt injection via file contents / command output. P2; measure added latency first.

---

## 5. Agent architecture revamp

### 5.1 Edit & output formats (P0 — kills the #1 failure class)

- **No file content inside JSON, ever.** `edit` takes a plain-text body with `<<<<<<< SEARCH / ======= / >>>>>>> REPLACE` blocks (Aider-style — best-performing edit format across models; unified diffs are *worst*, line-number math is hard for LLMs). `write` takes filename + fenced block. Whole-file rewrite allowed as fallback for files under ~100 lines — at 4B, trading tokens for reliability is often correct.
- **Fuzzy apply + precise errors:** strip-trailing-whitespace match, then whitespace-insensitive match; on failure return the closest region with a line-anchored diff of the mismatch ("SEARCH block not found; nearest match lines 40–46 differs in indentation") — that error *is* the retry signal. Uniqueness check before replacing (v1 silently patches first occurrence).
- **Tool calls move to Qwen3's native Hermes-style `<tool_call>` template** (the format the model was post-trained on) instead of the bespoke raw-JSON protocol. Community evidence: switching to the model-matched template "fixed 80% of tool call failures" (secondary source — validate with our own eval).
- **Two-stage output: free-text thought first, then the action block.** Constraining or front-loading structure onto reasoning measurably hurts small models ("format tax"). If the runtime ever exposes grammar-constrained sampling (§9), constrain *only* the action block.
- **Retry-with-error-feedback on every parse/apply failure** (v1 only retries when the malformed text happens to contain a tool-name substring and step < 3 — agent.py:2417). Cap 2–3 attempts, keep the failed attempt *in context* (models need the evidence to adapt), make feedback specific.

### 5.2 Tool suite: 15 → ~8, always visible

Consolidate: `bash` (absorbs run_command/run_code/list/find; persistent session — see below), `read` (with offset/limit paging — v1 can't page files at all), `write`, `edit`, `grep`, `plan` (todo ledger), `finish`, plus safety-gated `fetch_url`. Kill the keyword-conditional schema injection (agent.py:269–319) — it destabilizes the prefix and made tools invisible in both production edge-cases and the eval. Sub-4B models do best with few, sharply distinct tools; overlap (read/list/search/find; run_command/run_code) is a known small-model failure amplifier. Keep 1–2 worked exemplars *only* for the tricky formats (edit), not all tools.

- **Persistent shell session:** v1 spawns fresh `powershell.exe -Command` per call — `cd`, env vars, venv activation all evaporate. v2 keeps one PowerShell process per session (stdin/stdout pipe), with background-run support for long commands (test suites, builds) and Job-Object process-tree kill on cancel.
- **`batch` dropped; `delegate` narrowed** to one pattern: a read-only search/summarize worker that keeps large file dumps out of the main context and returns a ≤1–2K digest. No nested delegation, no parallel sub-agents — at 4B, handoffs compound errors, and the NPU serializes inference anyway.

### 5.3 Loop design: ReAct + persisted plan ledger

Keep the single-threaded, one-action-per-turn loop (correct for this scale — parallel tool calling is a frontier-model pattern). Add:
- **Plan-first for multi-step tasks:** generate an explicit plan with the Thinking model, persist it as a JSON ledger with status fields (models corrupt JSON checklists less than Markdown), execute with the Instruct model. Harness re-injects current plan state after tool results ("recitation") — proven antidote to small-model goal drift.
- **Verification-gated `finish`:** the harness *rejects* `finish` unless verification has run since the last file mutation (syntax check → lint → seeded tests where present → "does the file now contain X"). This is the highest-ROI reliability feature against premature "task complete!" declarations and the self-correction failures.
- **Fuzzy error-loop detection:** trip on N similar (same tool + same target + failure) events, not identical tuples; on trip → escalate to Thinking model with a capped budget → then offer cloud/user. Escalation must not depend on interactive `input()` (v1's does — unusable in one-shot/CI).

### 5.4 Escalation ladder

`Instruct-4B (routine) → Thinking-4B, capped budget (plan / retry / suspicious input) → cloud (existing opt-in redacted path) → user`. Triggers: verification failure ×2, loop detector, plan rejection, safety-classifier "suspicious" verdict.

---

## 6. Context & memory engineering (the latency fix)

### 6.1 Prompt layout — byte-stable prefix, append-only history

```
[static system core: identity, loop protocol, ~8 tool specs, safety rules — target ≤800 tokens, IDENTICAL bytes every turn of a session]
[static per-session context block: cwd, project type, git branch, date — rendered ONCE at session start]
[conversation: append-only; tool outputs appended verbatim-then-capped; never edit/reorder past messages mid-session]
[dynamic tail: plan-ledger recitation, current-turn user message]
```

Everything that varies per-turn moves to the *end*. No timestamps/counters in the prefix, deterministic serialization order. This matters even before any KV reuse exists (a stable prefix is the precondition for *every* caching mechanism, including a future runtime swap) and it shrinks per-turn prefill immediately: ≤800-token core vs ~2,100 today saves ~1.9s/turn at measured prefill speed — and more importantly pulls turn-1 input well below the ~2,600-token model cliff.

### 6.2 Budgets (against an 8K recompiled bundle — §9)

System core + session block ≤1K · live history ~4–5K · compaction trigger at ~70% of window · tool-output cap ~1–2K tokens at ingest with **head+tail** sampling (v1 truncates head-only, hiding trailing stack traces — agent.py:1150) · `read` returns ranges, not whole files. Compact at *boundaries* (batch, not per-turn) into a structured summary + ledger, since any rewrite of history costs one full re-prefill (~5–10s) — do it rarely and deliberately. Replace all chars/4 estimates with real tokenizer counts (ship the Qwen tokenizer; fix `/context` honesty).

### 6.3 Latency budget after the rebuild (no KV reuse assumed)

Turn cost ≈ (context tokens ÷ 700) + (output tokens ÷ 15). At a steady-state ~3K context: ~4.3s prefill + ~3–7s for a terse action = **7–12s/turn flat**, vs v1's 5→78s curve. If any KV-reuse experiment lands (§9), prefill drops to (new tokens ÷ 700) ≈ <1s and decode becomes the bottleneck — which the terse formats already optimize for.

### 6.4 Memory v2: files first, embeddings second

2026 consensus for coding agents: file-based memory + agentic grep won; embedding RAG lost (staleness/contradiction problems, and at 4B the model — not retrieval — is the bottleneck). v2: keep the global/project split; make memory a small curated `MEMORY.md` (stable within a session, in the prefix) + per-task scratch files read on demand; `search_memory` becomes **ripgrep-based**; memory *writes* are harness-triggered at session end, not left to the model's initiative. The v1 vector store (which indexes only the first 200 chars of queries and rewrites both store files on every turn — memory.py:223–265) is retired or demoted to an optional EmbeddingGemma-backed layer. The "dreaming" consolidation daemon survives only if its output quality gets an eval (it currently has none).

### 6.5 Harness-side I/O hygiene (secondary but easy)

Per-turn today: 3 git subprocesses, full rewrites of `history.json` + telemetry log + both vector-store files, memory-rules re-read. v2: cache git state (invalidate on `bash` mutations), append-only JSONL for history/telemetry, in-memory vector store with periodic flush — none of it rivals prefill cost, all of it is cheap to fix.

---

## 7. Safety v2 (assume the model is 100% injectable)

Measured reality: ~1/3 trap compliance, 2/3 deep-context injection compliance, and the code layer did nothing (hosts-file read and system-prompt dump both executed). Two independent layers, neither trusting the model:

1. **Approvals (policy):** ordered `deny > ask > allow` rules, first-match-wins, global + per-project, session-scoped "allow this pattern for now" grants. Classify the **parsed PowerShell AST**, not a regex over the string (v1's ~16 regexes are default-allow and miss `Set-Content`, `Move-Item`, `-EncodedCommand`, `python -c`, `Start-Process`…). `run_code`/script execution goes through the same classifier (v1: zero classification).
2. **Enforcement (sandbox):** workspace-scoped write enforcement in the harness (canonicalize paths; deny writes outside project root + scratchpad), **network deny-by-default** (offline is the product — enforce it, don't assume it), per-command preview-before-approve (exact command + cwd + classification). Stretch: spawn commands under a Windows restricted token (the Codex CLI reference design); WSL2 sandboxing as an alternative if ever acceptable.
3. Keep: append-only JSONL audit log. Upgrade: **git-snapshot-based undo** (commit-per-mutation on a shadow ref) replacing file-copy snapshots. Add: tool-output injection prefilter (§4.3) as a P2.

---

## 8. Eval suite v2 (build this FIRST — nothing else is measurable without it)

The audit's verdict on v1's live evals: real signal, unsound instrument. Specific defects to fix (all confirmed at file:line in the review):

1. **Prompt parity:** evals build the system prompt without the query, so conditional tool schemas never appear (memory-1's FAIL was partly this artifact). v2 evals drive **`run_autopilot` itself** — one code path, guaranteed parity, and the loop detector/compaction/undo paths finally get live coverage (today they're never exercised by any eval).
2. **Statistics:** ≥5 runs per behavioral case at *production* sampling settings (Qwen3 explicitly discourages greedy decoding); report pass@5 (capability) and **pass^5 (reliability)** — the metric that says whether a harness change actually helped. Single-run verdicts on a ~1/3-stochastic behavior are coin flips.
3. **Grading:** every case gets a binary verdict from **state assertions + answer-content assertions** (run a `verify.ps1` in the fixture dir; JSON-parse produced files; assert the ground-truth fact appears in the final message). Retire: digit-scraping (`_verify_summary_txt` concatenates all digits — "3 files (2 .txt)" grades as "32"), substring passes (`"2" in message`), and any-tool-call-counts-as-recovery checks.
4. **Adversarial grading inspects command *content*** (what was passed to `run_command`/`bash`), not tool names — uc3-t9 was graded RESISTED while it ran `run_command` with uninspected content.
5. **Real error injection:** make the sandbox actually enforce failures (read-only ACL, genuinely missing path) — v1 fabricates error strings the sandbox contradicts, so blind identical retries grade as "recovery."
6. **Latency benchmarking separated from correctness:** streamed TTFT + tok/s from server-side `usage` (requires the npurun usage fix — §9), retries counted separately. The α≈2.22 exponent conflated up-to-3 retry calls into "first latency"; re-measure cleanly.
7. **Suite split:** fast deterministic **regression** suite (per-commit, CI, alert on pass^k drops) vs **capability** suite (per-release, hard tasks, expected-fail tracking). Add what's missing entirely: long-horizon multi-file tasks, a real fix-the-failing-test coding task, compaction round-trip fidelity ("answer X after /compact"), token-budget scaling sweep with controlled output length.
8. **External yardsticks (feasible subset):** BFCL against the local endpoint (cheap, directly validates the §5.1 format change), an Aider-polyglot subset (edit-format compliance — the exact weak spot), a curated Terminal-Bench easy subset if WSL2/ARM image friction allows (flagged: many task images are x86). Skip SWE-bench (meaningless at 4B).

---

## 9. Inference runtime plan

**Decision: stay on the npurun fork for v2.0 with app-layer mitigations, while formally evaluating two Qualcomm-backed successors for v2.5.** Ranked reasoning:

| Option | KV reuse | Grammar/JSON schema | Status |
|---|---|---|---|
| **npurun (local fork)** — v2.0 | No (Rewind broken on qwen3 bundle) | No | Working today; single-maintainer upstream, quiet since May; 4K ctx wall until we recompile |
| **GenieX** (github.com/qualcomm/GenieX) — evaluate | Unknown — test | Unknown — test | **Qualcomm-official** community runtime, BSD-3, Windows ARM64, OpenAI-compatible server, same Genie underneath, growing catalog (incl. GGUF-via-llama.cpp path). The strongest candidate to replace a bus-factor-1 fork. |
| **Nexa SDK** (github.com/qualcomm/nexa-sdk) — evaluate | Unverified — benchmark | **Claimed** JSON-schema function calling on NPU | Qualcomm-partnered, day-0 Qwen3-4B Hexagon support. Verify enforcement is real logit masking, not prompt-level. |
| llama.cpp CPU (Oryon) | Yes (prompt cache) | Yes (GBNF) | ~20–30 tok/s decode for 4B q4 (matches NPU!) but ~10–20× slower prefill and 2–4× battery. Useful as an optional **JSON-critical route** and dev fallback; llama.cpp's Hexagon backend is a dead end (FastRPC overhead → slower than CPU). |
| ONNX Runtime GenAI + QNN | Yes | Yes (LLGuidance) | Would require doing Qwen3-4B QNN bring-up ourselves on nightly builds. Too costly; revisit if model coverage lands. |

Concrete runtime work items:
1. **Recompile the model bundle at 8K (target) / 16K (stretch) context** via the llm_on_genie path — removes the 4,096 wall that currently sits *below* v1's own compaction thresholds. Measure prefill cost and memory at 8K before committing to 16K.
2. **Fork hygiene:** commit the ~260 uncommitted lines properly; add server-side `usage` reporting (prompt/completion tokens — eval integrity depends on it), honor `max_tokens`, investigate the ~60s generation cap found today.
3. **KV experiments (time-boxed):** (a) test `Rewind` on a non-Qwen3 bundle (Llama-3.2-3B or Phi-4-mini) to isolate whether the qwen3 bundle is the blocker; (b) prototype `GenieDialog_save/restore` for warm session restarts. Either landing turns the §6.3 budget from ~4s prefill/turn into <1s.
4. **Run the GenieX and Nexa bake-off** with three gates: cross-turn KV prefix reuse (benchmark, don't trust claims), grammar/JSON-schema enforcement strictness, and Qwen3-4B parity at ≥15 tok/s. Two of three → begin migration in v2.5.
5. **Speculative decoding spike (v2.x):** Genie's CPU-draft example configs + Qwen3-0.6B draft. Upside 15 → ~25–35 tok/s; no public X Elite recipe, so strictly time-boxed.

---

## 10. UX & speed polish

- **True token streaming** (v1 renders only a counter, then the full answer — at 15 tok/s users stare at a spinner for 20–90s). Stream the free-text thought immediately; parse the action header incrementally so tool intent renders within ~1–2s.
- **Status honesty:** elapsed time + activity label ("Editing foo.py", "Running pytest…"), real token counts, `/context` backed by the actual tokenizer.
- **Cancellation that actually cancels:** abort the HTTP request server-side, kill child process *trees* (Job Objects), append `[interrupted by user]` to keep the transcript resumable. (v1's Esc abandons threads and can leave the NPU mid-generation — also implicated in the June NPU-disable/BSOD incident.)
- **Mid-run steering:** queue a user message injected at the next turn boundary.
- **Background commands:** long test suites/builds run detached with completion notification into the loop.

---

## 11. Offline completeness

- Enforce network-deny-by-default at the harness (not just "the model is local") — `fetch_url` becomes an explicit approval-gated exception; cloud escalation stays opt-in per event.
- All v2 additions must work with zero network: tokenizer shipped locally, both model bundles pre-pulled, EmbeddingGemma/guard models optional-and-local, eval suites runnable fully offline (external yardsticks like BFCL are dev-machine-only, clearly separated).
- Document the one-time online setup surface honestly: QAIRT SDK download, model pulls, self-compile of the Thinking bundle.

---

## 12. Roadmap

**Phase 0 — Instrument (eval v2 core).** Drive `run_autopilot` from the eval harness; state-based verifiers; ≥5-run pass@5/pass^5; production temperature; regression/capability split; server `usage` + `max_tokens` fixes in the fork. *Exit gate: v1 baseline re-measured on the new instrument (expect numbers to move — that's the point).*

**Phase 1 — Format & prompt rebuild (biggest quality-per-effort).** SEARCH/REPLACE + fenced-write formats with fuzzy apply; native Qwen3 tool-call template; two-stage thought→action output; unconditional retry-with-feedback; stable-prefix append-only prompt (≤800-token core); tool consolidation to ~8 with paging `read` and persistent `bash`; head+tail truncation. *Exit gate: JSON/apply failure rate near zero on the regression suite; pass^5 up on edit-heavy cases; turn-1 prefill <2s.*

**Phase 2 — Context & model.** 8K bundle recompile; compaction rebuilt against real tokenizer counts with boundary-batched rewrites; Thinking-2507 self-compile + escalation ladder + capped thinking budgets; verification-gated `finish`; plan ledger + recitation; fuzzy loop detector. *Exit gate: 10-turn session with flat ≤12s/turn; multiturn collapse case passes; extended-suite hard-fails ≤2 of 6 at pass@5.*

**Phase 3 — Safety & UX.** AST-based classifier + deny>ask>allow + workspace write scoping + network deny; git-snapshot undo; streaming + cancellation + background commands; memory v2 (files + ripgrep). *Exit gate: injection suite graded on command content shows 0 executed injections (harness-blocked regardless of model compliance); streamed TTFT <2s perceived.*

**v2.x experiments (time-boxed, any order):** Rewind on non-Qwen3 bundles; `GenieDialog_save/restore` warm restarts; GenieX/Nexa bake-off → possible runtime migration; speculative decoding spike; EmbeddingGemma memory layer; Qwen3Guard tool-output prefilter; Terminal-Bench subset under WSL2; Qwen3.5-4B/9B when Hexagon support matures.

**Deliberately not doing:** multi-agent orchestration, parallel tool calls, embedding-RAG-first memory, unified-diff edit format, 8B-primary model, temp-0 evals, llama.cpp-Hexagon backend, cross-runtime KV handoff.

---

## 13. Risks & open questions

1. **Thinking-2507 self-compile** is the riskiest single dependency (export toolchain RAM appetite killed a previous attempt on this machine — the AIMET path needed ~40 GB; verify the current llm_on_genie path's requirements *before* Phase 2, or fall back to cloud-compiling via AI Hub).
2. **8K recompile costs**: doubling context roughly doubles worst-case prefill (~11s at full window) and grows KV memory; measure at 8K before considering 16K.
3. **npurun bus factor**: upstream quiet since May 2026; the GenieX/Nexa bake-off is the hedge, and fork hygiene (committing local work) is the insurance.
4. **Eval re-baselining will change the headline numbers** — the v1 "9/9, 6 hard fails" figures are not comparable to pass^5 measurements. Expect apparent regressions that are actually measurement honesty.
5. **4B ceiling is real**: even a perfect harness won't make trap compliance 0% or multi-line reasoning frontier-grade. The harness absorbs what it can (verification gates, safety enforcement, format design); the escalation ladder handles the rest. Set expectations accordingly in the README.

---

## 14. Implementation findings (2026-07-30)

Phases 0 and 1 are built, measured, and committed. This section records what the
measurements actually showed — including where they contradicted the plan above.

### 14.1 The eval instrument (Phase 0) — shipped, and it worked

`evals/runner.py` + `checks.py` + `cases_*.py` drive the **production**
`run_autopilot` through an `AutopilotProbe` seam, so prompt parity is structural
rather than aspirational. Binary verdicts come from state + content + behaviour
assertions; every case runs N times with pass@k, pass^k, and Wilson intervals.
44 v1 live cases were ported with the audit's grading defects fixed. 22 offline
instrument tests run in CI.

It immediately paid for itself by finding things the old suite could not:

| Finding | How it surfaced |
|---|---|
| **The embedding model was never installed** — `search_memory` and vector indexing were silent no-ops in production | memory-1 marked INVALID instead of "failed", which prompted the check |
| **memory-1 was never a model failure** — 5/5 once the model was installed and prompt parity was real | the v1.7 audit had recorded it as a model-ceiling hard fail |
| **The NPU server degrades under sustained load** (§14.4) | invalid-run classification + a fresh-server probe |
| **A UTF-8 char-boundary panic aborts npurun** | v2's plain-text answers emit emoji; the crash landed within four requests |

### 14.2 v1 baseline, honestly measured

Extended suite, 5 runs/case, production sampling: **23/36 pass^5, 32/36 pass@5.**
Multi-turn: uc1 coding turns collapse from turn 3 (~2,500 est. input tokens),
uc2 routing holds, uc3 injections comply 3/3 (including an executed hosts-file
read), first-response latency 5.9s → 28.9s across a session.

The v1.7 audit's "6 hard fails" and the multi-turn α≈2.22 exponent are **not**
comparable to these numbers: the old instrument graded single runs with string
matching and folded retries into its latency measurement.

### 14.3 The v2 protocol lost the A/B — 7 rounds, then a pivot

| Round | Change | Extended pass^5 |
|---|---|---|
| — | v1 baseline | **22/35** |
| 2 | v2 as designed (`<tool_call>` markers) | 14/36 |
| 3 | payload-before-header, `recall` rename, read-dir redirect | 18/36 |
| 4 | atomic `<write>`/`<edit>` single-block forms | 14/36 |
| 5 | JSON `old_string`/`new_string` fallback, stronger verify nudge | 14/36 |
| 6 | v1's tuned rules ported into the v2 prompt | 10/36 (server degraded) |
| 7 | same, on a **verified-clean** server | **13/36** |

Real bugs were found and fixed along the way — Qwen3's `<tool_call>` tag is a
special token the qwen3-4b w4a16 bundle's **detokenizer garbles** (measured:
`<tool_call>hello</tool_call>` → `Fightinghello trespassing`, while `<action>`
round-trips exactly), payload-after-header is frequently never generated because
the model ends its turn at the block, and `search_memory` gets grabbed by any
"run a search…" phrasing. Each fix landed cleanly and the total never approached
v1.

**Conclusion: v1's protocol is what this model actually knows.** Two months of
`_AUTOPILOT_TEMPLATE` tuning has effectively specialised qwen3-4b-instruct-2507
to that JSON action format; the traces show it reaching for v1 shapes
(`{"name":"edit","arguments":{…,"command":…}}`, `write` with a `content` arg)
no matter what the prompt demonstrates. Each patched symptom reappeared in a
new form. This is a **model-specific** result, not a verdict on SEARCH/REPLACE
or native tool-calling in general — the published evidence for those formats
comes from far more capable models.

### 14.4 The measurement trap (most important process finding)

After ~1–2 hours of continuous eval traffic the npurun/Genie dialog degrades
into sticky `ERROR_QUERY_FAILED (-6)` and returns HTTP 500 for **every**
request. Proof: requests failing 3/4 on the aging server succeeded **12/12** on
a fresh restart. Rounds 4–6 ran against that degradation, so part of their
apparent regression was infrastructure, not behaviour.

Hardening now in the instrument:
1. `is_backend_failure()` classifies 5xx / `URLError` / connection loss /
   `ERROR_QUERY_FAILED` as infrastructure; those runs are **INVALID** and
   excluded from pass^k rather than counted as model failures. (4xx stays a
   real failure — `HTTPError` subclasses `URLError`, so order matters; a test
   pins it.)
2. `backend_preflight()` refuses to start a suite against a dead backend.
3. Six consecutive backend failures abort the suite loudly.
4. The A/B scripts restart the NPU server before each suite.

**Any local-model eval that runs for hours needs this.** Without it a degrading
server silently manufactures regressions that look exactly like real ones.

### 14.5 What shipped instead: v2 wins back-ported into v1

Mechanisms that are protocol-independent moved into the default loop:

- **3-tier fuzzy `edit_file`** (exact → trailing-whitespace → indent-shift),
  ambiguity still a hard error, no-match reports the closest region with line
  numbers as retry feedback — the audit's #1 finding.
- **Head+tail tool-output truncation** so stack traces and exit summaries
  survive (head-only trimming hid exactly what recovery needs).
- **`read_file` paging** (offset/limit) and a helpful directory error.
- **Verification-gated finish** — one nudge after an unverified mutation.

Measured against the v1 baseline (5 runs/case, fresh server): **7 cases
improved, 4 regressed by a single run each (noise), 24 unchanged; pass@5
31/35 → 33/36.** The clearest signal is `runtime-correct-1` 2/5 → 4/5 — exactly
the case the fuzzy applier targets — plus trap-1 0/5 → 2/5.

Multi-turn (3 runs, 0 invalid) is the sharper result:

| Turn | Baseline | +back-ports | 1st-LLM latency |
|---|---|---|---|
| uc1-t3 (fix the bug) | 0/3 | **3/3** | 10.0s → 12.5s |
| uc1-t4/t5/t6 | 0/3 | 0/3 | — |
| uc1-t6 (deepest context) | 0/3 | 0/3 | **28.9s → 15.2s** |
| uc2 (all six) | 17/18 | 17/18 | unchanged |
| uc3 injections | 0/3 | 0/3 | unchanged |

The coding loop's collapse point moved from turn 3 to turn 4: the fuzzy applier
fixes the *edit-apply* failures that were burning the step budget, and latency
at the deep end halves because edits land first try instead of spiralling. What
remains at t4–t6 is the **context cliff** (~2,560+ est. input tokens) — §6's
work, not an edit problem. uc3's injection compliance is unchanged because that
needs §7's deterministic safety layer; no prompt or edit fix will move it.

Also shipped in the npurun fork: `usage` reporting (exact completion tokens),
token-precise `max_tokens`, mid-stream stop-sequence aborts, and the UTF-8
char-boundary crash fix.

### 14.6 Revised recommendations

**Supersedes §5.1–5.2.** Keep v1's JSON action protocol as the default for
qwen3-4b-instruct-2507. `hexcli/protocol_v2.py` + `loop_v2.py` stay in the tree
behind `{"protocol": "v2"}`, fully tested — they are the right harness to
**retest against Qwen3-4B-Thinking** (§4.1) or any bundle whose detokenizer
isn't broken, and that retest is now a one-flag experiment with a trustworthy
instrument behind it.

**Unchanged and still the priority:** §6 context/latency work (the stable-prefix
and compaction rebuild are protocol-independent), §7 safety, §9 runtime
experiments (8K bundle, Rewind on a non-Qwen3 bundle, GenieX/Nexa bake-off),
and §4's dual-model plan.

**New lesson for the roadmap:** prompt *content* and prompt *architecture* are
independent variables, and this model is far more sensitive to the former.
Future work should change one at a time and measure — which the instrument now
makes cheap.
