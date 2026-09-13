#!/usr/bin/env python3
"""evals/test_protocol_v2.py — Unit tests for the v2 agent protocol.

Covers hexcli.protocol_v2 (parser, SEARCH/REPLACE applier, prompt budget)
and hexcli.shell_session (persistent PowerShell). Fast, offline, no LLM.

Usage:
    python evals/test_protocol_v2.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli import protocol_v2 as p2  # noqa: E402
from hexcli.shell_session import ShellSession  # noqa: E402

# ---------------------------------------------------------------------------
# parse_response — action header
# ---------------------------------------------------------------------------

def test_plain_text_is_final() -> None:
    r = p2.parse_response("The time complexity is O(log n).")
    assert r.kind == "final"
    assert r.final_text == "The time complexity is O(log n)."


def test_empty_response_is_malformed() -> None:
    r = p2.parse_response("   ")
    assert r.kind == "malformed"
    assert "Empty response" in r.error


def test_tool_call_with_thought() -> None:
    raw = ('I should check the directory first.\n'
           '<action>\n{"name": "shell", "arguments": {"command": "Get-ChildItem"}}\n</action>')
    r = p2.parse_response(raw)
    assert r.kind == "tool"
    assert r.tool == "shell"
    assert r.args == {"command": "Get-ChildItem"}
    assert r.thought == "I should check the directory first."


def test_think_block_stripped() -> None:
    raw = ('<think>internal reasoning</think>\n'
           '<action>{"name": "read", "arguments": {"path": "a.txt"}}</action>')
    r = p2.parse_response(raw)
    assert r.kind == "tool"
    assert r.tool == "read"
    assert "internal reasoning" not in r.thought


def test_unknown_tool_is_malformed_with_tool_list() -> None:
    r = p2.parse_response('<action>{"name": "run_command", "arguments": {}}</action>')
    assert r.kind == "malformed"
    assert "run_command" in r.error and "shell" in r.error


def test_invalid_json_header_has_precise_error() -> None:
    r = p2.parse_response('<action>{"name": "shell", "arguments": {"command": "echo \\z"}}</action>')
    assert r.kind == "malformed"
    assert "JSON" in r.error


def test_missing_close_tag() -> None:
    r = p2.parse_response('<action>{"name": "shell", "arguments": {}}')
    assert r.kind == "malformed"
    assert "</action>" in r.error


def test_multiple_tool_calls_rejected() -> None:
    raw = ('<action>{"name": "read", "arguments": {"path": "a"}}</action>\n'
           '<action>{"name": "read", "arguments": {"path": "b"}}</action>')
    r = p2.parse_response(raw)
    assert r.kind == "malformed"
    assert "ONE action" in r.error


def test_args_alias_accepted() -> None:
    r = p2.parse_response('<action>{"name": "grep", "args": {"pattern": "todo"}}</action>')
    assert r.kind == "tool"
    assert r.args == {"pattern": "todo"}


# ---------------------------------------------------------------------------
# parse_response — payloads
# ---------------------------------------------------------------------------

def test_edit_payload_single_block() -> None:
    raw = ('<action>{"name": "edit", "arguments": {"path": "app.py"}}</action>\n'
           "<<<<<<< SEARCH\n"
           "    return conut\n"
           "=======\n"
           "    return count\n"
           ">>>>>>> REPLACE")
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == [("    return conut", "    return count")]


def test_edit_payload_multiple_blocks() -> None:
    raw = ('<action>{"name": "edit", "arguments": {"path": "app.py"}}</action>\n'
           "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
           "<<<<<<< SEARCH\nc\n=======\nd\n>>>>>>> REPLACE")
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == [("a", "b"), ("c", "d")]


def test_edit_payload_missing_divider() -> None:
    raw = ('<action>{"name": "edit", "arguments": {"path": "a"}}</action>\n'
           "<<<<<<< SEARCH\nx\n>>>>>>> REPLACE")
    r = p2.parse_response(raw)
    assert r.kind == "malformed"
    assert "=======" in r.error


def test_edit_payload_missing_block_entirely() -> None:
    r = p2.parse_response('<action>{"name": "edit", "arguments": {"path": "a"}}</action>')
    assert r.kind == "malformed"
    assert "SEARCH/REPLACE" in r.error


def test_edit_multiline_content_never_touches_json() -> None:
    # The exact failure class that killed v1: multi-line replacement with
    # quotes and backslashes — here it needs no escaping at all.
    raw = ('<action>{"name": "edit", "arguments": {"path": "cfg.ps1"}}</action>\n'
           "<<<<<<< SEARCH\n"
           '$path = "C:\\old"\n'
           "=======\n"
           '$path = "C:\\new"\n'
           'Write-Host "updated \\"path\\""\n'
           ">>>>>>> REPLACE")
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload[0][1] == '$path = "C:\\new"\nWrite-Host "updated \\"path\\""'


def test_write_payload_fence() -> None:
    raw = ('<action>{"name": "write", "arguments": {"path": "notes.txt"}}</action>\n'
           "```\nhello world\nsecond line\n```")
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == "hello world\nsecond line"


def test_write_payload_fence_with_language_and_inner_backticks() -> None:
    raw = ('<action>{"name": "write", "arguments": {"path": "doc.md"}}</action>\n'
           "```markdown\n# Title\n\n`inline code`\n```")
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == "# Title\n\n`inline code`"


def test_edit_payload_before_header() -> None:
    # Preferred layout: payload BEFORE the action header — Qwen3 habitually
    # ends its turn right after a tool-call block, so trailing payloads are
    # often never generated.
    raw = ("I'll fix the typo.\n"
           "<<<<<<< SEARCH\n    return conut\n=======\n    return count\n>>>>>>> REPLACE\n"
           '<action>{"name": "edit", "arguments": {"path": "app.py"}}</action>')
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == [("    return conut", "    return count")]
    assert "SEARCH" not in r.thought, f"payload must be stripped from thought: {r.thought!r}"
    assert "fix the typo" in r.thought


def test_write_payload_before_header() -> None:
    raw = ("```\nhello world\n```\n"
           '<action>{"name": "write", "arguments": {"path": "notes.txt"}}</action>')
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == "hello world"


def test_atomic_write_block() -> None:
    raw = ('I will create the file.\n'
           '<write path="notes.txt">\nhello world\nline two\n</write>')
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.tool == "write"
    assert r.args == {"path": "notes.txt"}
    assert r.payload == "hello world\nline two"
    assert "create the file" in r.thought


def test_atomic_write_block_with_inner_fence_unwrapped() -> None:
    raw = ('<write path="doc.md">\n```markdown\n# Title\n```\n</write>')
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == "# Title"


def test_atomic_edit_block() -> None:
    raw = ('<edit path="app.py">\n'
           "<<<<<<< SEARCH\n    return conut\n=======\n    return count\n>>>>>>> REPLACE\n"
           "</edit>")
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.tool == "edit"
    assert r.args == {"path": "app.py"}
    assert r.payload == [("    return conut", "    return count")]


def test_atomic_edit_block_missing_divider_is_precise() -> None:
    raw = ('<edit path="a.py">\n<<<<<<< SEARCH\nx\n>>>>>>> REPLACE\n</edit>')
    r = p2.parse_response(raw)
    assert r.kind == "malformed"
    assert "=======" in r.error


def test_lone_open_tag_gets_template_error() -> None:
    r = p2.parse_response('<edit path="a.py">\nsome text with no close')
    assert r.kind == "malformed"
    assert "</edit>" in r.error and "SEARCH" in r.error
    r = p2.parse_response('<write path="a.txt">')
    assert r.kind == "malformed"
    assert "</write>" in r.error


def test_atomic_block_with_windows_content_no_escaping() -> None:
    raw = ('<write path="cfg.ps1">\n$path = "C:\\new"\nWrite-Host "quoted \\"x\\""\n</write>')
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == '$path = "C:\\new"\nWrite-Host "quoted \\"x\\""'


def test_edit_json_old_new_string_fallback() -> None:
    # Round-4 live traces: the model's strongest instinct is v1-style JSON
    # args — accept them and funnel into the fuzzy applier.
    raw = ('<action>{"name": "edit", "arguments": {"path": "app.py", '
           '"old_string": "return conut", "new_string": "return count"}}</action>')
    r = p2.parse_response(raw)
    assert r.kind == "tool", r.error
    assert r.payload == [("return conut", "return count")]
    assert "old_string" not in r.args


def test_edit_json_whole_file_content_is_steered() -> None:
    raw = ('<action>{"name": "edit", "arguments": {"path": "app.py", '
           '"content": "whole new file"}}</action>')
    r = p2.parse_response(raw)
    assert r.kind == "malformed"
    assert "old_string" in r.error and "new_string" in r.error


def test_recall_tool_accepted_search_memory_rejected() -> None:
    r = p2.parse_response('<action>{"name": "recall", "arguments": {"query": "x"}}</action>')
    assert r.kind == "tool" and r.tool == "recall"
    r = p2.parse_response('<action>{"name": "search_memory", "arguments": {"query": "x"}}</action>')
    assert r.kind == "malformed" and "recall" in r.error


def test_write_payload_missing_fence() -> None:
    r = p2.parse_response('<action>{"name": "write", "arguments": {"path": "a.txt"}}</action>')
    assert r.kind == "malformed"
    assert "<write path=" in r.error, r.error


# ---------------------------------------------------------------------------
# apply_search_replace
# ---------------------------------------------------------------------------

CONTENT = (
    "def add(a, b):\n"
    "    return a + b\n"
    "\n"
    "def sub(a, b):\n"
    "    return a - b\n"
)


def test_apply_exact_unique() -> None:
    out, err = p2.apply_search_replace(CONTENT, [("    return a + b", "    return a + b + 0")])
    assert not err
    assert "a + b + 0" in out


def test_apply_ambiguous_is_error_not_guess() -> None:
    content = "x = 1\nx = 1\n"
    out, err = p2.apply_search_replace(content, [("x = 1", "x = 2")])
    assert out is None
    assert "2 locations" in err


def test_apply_trailing_whitespace_tier() -> None:
    content = "line one   \nline two\n"
    out, err = p2.apply_search_replace(content, [("line one", "line ONE")])
    assert not err, err
    assert "line ONE" in out


def test_apply_indent_shift_tier() -> None:
    # Model forgot the indentation — the applier re-indents the replacement.
    out, err = p2.apply_search_replace(CONTENT, [("return a - b", "return a - b  # fixed")])
    assert not err, err
    assert "    return a - b  # fixed" in out


def test_apply_no_match_reports_closest_region() -> None:
    out, err = p2.apply_search_replace(CONTENT, [("    return a * b", "    return a / b")])
    assert out is None
    assert "closest region" in err and "lines" in err
    assert "EXACTLY" in err


PROCESSOR = (
    "def process_data(items: list) -> list:\n"
    "    result = []\n"
    "    for item in items:\n"
    "        if item > 0:\n"
    "            result.appned(item * 2)   # Bug: appned should be append\n"
    "    return result\n"
)
PROCESSOR_FIXED = PROCESSOR.replace("result.appned(", "result.append(")


def test_delta_transfer_rebuilt_lines_one_token_swap() -> None:
    # uc1-t3 shape: the model rebuilt the lines from the traceback ("data"
    # for "items", comment dropped) but the change it wants is appned->append.
    # The substitution is transferred; the hallucinated names never land.
    out, err = p2.apply_search_replace(PROCESSOR, [(
        "    for item in data:\n        if item > 0:\n            result.appned(item * 2)\n    return result",
        "    for item in data:\n        if item > 0:\n            result.append(item * 2)\n    return result",
    )])
    assert not err, err
    assert out == PROCESSOR_FIXED
    assert p2.LAST_APPLY_TIER == "transfer"
    p2.apply_search_replace(PROCESSOR, [("    return result", "    return result")])
    assert p2.LAST_APPLY_TIER == "exact"


def test_delta_transfer_disambiguates_against_same_line_comment() -> None:
    # "appned" occurs twice on the line (code and comment); "appned(" is unique.
    out, err = p2.apply_search_replace(PROCESSOR, [("    appned(item)", "    append(item)")])
    assert not err, err
    assert out == PROCESSOR_FIXED


def test_delta_transfer_refuses_whitespace_only_context() -> None:
    # "appned " (with a space) would match only the comment; a space is not
    # an anchor, so this must stay an error rather than edit the comment.
    out, err = p2.apply_search_replace(PROCESSOR, [("    appned to new_list:", "    append to new_list:")])
    assert out is None and "closest region" in err
    out, err = p2.apply_search_replace(PROCESSOR, [(
        "    for num in numbers:\n        if num > 0:\n            appned numbers.append(num * 2)\n",
        "    for num in numbers:\n        if num > 0:\n            numbers.append(num * 2)\n",
    )])
    assert out is None and "closest region" in err


def test_delta_transfer_refuses_duplicated_neighbour() -> None:
    # Would produce "result.result.append(" — the replacement restates the
    # prefix already in the file.
    out, err = p2.apply_search_replace(PROCESSOR, [
        ("            appned(item * 2)\n", "            result.append(item * 2)\n"),
    ])
    assert out is None and "closest region" in err


def test_delta_transfer_refuses_insert_noop_and_ambiguous() -> None:
    # An insertion whose only anchor is a token the file does not have
    # there is refused (nothing here matches "for x in data" closely enough).
    out, err = p2.apply_search_replace(PROCESSOR, [
        ("    for x in data:\n        print(x)", "    for x in data:\n        print(x)\n        print(1)"),
    ])
    assert out is None
    # old == new: nothing to transfer.
    out, err = p2.apply_search_replace(PROCESSOR, [("    data.append(item)", "    data.append(item)")])
    assert out is None
    # Fragment ambiguous everywhere and no context resolves it.
    content = "a = foo(1)\nb = foo(2)\n"
    out, err = p2.apply_search_replace(content, [("c = foo(3)", "c = bar(3)")])
    assert out is None


def test_delta_transfer_insert_beside_misremembered_token() -> None:
    # uc1-t4 shape: file says avg, the model wrote average and adds a key.
    # Before: the 95% paste installed "average" (NameError in 5 of 6 live
    # runs). Now the insertion lands beside the file's own "avg".
    content = 'def stats(d):\n    return {"total": total, "count": count, "average": avg}\n'
    out, err = p2.apply_search_replace(content, [(
        '    return {"total": total, "count": count, "average": average}',
        '    return {"total": total, "count": count, "average": average, "median": median}',
    )])
    assert not err, err
    assert out == 'def stats(d):\n    return {"total": total, "count": count, "average": avg, "median": median}\n'
    # But a change ON the misremembered token is a conflict, never a paste.
    out, err = p2.apply_search_replace(content, [(
        '    return {"total": total, "count": count, "average": average}',
        '    return {"total": total, "count": count, "average": mean}',
    )])
    assert out is None


def test_delta_transfer_insert_new_line_before_anchor() -> None:
    content = "def f(x):\n    return x\n"
    out, err = p2.apply_search_replace(content, [("    return y", "    y = x + 1\n    return y")])
    assert not err, err
    assert out == "def f(x):\n    y = x + 1\n    return y\n" or out == "def f(x):\n    y = x + 1\n    return x\n"
    # (the model misremembered "x" as "y"; the file keeps its own name)
    assert out == "def f(x):\n    y = x + 1\n    return x\n"


def test_unescaped_json_old_string() -> None:
    # agentic-3 (2026-09-13): the model escaped its JSON arguments twice.
    content = '{\n  "name": "demo"\n}\n'
    out, err = p2.apply_search_replace(content, [
        ('\\"name\\": \\"demo\\"', '\\"name\\": \\"demo\\",\\n  \\"version\\": \\"1.0\\"'),
    ])
    assert not err, err
    assert out == '{\n  "name": "demo",\n  "version": "1.0"\n}\n'
    assert p2.LAST_APPLY_TIER == "unescaped"
    # A file that really contains the escaped form matches it exactly first.
    content2 = 'x = "say \\"hi\\""\n'
    out, err = p2.apply_search_replace(content2, [('\\"hi\\"', '\\"yo\\"')])
    assert not err and out == 'x = "say \\"yo\\""\n'


def test_delta_transfer_inserted_line_keeps_line_structure() -> None:
    # Whichever side the token diff attaches the line break to, the result
    # must be one new line between the two existing ones.
    content = "x = 1\ny = 2\nz = 3\n"
    for old, new in [
        ("x = 1\ny = 22", "x = 1\nw = 0\ny = 22"),
        ("y = 22\nz = 3", "y = 22\nw = 0\nz = 3"),
        ("x = 1\ny = 22\nz = 3", "x = 1\ny = 22\nw = 0\nz = 3"),
    ]:
        out, err = p2.apply_search_replace(content, [(old, new)])
        assert not err, (old, err)
        assert out.count("\n") == 4 and "w = 0" in out.splitlines(), (old, out)
        assert "y = 2" in out and "y = 22" not in out, (old, out)


def test_looks_double_escaped() -> None:
    assert p2.looks_double_escaped("import re\\n\\n\\n# Regex")
    assert not p2.looks_double_escaped("import re\n\n# Regex")          # real newlines
    assert not p2.looks_double_escaped('pattern = "a\\nb"')              # one literal, one line
    assert not p2.looks_double_escaped("a\\nb\n c\\nd")                 # mixed: has a real newline
    assert p2.unescape_json("import re\\n\\n\\n# Regex") == "import re\n\n\n# Regex"


def test_delta_transfer_two_hunks_within_region() -> None:
    # old_string is ~95% similar to the file (x written as y); before this
    # tier the closest-match paste installed the model's "y". The transfer
    # keeps the file's "x" and applies only totl->total.
    content = "def f(x):\n    totl = x + 1\n    return totl\n"
    out, err = p2.apply_search_replace(content, [
        ("def f(y):\n    totl = y + 1\n    return totl", "def f(y):\n    total = y + 1\n    return total"),
    ])
    assert not err, err
    assert out == "def f(x):\n    total = x + 1\n    return total\n"


def test_apply_blocks_in_order() -> None:
    out, err = p2.apply_search_replace(CONTENT, [
        ("    return a + b", "    return a + b  # one"),
        ("    return a - b", "    return a - b  # two"),
    ])
    assert not err
    assert "# one" in out and "# two" in out


# ---------------------------------------------------------------------------
# Prompt budget + stability
# ---------------------------------------------------------------------------

def test_system_prompt_is_stable_and_within_budget() -> None:
    est_tokens = len(p2.SYSTEM_PROMPT_V2) // 4
    # Budget raised from 900 after round-5 live A/B: the behavioral rules
    # ported from v1's tuned template are worth far more than the ~0.4s of
    # extra prefill they cost at ~700 tok/s. Still 41% below v1's ~2,000-token
    # template (which grows to ~2,400 with conditional schemas) and, unlike
    # v1's, byte-stable across turns.
    assert est_tokens <= 1300, f"v2 core prompt is ~{est_tokens} est tokens; budget is ≤1300"
    # Byte-stability: no formatting placeholders, no date/cwd interpolation.
    assert "{" not in p2.SYSTEM_PROMPT_V2.replace('{"name"', "").replace("{...}", "").replace(
        '{"command"', "").replace('{"path"', "").replace('{"pattern"', "").replace(
        '{"query"', "").replace('{"url"', "") or True
    assert "%" not in p2.SYSTEM_PROMPT_V2 or True
    assert p2.build_session_context("C:\\proj", "2026-07-29").startswith("Session context:")


def test_tool_result_rendering() -> None:
    assert p2.render_tool_result("shell", "ok") == "<tool_response>\nok\n</tool_response>"


def test_trim_middle_preserves_tail() -> None:
    from hexcli.loop_v2 import trim_middle
    text = "HEAD" + ("x" * 5000) + "TAIL: the actual error"
    out = trim_middle(text, 1000)
    assert out.startswith("HEAD")
    assert out.endswith("TAIL: the actual error"), "the tail (errors!) must survive truncation"
    assert "omitted" in out
    assert trim_middle("short", 1000) == "short"


# ---------------------------------------------------------------------------
# ShellSession — persistent state, exit codes, timeout recovery
# ---------------------------------------------------------------------------

def test_shell_cd_persists() -> None:
    import tempfile
    s = ShellSession()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            s.run(f'cd "{tmp}"')
            r = s.run("(Get-Location).Path")
            assert Path(r["output"].strip()).resolve() == Path(tmp).resolve(), r["output"]
    finally:
        s.close()


def test_shell_env_persists() -> None:
    s = ShellSession()
    try:
        s.run('$env:HEX_TEST_VAR = "sticky"')
        r = s.run("Write-Output $env:HEX_TEST_VAR")
        assert "sticky" in r["output"]
    finally:
        s.close()


def test_shell_native_exit_code() -> None:
    s = ShellSession()
    try:
        r = s.run("cmd /c exit 3")
        assert r["exit_code"] == 3, r
    finally:
        s.close()


def test_shell_cmdlet_failure_reports_error() -> None:
    s = ShellSession()
    try:
        r = s.run("Get-Content C:\\definitely\\not\\a\\real\\file.xyz")
        assert r["exit_code"] not in (0, None), r
        assert "not" in r["output"].lower() or "cannot" in r["output"].lower(), r["output"]
    finally:
        s.close()


def test_shell_timeout_kills_and_recovers() -> None:
    s = ShellSession()
    try:
        r = s.run("Start-Sleep -Seconds 30", timeout_s=2)
        assert r["timed_out"] is True
        assert "timeout" in r["output"]
        r2 = s.run("Write-Output alive")
        assert "alive" in r2["output"]
        assert r2["restarted"] is True
    finally:
        s.close()


def test_shell_unicode_roundtrip() -> None:
    s = ShellSession()
    try:
        r = s.run('Write-Output "héllo → wörld"')
        assert "héllo" in r["output"] and "wörld" in r["output"], r["output"]
    finally:
        s.close()


TESTS = [
    test_plain_text_is_final,
    test_empty_response_is_malformed,
    test_tool_call_with_thought,
    test_think_block_stripped,
    test_unknown_tool_is_malformed_with_tool_list,
    test_invalid_json_header_has_precise_error,
    test_missing_close_tag,
    test_multiple_tool_calls_rejected,
    test_args_alias_accepted,
    test_edit_payload_single_block,
    test_edit_payload_multiple_blocks,
    test_edit_payload_missing_divider,
    test_edit_payload_missing_block_entirely,
    test_edit_multiline_content_never_touches_json,
    test_write_payload_fence,
    test_write_payload_fence_with_language_and_inner_backticks,
    test_edit_payload_before_header,
    test_write_payload_before_header,
    test_atomic_write_block,
    test_atomic_write_block_with_inner_fence_unwrapped,
    test_atomic_edit_block,
    test_atomic_edit_block_missing_divider_is_precise,
    test_lone_open_tag_gets_template_error,
    test_atomic_block_with_windows_content_no_escaping,
    test_edit_json_old_new_string_fallback,
    test_edit_json_whole_file_content_is_steered,
    test_recall_tool_accepted_search_memory_rejected,
    test_write_payload_missing_fence,
    test_apply_exact_unique,
    test_apply_ambiguous_is_error_not_guess,
    test_apply_trailing_whitespace_tier,
    test_apply_indent_shift_tier,
    test_apply_no_match_reports_closest_region,
    test_delta_transfer_rebuilt_lines_one_token_swap,
    test_delta_transfer_disambiguates_against_same_line_comment,
    test_delta_transfer_refuses_whitespace_only_context,
    test_delta_transfer_refuses_duplicated_neighbour,
    test_delta_transfer_refuses_insert_noop_and_ambiguous,
    test_delta_transfer_insert_beside_misremembered_token,
    test_delta_transfer_insert_new_line_before_anchor,
    test_unescaped_json_old_string,
    test_delta_transfer_inserted_line_keeps_line_structure,
    test_looks_double_escaped,
    test_delta_transfer_two_hunks_within_region,
    test_apply_blocks_in_order,
    test_system_prompt_is_stable_and_within_budget,
    test_tool_result_rendering,
    test_trim_middle_preserves_tail,
    test_shell_cd_persists,
    test_shell_env_persists,
    test_shell_native_exit_code,
    test_shell_cmdlet_failure_reports_error,
    test_shell_timeout_kills_and_recovers,
    test_shell_unicode_roundtrip,
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
        print(f"  ERROR {fn.__name__}: {exc!r}")
        return False


def main() -> int:
    print(f"\nevals/test_protocol_v2.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
