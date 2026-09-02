#!/usr/bin/env python3
"""hexcli.compaction — history compression and the context budget, lifted
out of agent.py.

The deterministic merge-aware compactor, the LLM summarizer behind explicit
/compact, and the derived history budget + auto-compact guard.

Cross-cutting names (call_llm, build_autopilot_prompt, estimate_tokens,
sync_session_store, the token estimator, and compact_history itself when
auto-compact fires it) are resolved through the agent module AT CALL TIME —
the same idiom loop_v2 uses — so every existing sa.<name> patch site keeps
intercepting. Module-local calls stay module-local only when nothing patches
them.

Split stage 4 (docs/V2X_ROADMAP.md, "The Split"). Bodies moved verbatim
apart from those hub lookups.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from hexcli.parsing import strip_thinking
from hexcli.prompts import COMPACT_SYSTEM_PROMPT
from hexcli.sessions import touch_session
from hexcli.ui import C, cprint


def _agent():
    from hexcli import agent
    return agent


# Markers for the deterministic compactor's output. Constants because the
# compactor must RECOGNISE its own previous output on re-compaction (see
# below); inline strings in two places would drift apart silently.
_CONDENSED_MARKER = "[Earlier turns, condensed:]"
_CONDENSED_ACK = "Understood — continuing with that context in mind."
_CONDENSED_DROP_RE = re.compile(r"^- \[…(\d+) earlier turn")

# The history budget is derived from the server's INPUT budget
# (`context_window_tokens`: what npurun enforces before it starts dropping
# messages; adopted from /v1/models at the first turn, 3,000 when the server
# does not advertise one), not from a model "cliff". The 2,600-token cliff
# that lived here from July to September was never a length effect —
# V2_PLAN §14.7 records uc1 failing at 2,477 while uc2 passed at 2,911 (a
# regex bug), and the August sweep (evals/cases_cliff.py) found quality flat
# right up to the server's trim. See docs/RESEARCH_NEXT_LEVERS.md §8.
_DEFAULT_INPUT_BUDGET_TOKENS = 3_000
# Reserve for the parts of a turn that are neither system prompt nor history:
# workspace snapshot, the user's query, and the first tool result coming back.
_TURN_OVERHEAD_TOKENS = 500
# Never demand compaction below this — pathological when the prompt is huge.
_MIN_HISTORY_BUDGET_TOKENS = 250
# Auto-compact only fires when its dry run shows at least this much freed.
# Below that, compacting is churn: it rewrites history the model then has to
# re-read, without buying room for the next turn.
_AUTO_COMPACT_MIN_GAIN_TOKENS = 100


def _expand_condensed(content: str) -> tuple[list[str], int]:
    """Split a previous condensed block back into (stub_lines, dropped_count)."""
    lines: list[str] = []
    dropped = 0
    for raw in content.splitlines():
        line = raw.strip()
        if not line.startswith("- "):
            continue  # header/footer markers
        m = _CONDENSED_DROP_RE.match(line)
        if m:
            dropped += int(m.group(1))
            continue
        lines.append(line)
    return lines, dropped


def compact_history_deterministic(
    session: dict[str, Any],
    keep_recent: int = 4,
    stub_chars: int = 160,
    total_stub_chars: int = 900,
) -> list[dict[str, str]]:
    """Compact history WITHOUT an LLM call.

    Auto-compact used to summarise via the same 4B model that was already at
    its degradation cliff — the worst possible moment to ask it for a faithful
    summary, and a full extra re-prefill besides (review finding W10). This
    keeps the most recent turns verbatim and reduces older ones to one-line
    stubs: instant, free, and impossible to hallucinate. The LLM summariser
    stays available for explicit /compact, where the user opts into the cost.

    Re-compaction is merge-aware: a previous run's condensed block is expanded
    back into its stub lines instead of being stubbed as an opaque message.
    Before this, every re-compact crushed the whole block into one 160-char
    stub (stubs-of-stubs), so at the 250-token budget floor — where compaction
    fires every couple of turns — older context was destroyed almost
    immediately. Merging also makes the function idempotent: with no new
    messages the output is byte-identical, which is what lets auto-compact
    dry-run it as a thrash guard.
    """
    messages: list[dict[str, str]] = list(session.get("messages", []))
    if len(messages) <= keep_recent + 1:
        return messages

    head, tail = messages[:-keep_recent], messages[-keep_recent:]
    # Build stubs newest-first and stop at the total budget: recent context is
    # worth more than old, and an unbounded stub list just recreates the
    # oversized history we are trying to shed.
    stub_lines: list[str] = []
    used = 0
    dropped = 0

    def _take(line: str) -> None:
        nonlocal used, dropped
        if used + len(line) > total_stub_chars:
            dropped += 1
            return
        stub_lines.append(line)
        used += len(line)

    for m in reversed(head):
        role = m.get("role")
        raw = m.get("content") or ""
        if role == "assistant" and raw.strip() == _CONDENSED_ACK:
            continue  # scaffolding from a previous compaction, not content
        if role == "user" and raw.lstrip().startswith(_CONDENSED_MARKER):
            inner, inner_dropped = _expand_condensed(raw)
            dropped += inner_dropped
            for line in reversed(inner):
                _take(line)
            continue
        text = " ".join(raw.split())
        if not text:
            continue
        who = "You" if role == "user" else "Hex"
        _take(f"- {who}: {text[:stub_chars]}" + ("…" if len(text) > stub_chars else ""))
    stub_lines.reverse()
    if dropped:
        stub_lines.insert(0, f"- […{dropped} earlier turn(s) dropped]")

    new_messages: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (_CONDENSED_MARKER + "\n" + "\n".join(stub_lines)
                        + "\n[Continue from here]"),
        },
        {
            "role": "assistant",
            "content": _CONDENSED_ACK,
        },
        *tail,
    ]
    session["messages"] = new_messages
    session["compact_count"] = session.get("compact_count", 0) + 1
    touch_session(session)
    return new_messages


def compact_history(
    config: dict[str, Any],
    session: dict[str, Any],
    *,
    quiet: bool = False,
) -> list[dict[str, str]]:
    """Summarise the current message history and replace it with a compact version.

    quiet=True suppresses the printed summary (used by auto-compact).
    """
    messages: list[dict[str, str]] = list(session.get("messages", []))
    _COMPACT_KEEP_RECENT = 4
    # Need at least keep_recent+3 messages so that 3+ messages are summarised
    # and removed — otherwise the 2 summary messages + 4 tail can exceed the
    # original count (e.g. 5 msgs → 6 msgs after compact).
    if len(messages) < _COMPACT_KEEP_RECENT + 3:
        print(f"Nothing to compact yet (need at least {_COMPACT_KEEP_RECENT + 3} messages).")
        return messages

    # /no_think disables Qwen3's chain-of-thought block so the token budget
    # goes to the actual summary rather than being consumed by <think> tags.
    summary_messages: list[dict[str, str]] = [
        {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
        *messages,
        {"role": "user", "content": "Produce the compact summary now. /no_think"},
    ]
    compact_tokens = max(512, int(config.get("compact_max_output_tokens", 512)))
    config_with_compact = {**config, "_compact_tokens": compact_tokens}
    summary, _ = _agent().call_llm(config_with_compact, summary_messages,
                                   "_compact_tokens", label="compacting")
    summary = strip_thinking(summary).strip()

    # Keep the last few messages verbatim so in-progress task state survives compaction.
    tail = messages[-_COMPACT_KEEP_RECENT:] if len(messages) > _COMPACT_KEEP_RECENT else []

    new_messages: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (
                "[Conversation compacted. Summary of prior context:]\n\n"
                + summary
                + "\n\n[Continue from here]"
            ),
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context summary and will continue from where we left off.",
        },
        *tail,
    ]
    session["messages"] = new_messages
    session["compact_count"] = session.get("compact_count", 0) + 1
    touch_session(session)

    n_removed = len(messages) - len(new_messages)
    if not quiet:
        cprint(f"\nCompacted: {len(messages)} → {len(new_messages)} messages (removed ~{n_removed}).", C.BCYAN)
        cprint("Summary:", C.BOLD)
        print(summary)
        print()
    return new_messages


def _history_budget_tokens(config: dict[str, Any]) -> tuple[int, int]:
    """Return (warn, critical) history-token thresholds derived from the ACTUAL
    system prompt size and the server's input budget.

    v1.3–v1.7 hardcoded warn=1,300 on a comment claiming the base prompt was
    ~1,000 tokens. It is really ~2,100, so auto-compact fired ~900 tokens PAST
    the degradation cliff — i.e. the safety net never once fired in time, which
    is what made multi-turn coding sessions collapse from turn 4 (see
    docs/V2_PLAN.md §14). Measuring the prompt instead of guessing keeps this
    honest when the prompt changes again.
    """
    override = config.get("context_warn_tokens")
    if override:
        return int(override), int(override) * 5 // 4
    ag = _agent()
    try:
        base = ag.estimate_tokens(ag.build_autopilot_prompt(
            cwd=str(Path.cwd()), max_steps=int(config.get("max_agent_steps", 15)),
        ))
    except Exception:
        base = 2_100
    window = int(config.get("context_window_tokens") or _DEFAULT_INPUT_BUDGET_TOKENS)
    warn = max(_MIN_HISTORY_BUDGET_TOKENS, window - base - _TURN_OVERHEAD_TOKENS)
    return warn, warn * 5 // 4


def context_fill_percent(session: dict[str, Any] | None, config: dict[str, Any]) -> int:
    """How full the history budget is, 0-100: 0 on a fresh session, 100 when
    the next turn will auto-compact. The prompt shows it as a small gauge."""
    msgs = (session or {}).get("messages", []) or []
    if not msgs:
        return 0
    ag = _agent()
    est = ag._TOKEN_ESTIMATOR.estimate(sum(len(m.get("content", "")) for m in msgs))
    warn, _ = _history_budget_tokens(config)
    return max(0, min(100, round(100 * est / max(warn, 1))))


def _maybe_auto_compact(
    config: dict[str, Any],
    session: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> None:
    """Silently compact history when the NEXT turn would cross the 4B
    instruction-following cliff.

    Fires after each autopilot turn. The threshold is derived from the actual
    system-prompt size (see _history_budget_tokens), not a hardcoded guess.
    The full summary is suppressed (quiet=True); only a one-line notice prints.

    Thrash guard: before 2.5.0's window-derived budget, a 2,200-token prompt
    clamped the history budget to the 250-token floor (now ~850 against a
    3,696-token server budget), and the compacted tail usually exceeded it —
    so v2.2 re-fired every single turn, shredding the condensed block a little
    further each time while freeing almost nothing (the user-reported "by the
    time it compacts, it autocompacts again by the next message"). The
    deterministic compactor is instant and idempotent, so dry-run it first and
    fire only when it would actually reclaim meaningful room.
    """
    ag = _agent()
    msgs = session.get("messages", [])
    est = ag._TOKEN_ESTIMATOR.estimate(sum(len(m.get("content", "")) for m in msgs))
    warn_tokens, _ = _history_budget_tokens(config)
    if est < warn_tokens:
        return
    use_llm = bool(config.get("auto_compact_uses_llm", False))
    if not use_llm:
        probe: dict[str, Any] = {"messages": msgs}
        est_after = ag._TOKEN_ESTIMATOR.estimate(sum(
            len(m.get("content", ""))
            for m in compact_history_deterministic(probe)))
        if est - est_after < _AUTO_COMPACT_MIN_GAIN_TOKENS:
            return
    # One line, after the fact, no numbers — token detail lives in /stats.
    # The slow LLM path announces itself first so the pause is explained;
    # the deterministic path is instant and needs no preamble.
    try:
        if use_llm:
            cprint("  Compacting chat history...", C.BCYAN)
            ag.compact_history(config, session, quiet=True)
        else:
            compact_history_deterministic(session)
        ag.sync_session_store(sessions, session)
        cprint("  Chat history compacted.", C.DIM)
    except ag.UserCancelled:
        cprint("  Auto-compact cancelled. Run /compact manually.", C.YELLOW)
    except Exception as exc:  # noqa: BLE001
        cprint(f"  Auto-compact failed ({exc}). Run /compact manually.", C.YELLOW)
