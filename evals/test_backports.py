#!/usr/bin/env python3
"""evals/test_backports.py — Unit tests for the v2 wins back-ported into v1.

The live A/B (7 rounds) showed the v2 *protocol* loses to v1's on
qwen3-4b-instruct-2507 (13/36 vs 22/35 pass^5 on a verified-clean server),
but several v2 mechanisms are protocol-independent improvements. These
tests cover them in their v1 home:

  * edit_file — 3-tier fuzzy fallback (exact → trailing-whitespace →
    indent-shift), ambiguity as a hard error, closest-region no-match report
  * trim_tool_output — head+tail truncation so stack traces survive
  * read_file — offset/limit paging and a clear directory error

Usage:
    python evals/test_backports.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402

# ---------------------------------------------------------------------------
# trim_tool_output — the tail is where the errors live
# ---------------------------------------------------------------------------

def test_trim_tool_output_keeps_tail() -> None:
    text = "START" + ("x" * 5000) + "TRACEBACK: the real error"
    out = sa.trim_tool_output(text, 1000)
    assert out.startswith("START")
    assert out.endswith("TRACEBACK: the real error"), "tail (errors) must survive"
    assert "omitted" in out


def test_trim_tool_output_passthrough_when_short() -> None:
    assert sa.trim_tool_output("short", 1000) == "short"


def test_trim_text_still_head_only() -> None:
    out = sa.trim_text("A" + "x" * 500, 100)
    assert out.startswith("A")
    assert "truncated" in out


# ---------------------------------------------------------------------------
# edit_file — fuzzy fallback, ambiguity, precise no-match
# ---------------------------------------------------------------------------

def test_edit_file_exact_match_still_works() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.py"
        f.write_text("value = 1\n", encoding="utf-8")
        sa.edit_file_tool(str(f), "value = 1", "value = 2")
        assert f.read_text(encoding="utf-8") == "value = 2\n"


def test_edit_file_fuzzy_trailing_whitespace_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.py"
        # File has trailing spaces the model won't reproduce.
        f.write_text("def go():\n    return 1   \n", encoding="utf-8")
        sa.edit_file_tool(str(f), "    return 1", "    return 2")
        assert "return 2" in f.read_text(encoding="utf-8")


def test_edit_file_fuzzy_indent_shift_fallback() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "a.py"
        f.write_text("def go():\n    return 1\n", encoding="utf-8")
        # Model forgets the indentation entirely.
        sa.edit_file_tool(str(f), "return 1", "return 42")
        assert "    return 42" in f.read_text(encoding="utf-8")


def test_edit_file_ambiguous_is_error_not_guess() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "b.py"
        original = "x = 1\nx = 1\n"
        f.write_text(original, encoding="utf-8")
        raised = False
        try:
            sa.edit_file_tool(str(f), "x = 1", "x = 2")
        except RuntimeError as exc:
            raised = True
            assert "2 locations" in str(exc), exc
        assert raised, "ambiguous edit must raise, never silently patch the first hit"
        assert f.read_text(encoding="utf-8") == original, "file must be untouched"


def test_edit_file_no_match_reports_closest_region() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "c.py"
        f.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        raised = False
        try:
            sa.edit_file_tool(str(f), "    return a * b", "    return a / b")
        except RuntimeError as exc:
            raised = True
            msg = str(exc)
            assert "closest region" in msg and "old_string" in msg, msg
        assert raised, "no-match edit must raise with guidance"


# ---------------------------------------------------------------------------
# read_file — paging and directory handling
# ---------------------------------------------------------------------------

def test_read_file_paging() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "big.txt"
        f.write_text("\n".join(f"line{i}" for i in range(1, 501)), encoding="utf-8")
        out = sa.read_file_tool(str(f), 100000, offset=100, limit=3)
        assert "line100" in out and "line103" not in out
        assert "lines 100-102 of 500" in out


def test_read_file_without_paging_unchanged() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "s.txt"
        f.write_text("hello", encoding="utf-8")
        assert sa.read_file_tool(str(f), 1000).strip() == "hello"


def test_read_file_directory_is_clear_error() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        raised = False
        try:
            sa.read_file_tool(tmp, 1000)
        except RuntimeError as exc:
            raised = True
            assert "directory" in str(exc) and "list_directory" in str(exc), exc
        assert raised, "reading a directory must raise a helpful error"


# ---------------------------------------------------------------------------
# parse_json_object - first-complete-object extraction
# ---------------------------------------------------------------------------

MULTI_ACTION = (
    '{"action":"edit_file","args":{"path":"p.py","old_string":"a","new_string":"b"}},'
    '{"action":"verify_syntax","args":{"path":"p.py"}},'
    '{"action":"run_code","args":{"path":"p.py"}}'
)

FENCED = "```json" + chr(10) + '{"action":"finish","message":"ok"}' + chr(10) + "```"
PROSE_JSON = "Sure, let me look." + chr(10) + '{"action":"list_directory","args":{"path":"."}}'
BRACE_IN_STRING = '{"action":"finish","message":"use {} for dicts"}'


def test_multi_action_response_yields_first_action() -> None:
    """The live failure that killed uc1-t4/t5/t6: the model batches the whole
    edit->verify->run sequence into one response. A greedy brace match spans to
    the last brace and parses as nothing, so the turn made ZERO tool calls."""
    a = sa.parse_agent_action(MULTI_ACTION)
    assert a["action"] == "tool" and a["tool"] == "edit_file", a
    assert a["args"]["old_string"] == "a", a


def test_multi_action_is_parseable_at_all() -> None:
    assert sa.parse_json_object(MULTI_ACTION) is not None, (
        "multi-action responses must not be discarded wholesale"
    )


def test_braces_inside_strings_do_not_confuse_scanner() -> None:
    a = sa.parse_agent_action(BRACE_IN_STRING)
    assert a["action"] == "finish" and "{}" in a["message"], a


def test_single_action_and_fences_still_work() -> None:
    assert sa.parse_agent_action('{"action":"finish","message":"done"}')["message"] == "done"
    assert sa.parse_agent_action(FENCED)["message"] == "ok"


def test_prose_before_json_still_works() -> None:
    a = sa.parse_agent_action(PROSE_JSON)
    assert a["action"] == "tool" and a["tool"] == "list_directory", a


def test_plain_prose_is_still_a_finish() -> None:
    a = sa.parse_agent_action("There is no JSON here at all.")
    assert a["action"] == "finish" and "no JSON" in a["message"]


def test_edit_file_tier4_high_similarity_unique_match() -> None:
    """uc1-t4 live failure: the model reconstructs the line from memory and
    lands ~97% similar to exactly one region — and repeats the same wrong
    string on every retry. Tier 4 applies the intended edit."""
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "p.py"
        f.write_text(
            "def stats(d):\n"
            '    return {"total": total, "count": count, "average": avg}\n',
            encoding="utf-8")
        # Model wrote "cnt" instead of "count" — highly similar, unique.
        sa.edit_file_tool(
            str(f),
            '    return {"total": total, "cnt": count, "average": avg}',
            '    return {"total": total, "count": count, "average": avg, "median": med}',
        )
        assert '"median": med' in f.read_text(encoding="utf-8")


def test_edit_file_tier4_rejects_weak_similarity() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "p.py"
        f.write_text("alpha beta gamma\n", encoding="utf-8")
        raised = False
        try:
            sa.edit_file_tool(str(f), "completely different text here", "x")
        except RuntimeError:
            raised = True
        assert raised, "a weak match must still error, never guess"


def test_edit_file_tier4_rejects_near_ties() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "p.py"
        # Two nearly identical lines — a high match against both is ambiguous.
        f.write_text("    value = compute(a, b, c)\n    value = compute(a, b, d)\n",
                     encoding="utf-8")
        raised = False
        try:
            sa.edit_file_tool(str(f), "    value = compute(a, b, x)", "    value = 0")
        except RuntimeError:
            raised = True
        assert raised, "near-tie candidates must error, never pick one"


TESTS = [
    test_multi_action_response_yields_first_action,
    test_edit_file_tier4_high_similarity_unique_match,
    test_edit_file_tier4_rejects_weak_similarity,
    test_edit_file_tier4_rejects_near_ties,
    test_multi_action_is_parseable_at_all,
    test_braces_inside_strings_do_not_confuse_scanner,
    test_single_action_and_fences_still_work,
    test_prose_before_json_still_works,
    test_plain_prose_is_still_a_finish,
    test_trim_tool_output_keeps_tail,
    test_trim_tool_output_passthrough_when_short,
    test_trim_text_still_head_only,
    test_edit_file_exact_match_still_works,
    test_edit_file_fuzzy_trailing_whitespace_fallback,
    test_edit_file_fuzzy_indent_shift_fallback,
    test_edit_file_ambiguous_is_error_not_guess,
    test_edit_file_no_match_reports_closest_region,
    test_read_file_paging,
    test_read_file_without_paging_unchanged,
    test_read_file_directory_is_clear_error,
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
    print(f"\nevals/test_backports.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
