#!/usr/bin/env python3
"""evals/test_stream_render.py — Unit tests for incremental stream rendering.

Covers hexcli.stream_render: the state machine that decides what a human sees
while an agent response is still arriving. Critical property — it must never
leak raw JSON to the user, and must never withhold prose.

Usage:
    python evals/test_stream_render.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli.stream_render import StreamRenderer  # noqa: E402


def _render(chunks: list[str]) -> tuple[str, list[str]]:
    out: list[str] = []
    tools: list[str] = []
    r = StreamRenderer(out.append, tools.append)
    for c in chunks:
        r.feed(c)
    r.finish()
    return "".join(out), tools


def _char_chunks(text: str) -> list[str]:
    """Worst case: one character per delta."""
    return list(text)


# ---------------------------------------------------------------------------
# finish messages stream as readable text
# ---------------------------------------------------------------------------

def test_finish_message_streams_text_only() -> None:
    raw = '{"action":"finish","message":"All done here."}'
    text, tools = _render(_char_chunks(raw))
    assert text == "All done here.", repr(text)
    assert tools == []


def test_no_json_syntax_ever_leaks() -> None:
    raw = '{"action":"finish","message":"Result: 42"}'
    text, _ = _render(_char_chunks(raw))
    for ch in ('{', '}', '"action"', 'message"'):
        assert ch not in text, f"leaked {ch!r} into user output: {text!r}"


def test_escapes_are_decoded() -> None:
    raw = r'{"action":"finish","message":"line1\nline2\ttab \"quoted\" back\\slash"}'
    text, _ = _render(_char_chunks(raw))
    assert text == 'line1\nline2\ttab "quoted" back\\slash', repr(text)


def test_unicode_escape_decoded() -> None:
    raw = '{"action":"finish","message":"caf\\u00e9"}'
    text, _ = _render(_char_chunks(raw))
    assert text == "café", repr(text)


def test_trailing_json_after_message_ignored() -> None:
    raw = '{"action":"finish","message":"Done."},{"action":"read_file","args":{}}'
    text, _ = _render(_char_chunks(raw))
    assert text == "Done.", repr(text)


def test_message_before_action_key_still_works() -> None:
    raw = '{"message":"Reordered keys still stream.","action":"finish"}'
    text, _ = _render(_char_chunks(raw))
    assert text == "Reordered keys still stream.", repr(text)


# ---------------------------------------------------------------------------
# tool actions announce, don't dump
# ---------------------------------------------------------------------------

def test_tool_action_announced_not_printed() -> None:
    raw = '{"action":"read_file","args":{"path":"notes.txt"}}'
    text, tools = _render(_char_chunks(raw))
    assert tools == ["read_file"], tools
    assert text == "", f"tool actions must not print to the user: {text!r}"


def test_nested_tool_form_announces_real_name() -> None:
    raw = '{"action":"tool","tool":"edit_file","args":{"path":"a.py"}}'
    text, tools = _render(_char_chunks(raw))
    assert tools == ["edit_file"], tools
    assert text == ""


def test_tool_announced_before_stream_completes() -> None:
    # The whole point: intent visible early, not after the full response.
    r_out: list[str] = []
    tools: list[str] = []
    r = StreamRenderer(r_out.append, tools.append)
    r.feed('{"action":"run_command","args":{"command":"Get-ChildItem -Recurse ')
    assert tools == ["run_command"], "tool must be announced mid-stream"


# ---------------------------------------------------------------------------
# prose passes straight through
# ---------------------------------------------------------------------------

def test_plain_prose_streams_immediately() -> None:
    text, tools = _render(_char_chunks("Binary search is O(log n)."))
    assert text == "Binary search is O(log n)."
    assert tools == []


def test_prose_is_not_withheld_waiting_for_json() -> None:
    out: list[str] = []
    r = StreamRenderer(out.append)
    r.feed("The answer ")
    assert "".join(out) == "The answer ", "prose must not be buffered indefinitely"


def test_short_prose_flushed_on_finish() -> None:
    out: list[str] = []
    r = StreamRenderer(out.append)
    r.feed("`")           # ambiguous: could be a fence
    r.finish()
    assert "".join(out) == "`"


def test_fenced_json_still_treated_as_json() -> None:
    raw = '```json\n{"action":"finish","message":"Fenced."}\n```'
    text, _ = _render(_char_chunks(raw))
    assert "Fenced." in text
    assert "{" not in text and "action" not in text


# ---------------------------------------------------------------------------
# chunking invariance
# ---------------------------------------------------------------------------

def test_chunk_boundaries_do_not_change_output() -> None:
    raw = r'{"action":"finish","message":"Multi\nline \"quoted\" answer."}'
    per_char, _ = _render(_char_chunks(raw))
    whole, _ = _render([raw])
    halves, _ = _render([raw[:17], raw[17:]])
    thirds, _ = _render([raw[:9], raw[9:30], raw[30:]])
    assert per_char == whole == halves == thirds, (per_char, whole, halves, thirds)


def test_split_inside_escape_sequence() -> None:
    raw = r'{"action":"finish","message":"a\nb"}'
    i = raw.index("\\n")
    text, _ = _render([raw[:i + 1], raw[i + 1:]])  # split between \ and n
    assert text == "a\nb", repr(text)


def test_split_inside_unicode_escape() -> None:
    raw = '{"action":"finish","message":"caf\\u00e9!"}'
    i = raw.index("00e9")
    text, _ = _render([raw[:i + 2], raw[i + 2:]])
    assert text == "café!", repr(text)


# ---------------------------------------------------------------------------
# Protocol v2 action blocks must not leak either
# ---------------------------------------------------------------------------

V2_ACTION = (
    "<action>\n"
    '{"name": "shell", "arguments": {"command": "Get-ChildItem"}}\n'
    "</action>"
)
V2_WRITE = '<write path="notes.txt">\nhello world\n</write>'
V2_EDIT = (
    '<edit path="app.py">\n'
    "<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n"
    "</edit>"
)


def test_v2_action_block_not_streamed_to_user() -> None:
    """Regression: the probe classified anything not starting with '{' as
    prose, so protocol-v2 responses streamed their raw action JSON to the
    user — the exact thing this renderer exists to prevent."""
    text, tools = _render(_char_chunks(V2_ACTION))
    assert text == "", f"v2 action JSON leaked to the user: {text!r}"
    assert tools == ["shell"], tools


def test_v2_write_block_payload_not_streamed() -> None:
    text, tools = _render(_char_chunks(V2_WRITE))
    assert "hello world" not in text, f"file payload leaked: {text!r}"
    assert tools == ["write"], tools


def test_v2_edit_block_announces_edit() -> None:
    text, tools = _render(_char_chunks(V2_EDIT))
    assert tools == ["edit"], tools
    assert "SEARCH" not in text, f"edit payload leaked: {text!r}"


TESTS = [
    test_v2_action_block_not_streamed_to_user,
    test_v2_write_block_payload_not_streamed,
    test_v2_edit_block_announces_edit,
    test_finish_message_streams_text_only,
    test_no_json_syntax_ever_leaks,
    test_escapes_are_decoded,
    test_unicode_escape_decoded,
    test_trailing_json_after_message_ignored,
    test_message_before_action_key_still_works,
    test_tool_action_announced_not_printed,
    test_nested_tool_form_announces_real_name,
    test_tool_announced_before_stream_completes,
    test_plain_prose_streams_immediately,
    test_prose_is_not_withheld_waiting_for_json,
    test_short_prose_flushed_on_finish,
    test_fenced_json_still_treated_as_json,
    test_chunk_boundaries_do_not_change_output,
    test_split_inside_escape_sequence,
    test_split_inside_unicode_escape,
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
    print(f"\nevals/test_stream_render.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
