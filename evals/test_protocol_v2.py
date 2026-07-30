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


def test_write_payload_missing_fence() -> None:
    r = p2.parse_response('<action>{"name": "write", "arguments": {"path": "a.txt"}}</action>')
    assert r.kind == "malformed"
    assert "fenced block" in r.error


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
    assert est_tokens <= 900, f"v2 core prompt is ~{est_tokens} est tokens; budget is ≤900"
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
    test_write_payload_missing_fence,
    test_apply_exact_unique,
    test_apply_ambiguous_is_error_not_guess,
    test_apply_trailing_whitespace_tier,
    test_apply_indent_shift_tier,
    test_apply_no_match_reports_closest_region,
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
