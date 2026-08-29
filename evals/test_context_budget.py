#!/usr/bin/env python3
"""evals/test_context_budget.py — Unit tests for the v1.8 context-budget fix.

Background (docs/V2_PLAN.md §14): v1.3–v1.7 hardcoded an auto-compact warn
threshold of 1,300 history tokens on the belief that the system prompt was
~1,000 tokens. It is really ~2,100, so the safety net fired ~900 tokens PAST
the measured ~2,600-token degradation cliff — i.e. never in time. That is what
made multi-turn coding sessions collapse from turn 4 onward.

These tests pin the two halves of the fix:
  * the budget is DERIVED from the measured prompt, so it stays honest when
    the prompt changes again
  * auto-compact is deterministic (no LLM call), because summarising with a
    model already at its cliff is both slow and unreliable

Usage:
    python evals/test_context_budget.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402

_CFG: dict[str, Any] = {**sa.DEFAULT_CONFIG, "backend": "mock", "memory_enabled": False}


# ---------------------------------------------------------------------------
# Derived budget
# ---------------------------------------------------------------------------

def test_budget_is_derived_not_hardcoded() -> None:
    warn, crit = sa._history_budget_tokens(_CFG)
    base = len(sa.build_autopilot_prompt(cwd=".", max_steps=15)) // 4
    assert crit > warn, (warn, crit)
    # The whole point: warn + base + overhead must land at or under the cliff.
    assert warn + base <= sa._DEGRADATION_CLIFF_TOKENS, (
        f"budget {warn} + prompt {base} = {warn + base} exceeds the "
        f"{sa._DEGRADATION_CLIFF_TOKENS}-token cliff"
    )


def test_budget_never_below_floor() -> None:
    # Even a pathologically huge prompt must leave a usable floor rather than
    # demanding compaction of an empty history.
    # Patch the HEAD, not _AUTOPILOT_TEMPLATE: the rules section is now
    # assembled per turn, so the template constant is no longer what
    # build_autopilot_prompt reads. The head is always included, so a huge head
    # is still a pathologically huge prompt. (Patching the old name would have
    # been a silent no-op — this test caught exactly that when the rules were
    # made conditional.)
    # Patch BOTH sources: with conditional_rules off (the shipped default) the
    # prompt comes from _AUTOPILOT_TEMPLATE, and with it on the rules section
    # is assembled from _AUTOPILOT_HEAD. Patching only one silently stops
    # reaching the prompt if that default ever flips — the assertion below is
    # what turns that into a failure instead of a vacuous pass.
    orig_tpl, orig_head = sa._AUTOPILOT_TEMPLATE, sa._AUTOPILOT_HEAD
    try:
        sa._AUTOPILOT_TEMPLATE = "x" * 40_000
        sa._AUTOPILOT_HEAD = "x" * 40_000
        inflated = len(sa.build_autopilot_prompt(cwd=".", max_steps=15))
        assert inflated > 40_000, "the patch did not reach the built prompt"
        warn, _ = sa._history_budget_tokens(_CFG)
        assert warn == sa._MIN_HISTORY_BUDGET_TOKENS, warn
    finally:
        sa._AUTOPILOT_TEMPLATE, sa._AUTOPILOT_HEAD = orig_tpl, orig_head


def test_budget_respects_explicit_override() -> None:
    warn, crit = sa._history_budget_tokens({**_CFG, "context_warn_tokens": 900})
    assert warn == 900 and crit == 1125, (warn, crit)


def test_old_hardcoded_threshold_would_have_missed_the_cliff() -> None:
    """Regression guard for the actual v1.7 bug."""
    base = len(sa.build_autopilot_prompt(cwd=".", max_steps=15)) // 4
    assert base + 1_300 > sa._DEGRADATION_CLIFF_TOKENS, (
        "if this fails the prompt shrank enough that the old constant would "
        "have been fine — re-derive the story before changing the test"
    )
    warn, _ = sa._history_budget_tokens(_CFG)
    assert warn < 1_300, "the derived budget must be tighter than the old constant"


# ---------------------------------------------------------------------------
# Deterministic compaction
# ---------------------------------------------------------------------------

def _session(n_pairs: int) -> dict[str, Any]:
    msgs: list[dict[str, str]] = []
    for i in range(n_pairs):
        msgs.append({"role": "user", "content": f"question number {i} " + "detail " * 40})
        msgs.append({"role": "assistant", "content": f"answer number {i} " + "words " * 40})
    return {"id": "t", "messages": msgs}


def test_deterministic_compaction_shrinks_and_keeps_tail() -> None:
    s = _session(10)
    before = sum(len(m["content"]) for m in s["messages"])
    last = s["messages"][-1]["content"]
    out = sa.compact_history_deterministic(s)
    after = sum(len(m["content"]) for m in out)
    assert after < before // 2, (before, after)
    assert out[-1]["content"] == last, "most recent turn must survive verbatim"
    assert "Earlier turns, condensed" in out[0]["content"]
    assert s["compact_count"] == 1


def test_deterministic_compaction_makes_no_llm_call() -> None:
    # An empty mock queue would return the "queue exhausted" sentinel; assert
    # the queue is untouched, proving no call happened.
    sa.set_mock_responses(['{"action":"finish","message":"SENTINEL"}'])
    sa.compact_history_deterministic(_session(8))
    assert len(sa._MOCK_RESPONSE_QUEUE) == 1, "compaction must not consume an LLM call"


def test_deterministic_compaction_noop_on_short_history() -> None:
    s = {"id": "t", "messages": [{"role": "user", "content": "hi"}]}
    out = sa.compact_history_deterministic(s)
    assert out == s["messages"]
    assert "compact_count" not in s


def test_deterministic_compaction_preserves_recent_facts() -> None:
    s = _session(8)
    s["messages"][-2]["content"] = "the API key lives in config/secrets.toml"
    out = sa.compact_history_deterministic(s)
    joined = " ".join(m["content"] for m in out)
    assert "config/secrets.toml" in joined, "recent concrete facts must survive"


def test_recompaction_is_idempotent() -> None:
    """Compacting an already-compacted history with no new messages must be a
    byte-identical no-op — the property the auto-compact dry-run guard relies
    on. Before the merge-aware compactor, each pass crushed the condensed
    block into a single 160-char stub (stubs-of-stubs)."""
    s = _session(10)
    sa.compact_history_deterministic(s)
    first = [dict(m) for m in s["messages"]]
    out = sa.compact_history_deterministic(s)
    assert out == first, "re-compaction with no new content must not change history"


def test_condensed_lines_survive_recompaction() -> None:
    """After new turns arrive, re-compaction must carry the previous block's
    stub lines through (newest kept under the budget), not stub the block."""
    s = _session(6)
    sa.compact_history_deterministic(s)
    inner = [ln for ln in s["messages"][0]["content"].splitlines()
             if ln.startswith("- ") and not ln.startswith("- […")]
    assert inner, "setup: first compaction produced no stub lines"
    s["messages"].append({"role": "user", "content": "new question " + "detail " * 80})
    s["messages"].append({"role": "assistant", "content": "new answer " + "words " * 80})
    out = sa.compact_history_deterministic(s)
    block = out[0]["content"]
    assert "[Earlier turns, condensed:]" not in block.split("\n", 1)[1], (
        "previous condensed block was stubbed wholesale instead of merged")
    kept = sum(1 for ln in inner if ln in block)
    assert kept >= len(inner) // 2, (
        f"only {kept}/{len(inner)} previous stub lines survived the merge")


def test_auto_compact_skips_zero_gain_refire() -> None:
    """The thrash case: already at the floor, nothing new to shed → the guard
    must skip instead of rewriting history every turn."""
    s = _session(10)
    sa.compact_history_deterministic(s)
    assert s["compact_count"] == 1
    before = [dict(m) for m in s["messages"]]
    sa._maybe_auto_compact({**_CFG, "context_warn_tokens": 10}, s, [s])
    assert s["compact_count"] == 1, "auto-compact re-fired with nothing to gain"
    assert s["messages"] == before, "guarded auto-compact must not touch history"


def test_auto_compact_fires_again_after_real_growth() -> None:
    """The guard must not wedge compaction shut: once enough new content
    accumulates that compacting frees real room, it fires again."""
    s = _session(10)
    sa.compact_history_deterministic(s)
    for i in range(3):
        s["messages"].append({"role": "user", "content": f"more q{i} " + "detail " * 120})
        s["messages"].append({"role": "assistant", "content": f"more a{i} " + "words " * 120})
    sa._maybe_auto_compact({**_CFG, "context_warn_tokens": 10}, s, [s])
    assert s["compact_count"] == 2, "auto-compact should fire after real growth"


def test_auto_compact_uses_deterministic_path_by_default() -> None:
    s = _session(12)
    sessions = [s]
    sa.set_mock_responses(['{"action":"finish","message":"SENTINEL"}'])
    sa._maybe_auto_compact({**_CFG, "context_warn_tokens": 10}, s, sessions)
    assert len(sa._MOCK_RESPONSE_QUEUE) == 1, "default auto-compact must not call the LLM"
    assert s.get("compact_count") == 1, "auto-compact should have fired"


# ---------------------------------------------------------------------------
# Token estimator — calibrated chars-per-token, replacing blanket chars/4
# ---------------------------------------------------------------------------

def test_estimator_default_matches_chars_over_4() -> None:
    """Until real observations arrive, behaviour is byte-for-byte the old
    estimate — no silent budget shift on a fresh session."""
    est = sa._TokenEstimator()
    assert est.estimate(8000) == 2000


def test_estimator_learns_from_observations() -> None:
    """Code-heavy output at ~3.3 chars/token must pull the estimate UP —
    underestimating tokens is what fires compaction past the cliff."""
    est = sa._TokenEstimator()
    before = est.estimate(8000)
    for _ in range(30):
        est.observe(3300, 1000)
    after = est.estimate(8000)
    assert after > before, "estimate must rise as the observed ratio falls"
    assert abs(est.ratio - 3.3) < 0.1, f"EMA should converge near 3.3, got {est.ratio}"


def test_estimator_clamps_and_rejects_garbage() -> None:
    est = sa._TokenEstimator()
    est.observe(50_000, 100)      # 500 chars/token: broken usage report
    assert est.ratio == 4.0, "implausible sample must be rejected outright"
    est.observe(30, 5)            # too small to be signal
    assert est.ratio == 4.0
    for _ in range(200):
        est.observe(1600, 1000)   # 1.6 — extreme but within the plausible band
    assert est.ratio == 2.5, "the clamp floor must hold against extreme samples"
    for _ in range(200):
        est.observe(8000, 1000)   # 8.0 — the top of the plausible band
    assert est.ratio == 4.5, "the clamp ceiling must hold against extreme samples"


def test_mock_backend_never_feeds_the_estimator() -> None:
    """Fixture eval_counts are fake; a test run must not poison the ratio."""
    before = sa._TOKEN_ESTIMATOR.observations
    sa.set_mock_responses(['{"action":"finish","message":"' + "x" * 400 + '"}'])
    sa.call_llm({**_CFG, "backend": "mock"}, [], "autopilot_max_output_tokens")
    assert sa._TOKEN_ESTIMATOR.observations == before


TESTS = [
    test_estimator_default_matches_chars_over_4,
    test_estimator_learns_from_observations,
    test_estimator_clamps_and_rejects_garbage,
    test_mock_backend_never_feeds_the_estimator,
    test_budget_is_derived_not_hardcoded,
    test_budget_never_below_floor,
    test_budget_respects_explicit_override,
    test_old_hardcoded_threshold_would_have_missed_the_cliff,
    test_deterministic_compaction_shrinks_and_keeps_tail,
    test_deterministic_compaction_makes_no_llm_call,
    test_deterministic_compaction_noop_on_short_history,
    test_deterministic_compaction_preserves_recent_facts,
    test_recompaction_is_idempotent,
    test_condensed_lines_survive_recompaction,
    test_auto_compact_skips_zero_gain_refire,
    test_auto_compact_fires_again_after_real_growth,
    test_auto_compact_uses_deterministic_path_by_default,
]


def _run(fn: Any) -> bool:
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as exc:
        print(f"  FAIL  {fn.__name__}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False


def main() -> int:
    print(f"\nevals/test_context_budget.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
