#!/usr/bin/env python3
"""evals/test_tool_budget.py — the per-step tool-output budget and the
grader fixes of 2026-09-01.

Background: the compiled window is 4,096 tokens. The server drops older
messages to fit but cannot drop part of the newest one, so a single tool
result bigger than the remaining room overflowed the window and the model
returned an EMPTY reply — which the loop then turned into "finish with the
raw tool output as the answer". The configured tool_output_limit (12,000
chars) allowed ~3,000 tokens; the measured breaking point was ~1,800.

Pins: the budget shrinks with context and respects ceiling/floor; read_file
returns a line-aligned first page with a paging header instead of a mid-line
head cut; an empty model reply is retried instead of finishing blank; and the
repaired graders accept correct answers they used to fail.

Usage:
    python evals/test_tool_budget.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from evals import checks as ck  # noqa: E402
from evals.runner import Trace  # noqa: E402

_CFG: dict[str, Any] = {**sa.DEFAULT_CONFIG, "backend": "mock",
                        "memory_enabled": False, "telemetry_enabled": False}


# ---------------------------------------------------------------------------
# Budget math
# ---------------------------------------------------------------------------

def _msgs(chars: int) -> list[dict[str, str]]:
    return [{"role": "system", "content": "x" * chars}]


def test_budget_shrinks_as_context_fills() -> None:
    small = sa._step_tool_output_limit(_CFG, _msgs(2_000))
    large = sa._step_tool_output_limit(_CFG, _msgs(12_000))
    assert small > large, (small, large)


def test_budget_never_exceeds_configured_ceiling() -> None:
    assert sa._step_tool_output_limit(_CFG, []) == _CFG["tool_output_limit"]


def test_budget_never_below_floor() -> None:
    # Context already past the window: the result still gets a usable floor
    # rather than zero chars.
    assert sa._step_tool_output_limit(_CFG, _msgs(40_000)) == sa._TOOL_OUTPUT_MIN_CHARS


def test_budget_at_the_shipped_prompt_size_cannot_overflow() -> None:
    """The regression that motivated this: prompt (~2,355 tok) + a max-size
    tool result must stay under the window with room to answer."""
    prompt_chars = 9_300  # measured autopilot prompt
    limit = sa._step_tool_output_limit(_CFG, _msgs(prompt_chars) + [{"role": "user", "content": "x" * 400}])
    total_tokens = sa._TOKEN_ESTIMATOR.estimate(prompt_chars + 400 + limit)
    assert total_tokens + sa._TOOL_OUTPUT_RESERVE_TOKENS <= _CFG["context_window_tokens"], (limit, total_tokens)
    assert limit < _CFG["tool_output_limit"], "the ceiling alone would overflow — budget must bite"


def test_bigger_window_config_restores_old_behaviour() -> None:
    # The A/B lever: a huge window makes the budget a no-op (ceiling wins).
    cfg = {**_CFG, "context_window_tokens": 100_000}
    assert sa._step_tool_output_limit(cfg, _msgs(12_000)) == _CFG["tool_output_limit"]


# ---------------------------------------------------------------------------
# read_file first page
# ---------------------------------------------------------------------------

def test_read_file_returns_line_aligned_first_page_with_header() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "big.txt"
        p.write_text("\n".join(f"line {i:03d} " + "w" * 40 for i in range(1, 301)), encoding="utf-8")
        old = os.getcwd()
        os.chdir(tmp)
        try:
            sa.set_active_config(dict(_CFG))
            out = sa.read_file_tool("big.txt", 2_000)
        finally:
            sa.set_active_config(None)
            os.chdir(old)
    assert out.startswith("[lines 1-"), out[:60]
    head = out.splitlines()[0]
    assert "of 300." in head and "offset=" in head, head
    body = out.split("\n", 1)[1]
    assert len(body) <= 2_000
    assert body.endswith("w" * 40), "page must end on a whole line, not mid-line"
    assert "truncated" not in out


def test_read_file_small_file_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "small.txt"
        p.write_text("hello\nworld\n", encoding="utf-8")
        old = os.getcwd()
        os.chdir(tmp)
        try:
            sa.set_active_config(dict(_CFG))
            out = sa.read_file_tool("small.txt", 2_000)
        finally:
            sa.set_active_config(None)
            os.chdir(old)
    assert out == "hello\nworld\n"


# ---------------------------------------------------------------------------
# Empty reply is retried, not finished blank
# ---------------------------------------------------------------------------

def test_empty_model_reply_is_retried() -> None:
    sa.set_mock_responses(["", '{"action":"finish","message":"real answer"}'])
    with tempfile.TemporaryDirectory() as tmp:
        old = os.getcwd()
        os.chdir(tmp)
        try:
            out = sa.run_autopilot(dict(_CFG), [], "What is 2+2?", "powershell")
        finally:
            os.chdir(old)
    assert out == "real answer", out
    assert not sa._MOCK_RESPONSE_QUEUE, "the retry must have consumed the second response"


# ---------------------------------------------------------------------------
# Grader regressions (real answers the old graders failed on 2026-09-01)
# ---------------------------------------------------------------------------

def _trace(msg: str) -> Trace:
    t = Trace.__new__(Trace)
    t.final_message = msg
    t.tool_calls = []
    return t


def test_answer_matches_accepts_a_correct_cpu_answer() -> None:
    ok, _ = ck.answer_matches([r"snapdragon|oryon|qualcomm|arm|x1e"], [r"intel|ryzen|core i[3579]"])(
        None, _trace("The machine's CPU is the Snapdragon(R) X Elite - X1E78100 - Qualcomm(R) Oryon(TM) CPU."))
    assert ok


def test_answer_matches_rejects_the_confabulation() -> None:
    ok, _ = ck.answer_matches([r"snapdragon|oryon|qualcomm|arm|x1e"], [r"intel|ryzen|core i[3579]"])(
        None, _trace("Your CPU is an Intel Core i7-13700K."))
    assert not ok


def test_message_has_int_accepts_number_words() -> None:
    assert ck.message_has_int(2)(None, _trace("Out of these, two end in '.txt'."))[0]
    assert not ck.message_has_int(3)(None, _trace("32 files"))[0], "whole-token digit rule must hold"


def test_message_shorter_than_catches_tool_dumps() -> None:
    assert ck.message_shorter_than(1500)(None, _trace("alpha and omega are defined."))[0]
    assert not ck.message_shorter_than(1500)(None, _trace("def alpha():\n    return 1\n" * 100))[0]


TESTS = [
    test_budget_shrinks_as_context_fills,
    test_budget_never_exceeds_configured_ceiling,
    test_budget_never_below_floor,
    test_budget_at_the_shipped_prompt_size_cannot_overflow,
    test_bigger_window_config_restores_old_behaviour,
    test_read_file_returns_line_aligned_first_page_with_header,
    test_read_file_small_file_unchanged,
    test_empty_model_reply_is_retried,
    test_answer_matches_accepts_a_correct_cpu_answer,
    test_answer_matches_rejects_the_confabulation,
    test_message_has_int_accepts_number_words,
    test_message_shorter_than_catches_tool_dumps,
]


def _run(fn) -> bool:
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
    print(f"\nevals/test_tool_budget.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
