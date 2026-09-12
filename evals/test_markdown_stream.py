#!/usr/bin/env python3
"""evals/test_markdown_stream.py — markdown-lite to ANSI for streamed answers.

Every case is run twice: the text fed whole and fed one character at a
time. The outputs must be identical, because the REPL feeds tokens of
arbitrary length and nothing may depend on where they split.

Usage:
    python evals/test_markdown_stream.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli import ui  # noqa: E402
from hexcli.markdown_stream import MarkdownStream, render_markdown  # noqa: E402

B, R, CY, D = "\033[1m", "\033[0m", "\033[96m", "\033[2m"
RULE = "─" * 40


def both(text: str) -> str:
    whole = render_markdown(text)
    md = MarkdownStream()
    per_char = "".join(md.feed(ch) for ch in text) + md.finish()
    assert whole == per_char, f"split-dependent output:\n whole={whole!r}\n chars={per_char!r}"
    return whole


def _color(on: bool) -> None:
    ui.set_color_enabled(on)


def test_headings_become_bold_without_the_hashes() -> None:
    _color(True)
    try:
        assert both("# Title\ntext\n") == f"{B}Title{R}\ntext\n"
        assert both("### Deep\n") == f"{B}Deep{R}\n"
        # Not a heading: no space after the hashes, or hashes mid-line.
        assert both("#hashtag\n") == "#hashtag\n"
        assert both("C# is fine\n") == "C# is fine\n"
        assert both("#######\n") == "#######\n"
    finally:
        _color(True)


def test_bullets_become_dots_and_keep_indentation() -> None:
    _color(True)
    try:
        assert both("- one\n* two\n+ three\n") == f"{D}•{R} one\n{D}•{R} two\n{D}•{R} three\n"
        assert both("  - nested\n") == f"  {D}•{R} nested\n"
        # A dash that is not a bullet stays.
        assert both("-1 degrees\n") == "-1 degrees\n"
        assert both("--flag\n") == "--flag\n"
        assert both("1. ordered\n") == "1. ordered\n"
    finally:
        _color(True)


def test_bold_and_code_spans() -> None:
    _color(True)
    try:
        assert both("a **bold** word\n") == f"a {B}bold{R} word\n"
        assert both("**Note:** x\n") == f"{B}Note:{R} x\n"
        assert both("use `dict.get`\n") == f"use {CY}dict.get{R}\n"
        # Single stars are arithmetic, not markup.
        assert both("2 * 3 = 6\n") == "2 * 3 = 6\n"
        # Stars inside a code span are literal.
        assert both("`a**b`\n") == f"{CY}a**b{R}\n"
        # Bold across a code span re-applies bold after it.
        assert both("**x `y` z**\n") == f"{B}x {CY}y{R}{B} z{R}\n"
        # Unclosed bold at the end of the text is closed by finish().
        assert both("**open") == f"{B}open{R}"
    finally:
        _color(True)


def test_fences_become_rules_and_leave_code_untouched() -> None:
    _color(True)
    try:
        src = "Example:\n```python\nx = {\"a\": 1}\nprint(x[\"a\"])  # **not bold**\n```\nDone.\n"
        out = both(src)
        opener = f"{D}──── python {'─' * (40 - 4 - len(' python '))}{R}"
        assert out == (
            f"Example:\n{opener}\nx = {{\"a\": 1}}\nprint(x[\"a\"])  # **not bold**\n"
            f"{D}{RULE}{R}\nDone.\n"
        ), repr(out)
        # No language, and a fence that never closes.
        assert both("```\ncode\n```\n") == f"{D}{'─' * 40}{R}\ncode\n{D}{RULE}{R}\n"
        assert both("```\ncode\n") == f"{D}{'─' * 40}{R}\ncode\n"
        # Backticks at a line start that are not a fence are a code span.
        assert both("`x` first\n") == f"{CY}x{R} first\n"
        assert both("``x\n") == f"{CY}x{R}\n"
        # Three backticks mid-line are literal, not three toggles.
        assert both("see ```x``` here\n") == "see ```x``` here\n"
        # "```" followed by anything but a language name is not a fence line:
        # the backticks come back literally (seen live: a model that put the
        # code on the fence line with literal \n instead of newlines).
        assert both("```python\\ndef f():\\n    pass\\n```\n") == "```python\\ndef f():\\n    pass\\n```\n"
        assert both("```this is prose\n") == "```this is prose\n"
        assert both("```" + "x" * 30 + "\n") == "```" + "x" * 30 + "\n"
    finally:
        _color(True)


def test_fence_closes_with_trailing_space_or_crlf_and_spans_carry_over_soft_breaks() -> None:
    """Review findings: a closing fence with trailing whitespace or CRLF
    must still close; bold and code spans continue over a soft line break
    and end at a blank line; a carried span never leaves a stray reset."""
    _color(True)
    try:
        rule = f"{D}{RULE}{R}"
        opener = f"{D}──── py {'─' * (40 - 4 - len(' py '))}{R}"
        assert both("```py\ncode\n``` \nafter **b**\n") == f"{opener}\ncode\n{rule}\nafter {B}b{R}\n"
        assert both("```py\r\ncode\r\n```\r\n") == f"{opener}\ncode\r\n{rule}\n"
        # Bold across one newline: re-armed on the next line, closed by its marker.
        assert both("**bold\nmore** plain\n") == f"{B}bold{R}\n{B}more{R} plain\n"
        # A blank line ends the span; the orphan closer is literal.
        assert both("**open\n\nnext** x\n") == f"{B}open{R}\n\nnext** x\n"
        assert both("x `y\nz` w\n") == f"x {CY}y{R}\n{CY}z{R} w\n"
        # Carried at the very end of the text: no trailing reset.
        assert both("``x\n") == f"{CY}x{R}\n"
        assert both("**a\n") == f"{B}a{R}\n"
    finally:
        _color(True)


def test_plain_text_is_unchanged_and_colour_off_keeps_only_substitutions() -> None:
    _color(True)
    try:
        assert both("Just a sentence. Another one.\n\nSecond paragraph.\n") == \
            "Just a sentence. Another one.\n\nSecond paragraph.\n"
        assert both("") == ""
    finally:
        _color(True)
    _color(False)
    try:
        assert both("# T\n- a\n**b** `c`\n") == "T\n• a\nb c\n"
    finally:
        _color(True)


def test_render_result_applies_markdown() -> None:
    import contextlib
    import io
    _color(False)
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            ui.render_result("Result", "# Head\n- item")
        assert "Head\n• item" in buf.getvalue(), buf.getvalue()
    finally:
        _color(True)


TESTS = [
    test_headings_become_bold_without_the_hashes,
    test_bullets_become_dots_and_keep_indentation,
    test_bold_and_code_spans,
    test_fences_become_rules_and_leave_code_untouched,
    test_fence_closes_with_trailing_space_or_crlf_and_spans_carry_over_soft_breaks,
    test_plain_text_is_unchanged_and_colour_off_keeps_only_substitutions,
    test_render_result_applies_markdown,
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
    print(f"\nevals/test_markdown_stream.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
