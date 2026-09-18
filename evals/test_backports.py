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

# Offline suites must never wait on a human at a consent prompt.
sa.ui.CONFIRM_TIMEOUT_S = 0.05
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


BROKEN_WRITE = (
    '{"action":"write_file","args":{"path":"calc.html","content":"<button onclick=\\"clear()\\">C</button>'
    '<button onclick=\\"input(\'7\')">7</button>"}}\n'
    '{"action":"finish","message":"Created calc.html."}'
)


def test_stray_quote_in_a_write_is_repaired_not_skipped() -> None:
    """2026-09-13 session: one unescaped quote in 1.6K of HTML closed the JSON
    string early; the parser skipped the broken write_file and took the
    finish behind it, and the turn claimed a file it never wrote."""
    a = sa.parse_agent_action(BROKEN_WRITE)
    assert a["action"] == "tool" and a["tool"] == "write_file", a
    assert a["args"]["content"] == '<button onclick="clear()">C</button><button onclick="input(\'7\')">7</button>', a["args"]


def test_real_calculator_reply_decodes_to_the_write() -> None:
    """The 2026-09-13 session's reply verbatim: 1.6K of HTML with fifteen
    unescaped attribute quotes, then a finish claiming the file was created."""
    raw = (Path(__file__).resolve().parent / "fixtures" / "reply_2026-09-13_calculator_unescaped_quotes.txt").read_text(encoding="utf-8")
    a = sa.parse_agent_action(raw)
    assert a["action"] == "tool" and a["tool"] == "write_file", a["action"]
    content = a["args"]["content"]
    assert content.startswith("<!DOCTYPE html>") and content.rstrip().endswith("</html>"), content[-80:]
    assert content.count('onclick="input(') == 15 and "\\\"" not in content, content.count('onclick="input(')


def test_truncated_reply_is_told_apart_from_a_malformed_one() -> None:
    """A reply cut off mid-string cannot be fixed by re-quoting; the model has
    to send the content in two parts. The 2026-09-13 calculator reply (stray
    quotes, but complete) must NOT be called truncated — it is repairable."""
    complete = (Path(__file__).resolve().parent / "fixtures"
                / "reply_2026-09-13_calculator_unescaped_quotes.txt").read_text(encoding="utf-8")
    assert not sa.parsing.looks_truncated(complete)
    assert sa.parse_agent_action(complete)["action"] == "tool"
    cut = '{"action":"write_file","args":{"path":"c.html","content":"<html>\\n<button onclick=\\"go()'
    assert sa.parsing.looks_truncated(cut)
    assert not sa.parsing.looks_truncated('{"action":"finish","message":"done"}')
    assert not sa.parsing.looks_truncated("I could not do that.")


def test_json_error_describes_what_finally_blocks_decoding() -> None:
    """Not the first stray quote, which the repair already fixed."""
    cut = '{"action":"write_file","args":{"path":"c.html","content":"a\\"b\\"c then cut off'
    detail = sa.parsing.describe_json_error(cut)
    assert detail.startswith("Unterminated string"), detail
    assert sa.parsing.describe_json_error('{"action":"finish","message":"ok"}') == ""


def test_retry_echo_keeps_only_a_head_of_a_long_failed_reply() -> None:
    """The cut-off reply used to stay whole in the retry context, so the retry
    had less room than the attempt before it and was cut shorter still."""
    assert sa._retry_echo('{"action":"finish"}') == '{"action":"finish"}'
    echoed = sa._retry_echo("y" * 2000)
    assert len(echoed) < 500 and echoed.startswith("y" * 400)
    assert "1600 more characters" in echoed


def test_broken_first_object_is_a_retry_not_the_finish_behind_it() -> None:
    raw = '{"action":"write_file","args":{"path":"x","content":"abc\n{"action":"finish","message":"Created x."}'
    a = sa.parse_agent_action(raw)
    assert a["action"] == "finish" and a.get("fallback") == "prose", a
    assert "Created x." not in a["message"] or "write_file" in a["message"]
    assert "Unterminated string" in sa.parsing.describe_json_error(raw) or "Expecting" in sa.parsing.describe_json_error(raw)


def test_batched_valid_actions_still_take_the_first() -> None:
    raw = ('{"action":"edit_file","args":{"path":"a","old_string":"x","new_string":"y"}}'
           '{"action":"verify_syntax","args":{"path":"a"}}')
    a = sa.parse_agent_action(raw)
    assert a["action"] == "tool" and a["tool"] == "edit_file"


def test_raw_newline_inside_a_json_string_is_accepted() -> None:
    raw = '{"action":"write_file","args":{"path":"n.txt","content":"line one\nline two"}}'.replace("\\n", "\n")
    a = sa.parse_agent_action(raw)
    assert a["action"] == "tool" and a["args"]["content"] == "line one\nline two"


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


def test_write_file_decodes_double_escaped_body() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        f = Path(tmp) / "m.py"
        # One line of literal backslash-n: the model escaped its JSON twice.
        sa.write_file_tool(str(f), "import re" + "\\n" * 3 + "# Regex" + "\\n" + "X = 1")
        body = f.read_text(encoding="utf-8")
        assert body == "import re\n\n\n# Regex\nX = 1", body
        # A body with real newlines is written verbatim, backslashes and all.
        sa.write_file_tool(str(f), "x = \"a\\nb\"\ny = 1\n")
        assert f.read_text(encoding="utf-8") == "x = \"a\\nb\"\ny = 1\n"
        # runit-1, 2026-09-17, verbatim: {"content":"print(\\\"Hello, world\\\")"}
        # decodes to a body whose only quotes are escaped. Written as-is it is
        # a SyntaxError; 5 of 10 valid runs ended there.
        sa.write_file_tool(str(f), 'print(\\"Hello, world\\")')
        assert f.read_text(encoding="utf-8") == 'print("Hello, world")'
        # One bare quote anywhere and the body is the model's to keep.
        sa.write_file_tool(str(f), 's = "say \\"hi\\""\n')
        assert f.read_text(encoding="utf-8") == 's = "say \\"hi\\""\n'


def test_reply_missing_its_last_brace_is_closed_and_decoded() -> None:
    """2026-09-18 session, verbatim: two write_file replies ended at `"}`,
    one brace short. The stray-quote repair escaped the content's closing
    quote and the model was told its quoting was wrong; it then escaped
    everything twice and wrote a file with backslashes in it."""
    from hexcli import parsing as _p
    fx = Path(__file__).resolve().parent / "fixtures"
    for name, expect_len in (("reply_2026-09-18_missing_brace_1.txt", 2239),
                             ("reply_2026-09-18_missing_brace_2.txt", 782)):
        raw = (fx / name).read_text(encoding="utf-8")
        assert len(raw) == expect_len and raw.endswith('"}'), name
        action = _p.parse_json_object(raw)
        assert action and action["action"] == "write_file", name
        assert action["args"]["path"] == "calculator.py", name
        body = action["args"]["content"]
        assert "\n" in body, name                       # real newlines, not one long line
        import ast as _ast
        _ast.parse(body)                                 # and it is the Python the model wrote
        assert _p.describe_json_error(raw) == "", name
        assert not _p.looks_truncated(raw), name
    # Two braces short closes twice; a still-open string is NOT closed --
    # that reply is cut off, and the two-step feedback is the right answer.
    assert _p.parse_json_object('{"action":"finish","message":"ok"') == {"action": "finish", "message": "ok"}
    assert _p.parse_json_object('{"action":"read_file","args":{"path":"a.py"') == {"action": "read_file", "args": {"path": "a.py"}}
    cut = '{"action":"write_file","args":{"path":"a.py","content":"print(1)\nprint('
    assert _p.parse_json_object(cut) is None
    assert _p.looks_truncated(cut)
    # An error in the middle of the text is not a missing brace, and the
    # stray-quote repair still handles it.
    assert _p.parse_json_object('{"action":"finish","message":"say "hi" now"}') == {"action": "finish", "message": 'say "hi" now'}


TESTS = [
    test_reply_missing_its_last_brace_is_closed_and_decoded,
    test_write_file_decodes_double_escaped_body,
    test_stray_quote_in_a_write_is_repaired_not_skipped,
    test_real_calculator_reply_decodes_to_the_write,
    test_truncated_reply_is_told_apart_from_a_malformed_one,
    test_json_error_describes_what_finally_blocks_decoding,
    test_retry_echo_keeps_only_a_head_of_a_long_failed_reply,
    test_broken_first_object_is_a_retry_not_the_finish_behind_it,
    test_batched_valid_actions_still_take_the_first,
    test_raw_newline_inside_a_json_string_is_accepted,
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
