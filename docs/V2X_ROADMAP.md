# Hex CLI v2.x Roadmap

Written 2026-08-13, after the v2.0.0 release and the final audit (V2_PLAN
§14.16). This document is the successor to V2_PLAN §12: it carries forward
everything from the v2 plan that is still worth doing, drops everything that
was shipped or measured-and-rejected, and sequences the rest.

Ground rules carried over from v2, non-negotiable:

- **Nothing ships on intuition.** Every capability change runs the instrument
  (pass^k, fresh server per arm, `--set` A/B, triage before belief).
- **The rejected list stays rejected** without new external evidence: v2
  protocol as default, prompt trimming / conditional rules, tool
  consolidation, 8K bundle, 8B escalation, `<tool_call>` template, CPU-only
  runtime, GenieX qairt on this machine (issue #1266 open), Rust rewrite.
- **The model is frozen** at qwen3-4b-instruct-2507 until a supported
  successor bundle exists (checked 2026-08-13: Qwen3.5 still ships only
  0.8B/2B, llamacpp-only).

## Where each v2-plan item stands

| Plan item (V2_PLAN §) | Status | Disposition |
|---|---|---|
| Eval instrument (§8) | shipped | done |
| Edit formats, fuzzy apply, retry-with-feedback (§5.1) | shipped (as 4-tier fuzzy + unconditional retry) | done |
| Native `<tool_call>` template (§5.1) | impossible (detokenizer) | dead |
| Tool consolidation 15→8 (§5.2) | measured wrong target (~200 tok) | dead |
| Persistent shell session (§5.2) | deferred, unproven at 4B | **v2.4** |
| Plan ledger + recitation (§5.3) | deferred, unproven at 4B | **v2.4** |
| Verification-gated finish, loop detector (§5.3) | shipped | done |
| Escalation ladder (§5.4) | shipped, off by default (no viable bigger local model) | dormant until model lever moves |
| ≤800-token stable prefix (§6.1) | trimming rejected; **prefix byte-stability itself still unmeasured and is the precondition for any KV reuse** | **v2.3** |
| Compaction on real token counts (§6.2) | shipped (estimator) | done |
| Memory v2: files + ripgrep (§6.4) | deferred | **v2.4** |
| I/O hygiene: JSONL history, git-state cache (§6.5) | not done, cheap | **v2.2** |
| AST-based command classifier, deny>ask>allow policy rules (§7.1) | partially covered by sensitive tier; AST + policy-file layer not built | **v2.4** |
| Workspace write scoping, network deny (§7.2) | shipped | done |
| Git-snapshot undo (§7.3) | deferred | **v2.4** |
| Runtime: usage/max_tokens/stop fixes (§9.2) | shipped in fork | done |
| KV experiments: Rewind on non-Qwen3 bundle, GenieDialog_save/restore (§9.3) | **never run** | **v2.3** |
| GenieX bake-off (§9.4) | run; halted on this machine (0.74 tok/s, #1266) | watch item |
| **Nexa SDK evaluation (§9.4)** | **never run** — claims JSON-schema function calling on NPU | **v2.3** |
| Speculative decoding spike (§9.5) | never run | **v2.3 (strictly time-boxed)** |
| Streaming, cancellation, /stats, doctor (§10) | shipped | done |
| Mid-run steering (queue message) (§10) | not done | **v2.4** |
| Background commands (§10) | deferred | **v2.4** |
| External yardsticks: BFCL / Aider subset (§8.8) | not done | **v2.5 (optional)** |
| Packaging / setup surface (§11) | not done — the user-facing gap | **v2.1** |

Plus debt found in the audit, not in the original plan: ghost config keys
(`require_verification`, `autopilot_system_prompt`), `workspace_write_allow`
not settable via `/config` (no list coercion), dead code
(`stream_delay_ms`, `_MOCK_EVAL_COUNT`), `close_session_shell` never called
(shell leak), `LocalEscalator.stop` never called (orphan server), v2 loop
ignoring `shell_exe` and using a 60s timeout, agent.py split at 2 of ~8
stages.

---

## v2.1 — Ship it to people (packaging + product surface)

The biggest gap between "excellent project" and "usable product" is that
installing Hex CLI is a ritual: QAIRT SDK download, npurun build, env vars,
model pull. Nobody but the author has ever run it.

1. **Installer / bootstrap** (the headline). One command that: checks the
   machine (ARM64, NPU driver, disk), guides the QAIRT SDK download (cannot
   be redistributed — link + verify), installs a prebuilt npurun.exe (build
   it in CI on the fork), pulls the model, writes env config, runs
   `--doctor`. PyInstaller or pipx for the Python side.
2. **`/setup` wizard** — first-run interactive config: workspace scope,
   network policy, destructive-command policy, history location.
3. **One-shot / pipe mode hardening** — `echo "task" | hexcli`, `hexcli -c
   "task"`; builds on the non-interactive consent fix (a15ac97). This is what
   makes it scriptable.
4. **Custom slash commands** — user-defined prompt templates in
   `~/.shellai/commands/`.
5. **Session search** — grep over saved sessions from the REPL.
6. **Debt sweep**: ghost keys into DEFAULT_CONFIG or removed; list coercion
   for `/config workspace_write_allow`; dead code out; `close_session_shell`
   and `LocalEscalator.stop` actually called; v2-loop `shell_exe`/timeout
   parity or formal demotion of protocol v2 to research-only.

*Exit gate: a fresh Windows-on-ARM machine goes from zero to a working agent
in ≤3 commands, `--doctor` green. CI covers the installer's non-download
steps.*

## v2.2 — Codebase health (the split, stages 3–8)

agent.py is ~3,300 lines. The split plan and its hazard playbook
(monkeypatch vacuity — see project memory / §14.16) already exist; remaining
stages in order of increasing risk: parsing → backends → tools →
config/`_ACTIVE_CONFIG` (hardest) → repl → loop.

- One stage per PR, mutation-tested (neutralize the moved thing, confirm the
  suite fails, restore).
- Fold in §6.5 I/O hygiene while touching the relevant code: append-only
  JSONL history/telemetry, git-state caching.

*Exit gate: no module over ~800 lines; suite green with zero vacuous patches
(each stage's patch sites verified by mutation).*

## v2.3 — Latency: kill the per-turn prefill tax (time-boxed spikes)

The system prompt costs ~3s of prefill every turn because nothing reuses KV
state. This is the single largest remaining UX lever and it is entirely
runtime work. Three spikes, each time-boxed to a day or two:

1. **Rewind on a non-Qwen3 bundle** (Llama-3.2-3B): isolates whether the
   Rewind failure is the qwen3 bundle or Genie itself. If Rewind works
   elsewhere, a future bundle choice could unlock it.
2. **`GenieDialog_save/restore` warm restarts**: even session-resume-only
   reuse removes the first-turn prefill for reopened sessions.
3. **Nexa SDK bake-off** (never evaluated; the only §9 option still
   untested): gates from the original plan — real cross-turn KV prefix
   reuse (benchmark, don't trust claims), JSON-schema enforcement that is
   actual logit masking, Qwen3-4B parity at ≥15 tok/s. Two of three →
   migration candidate.
4. **Prefix byte-stability audit** (cheap, do first): assert the serialized
   prompt prefix is byte-identical across turns of a session — the
   precondition for every caching mechanism, including a future runtime.
5. **Speculative decoding spike** (Qwen3-0.6B draft via Genie EAGLE path):
   upside 15 → ~25-35 tok/s decode; no public X Elite recipe; hard time-box.

*Exit gate: one mechanism demonstrates warm-turn prefill <1s — or every
mechanism has a written dead-end verdict with numbers, closing §9 for good.*

## v2.4 — Capability experiments at 4B (each behind a flag, each A/B'd)

The deferred features, revived one at a time under the v2 discipline: build
behind a config flag, A/B on the instrument, keep only winners.

- **Plan ledger + recitation** (multiturn suite is the yardstick — targets
  goal drift, the uc1 failure class).
- **Memory v2**: files + ripgrep; retire or demote the vector store; memory
  writes harness-triggered at session end.
- **Git-snapshot undo** (shadow-ref commit per mutation) replacing file-copy
  snapshots.
- **Background commands + mid-run steering** (queue a message at the next
  turn boundary).
- **Persistent shell session** (cd/env/venv survive across commands) — the
  riskiest at 4B (hidden state confuses small models); A/B against the
  agentic suite before keeping.
- **AST-based command classifier + deny>ask>allow policy files** — the last
  unimplemented layer of Safety v2; the sensitive tier covers today's known
  payloads, the AST layer covers the unknown ones.

*Exit gate per feature: pass^5 non-regression on the full suite, win on its
target cases, or it reverts to off/removed with the numbers recorded.*

## v2.5 — Event-driven (watch items, no schedule)

Do these when the world changes, not before:

- **Qwen3.5-4B (or any successor) NPU bundle appears** → run the instrument
  on it that afternoon. The two failure classes it must beat: bait
  compliance (trap cases), deep-context edit quality (uc1-t5/t6).
- **GenieX #1266 resolves** (maintainer engaged 2026-08-10, cannot repro) or
  an NPU driver/GenieX release notes a perf fix → re-run the qairt
  migration checklist; the prize is KeepCache + exact usage reporting.
- **A Windows-viable fine-tune→bundle path appears** → LoRA the 14 rules
  into the weights; frees ~1,459 prompt tokens *and* the latency tax.
- **External yardsticks** (BFCL subset, Aider-polyglot subset against the
  local endpoint) — optional credibility measurement, dev-machine-only.

---

## Sequencing rationale

v2.1 first because the product's limiting factor is now adoption, not
quality: v2.0 beat its gates, but no second human can run it. v2.2 before
the experiment phases because every v2.4 feature touches the loop, and the
split makes each subsequent change cheaper and safer. v2.3 before v2.4
because a <1s warm turn changes the economics of every capability feature
(recitation and plan ledgers cost tokens; cheap prefill makes them nearly
free). v2.4 is deliberately last among the scheduled phases: it is the only
speculative one, and the v2 lesson is that speculative work must queue
behind the instrument, not ahead of it.
