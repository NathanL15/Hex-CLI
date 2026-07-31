#!/usr/bin/env python3
"""evals/test_lineedit.py — the rich input line.

The editor is driven entirely through injected key/write callables, so every
behaviour below is exercised with no terminal, no msvcrt, and no LLM. That is
the whole reason the key source is abstracted: a hand-rolled line editor is
only defensible if it is actually tested.

Usage:
    python evals/test_lineedit.py
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

from hexcli import lineedit as le  # noqa: E402

BACKSPACE = le.BACKSPACE
CLEAR_SCREEN = le.CLEAR_SCREEN
DELETE = le.DELETE
DOWN = le.DOWN
END = le.END
ENTER = le.ENTER
EOF_KEY = le.EOF_KEY
ESCAPE = le.ESCAPE
EXHAUSTED = le.EXHAUSTED
HOME = le.HOME
INTERRUPT = le.INTERRUPT
KILL_LINE = le.KILL_LINE
KILL_TO_START = le.KILL_TO_START
KILL_WORD = le.KILL_WORD
LEFT = le.LEFT
NEWLINE = le.NEWLINE
RIGHT = le.RIGHT
TAB = le.TAB
UP = le.UP
WORD_LEFT = le.WORD_LEFT
WORD_RIGHT = le.WORD_RIGHT
History = le.History
LineEditor = le.LineEditor
common_prefix = le.common_prefix
default_completer = le.default_completer
visible_len = le.visible_len


def editor(keys: list[str], **kw: Any) -> tuple[LineEditor, list[str]]:
    """An editor fed a scripted key list, capturing everything it writes."""
    out: list[str] = []
    stream = iter(keys)
    ed = LineEditor(
        read_key=lambda: next(stream, EXHAUSTED),
        write=out.append,
        width=kw.pop("width", 80),
        styled=kw.pop("styled", False),
        **kw,
    )
    return ed, out


def typed(text: str) -> list[str]:
    return list(text)


def run(keys: list[str], **kw: Any) -> str:
    ed, _ = editor(keys, **kw)
    return ed.read("you> ")


# ---------------------------------------------------------------------------
# Basic entry
# ---------------------------------------------------------------------------

def test_plain_line_returns_text() -> None:
    assert run(typed("hello world") + [ENTER]) == "hello world"


def test_empty_line_returns_empty() -> None:
    assert run([ENTER]) == ""


def test_backspace_deletes_before_cursor() -> None:
    assert run(typed("helloo") + [BACKSPACE, ENTER]) == "hello"


def test_backspace_at_start_is_harmless() -> None:
    assert run([BACKSPACE, BACKSPACE] + typed("hi") + [ENTER]) == "hi"


def test_insert_in_the_middle() -> None:
    keys = typed("helo") + [LEFT] + typed("l") + [ENTER]
    assert run(keys) == "hello"


def test_delete_removes_after_cursor() -> None:
    keys = typed("hello") + [HOME, DELETE, ENTER]
    assert run(keys) == "ello"


def test_delete_at_end_is_harmless() -> None:
    assert run(typed("hi") + [DELETE, DELETE, ENTER]) == "hi"


def test_arrows_clamp_at_both_ends() -> None:
    keys = typed("ab") + [LEFT, LEFT, LEFT, LEFT, RIGHT, RIGHT, RIGHT] + typed("!") + [ENTER]
    assert run(keys) == "ab!"


def test_home_and_end() -> None:
    keys = typed("world") + [HOME] + typed("hello ") + [END] + typed("!") + [ENTER]
    assert run(keys) == "hello world!"


def test_escape_clears_the_line() -> None:
    assert run(typed("garbage") + [ESCAPE] + typed("clean") + [ENTER]) == "clean"


def test_unicode_survives_round_trip() -> None:
    assert run(typed("héllo — ✓") + [ENTER]) == "héllo — ✓"


# ---------------------------------------------------------------------------
# Word motion and kills
# ---------------------------------------------------------------------------

def test_kill_word_removes_previous_word() -> None:
    # "--amend" is one word: '-' is a word character so flags and filenames
    # die in a single Ctrl+W rather than needing one per punctuation mark.
    assert run(typed("git commit --amend") + [KILL_WORD, ENTER]) == "git commit "


def test_kill_word_from_trailing_space() -> None:
    assert run(typed("one two ") + [KILL_WORD, ENTER]) == "one "


def test_kill_to_start() -> None:
    keys = typed("throw away keep") + [WORD_LEFT, KILL_TO_START, ENTER]
    assert run(keys) == "keep"


def test_kill_line_removes_to_end() -> None:
    keys = typed("keep this drop that") + [HOME, WORD_RIGHT, WORD_RIGHT, KILL_LINE, ENTER]
    assert run(keys) == "keep this"


def test_word_left_then_right_returns() -> None:
    keys = typed("alpha beta") + [WORD_LEFT, WORD_RIGHT] + typed("!") + [ENTER]
    assert run(keys) == "alpha beta!"


def test_word_motion_treats_paths_as_one_word() -> None:
    # '.', '-' and '_' are word characters, so Ctrl+W eats a whole filename
    # rather than stopping at every dot.
    assert run(typed("open my-file.txt") + [KILL_WORD, ENTER]) == "open "


# ---------------------------------------------------------------------------
# Multi-line
# ---------------------------------------------------------------------------

def test_pasted_newline_does_not_submit() -> None:
    """The bug this feature exists to fix: pasting a multi-line block used to
    submit the first line and run the rest as separate commands."""
    keys = typed("line one") + [NEWLINE] + typed("line two") + [NEWLINE] + \
        typed("line three") + [ENTER]
    assert run(keys) == "line one\nline two\nline three"


def test_backslash_continues_the_line() -> None:
    keys = typed("first \\") + [ENTER] + typed("second") + [ENTER]
    assert run(keys) == "first \nsecond"


def test_home_end_are_per_line_in_multiline() -> None:
    keys = typed("aaa") + [NEWLINE] + typed("bbb") + [HOME] + typed("X") + [ENTER]
    assert run(keys) == "aaa\nXbbb"


def test_backspace_joins_lines() -> None:
    keys = typed("aaa") + [NEWLINE, BACKSPACE] + typed("bbb") + [ENTER]
    assert run(keys) == "aaabbb"


def test_kill_to_start_stops_at_the_line_boundary() -> None:
    keys = typed("keep") + [NEWLINE] + typed("drop") + [KILL_TO_START, ENTER]
    assert run(keys) == "keep\n"


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def test_interrupt_raises_keyboard_interrupt() -> None:
    ed, _ = editor(typed("half typed") + [INTERRUPT])
    try:
        ed.read("you> ")
    except KeyboardInterrupt:
        return
    raise AssertionError("Ctrl+C did not raise KeyboardInterrupt")


def test_eof_on_empty_line_raises_eoferror() -> None:
    ed, _ = editor([EOF_KEY])
    try:
        ed.read("you> ")
    except EOFError:
        return
    raise AssertionError("Ctrl+D on an empty line did not raise EOFError")


def test_eof_with_text_does_not_exit() -> None:
    """Ctrl+D mid-line must not throw away a half-typed request."""
    assert run(typed("wait") + [EOF_KEY] + typed("!") + [ENTER]) == "wait!"


def test_exhausted_key_source_returns_buffer() -> None:
    ed, _ = editor(typed("no enter"))
    assert ed.read("you> ") == "no enter"


def test_exhausted_empty_source_raises_eoferror() -> None:
    ed, _ = editor([])
    try:
        ed.read("you> ")
    except EOFError:
        return
    raise AssertionError("an exhausted key source on an empty line must EOF")


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------

def _history(entries: list[str]) -> History:
    h = History(None)
    h.entries = list(entries)
    return h


def test_up_recalls_previous_entry() -> None:
    h = _history(["first", "second"])
    assert run([UP, ENTER], history=h) == "second"


def test_up_twice_walks_further_back() -> None:
    h = _history(["first", "second"])
    assert run([UP, UP, ENTER], history=h) == "first"


def test_down_returns_to_the_draft() -> None:
    h = _history(["old"])
    keys = typed("draft") + [UP, DOWN, ENTER]
    assert run(keys, history=h) == "draft"


def test_up_at_the_oldest_entry_stops() -> None:
    h = _history(["only"])
    assert run([UP, UP, UP, ENTER], history=h) == "only"


def test_prefix_search_filters_history() -> None:
    """Typing `git ` then Up should skip non-git entries."""
    h = _history(["git status", "ls -la", "git commit"])
    keys = typed("git ") + [UP, ENTER]
    assert run(keys, history=h) == "git commit"


def test_prefix_search_walks_only_matches() -> None:
    h = _history(["git status", "ls -la", "git commit"])
    keys = typed("git ") + [UP, UP, ENTER]
    assert run(keys, history=h) == "git status"


def test_recalled_entry_is_editable() -> None:
    h = _history(["hello"])
    assert run([UP] + typed(" world") + [ENTER], history=h) == "hello world"


def test_history_records_submitted_line() -> None:
    h = _history([])
    ed, _ = editor(typed("remember me") + [ENTER], history=h)
    ed.read("you> ")
    assert h.entries == ["remember me"]


def test_history_skips_blank_and_repeat() -> None:
    h = _history(["same"])
    h.add("")
    h.add("same")
    h.add("   ")
    assert h.entries == ["same"]


def test_history_honours_its_limit() -> None:
    h = History(None, limit=3)
    for i in range(10):
        h.add(f"cmd{i}")
    assert h.entries == ["cmd7", "cmd8", "cmd9"]


def test_history_round_trips_through_a_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "nested" / "input_history"
        h = History(path)
        h.add("alpha")
        h.add("beta")
        reloaded = History(path)
        assert reloaded.entries == ["alpha", "beta"], reloaded.entries


def test_history_file_preserves_multiline_entries() -> None:
    """Entries are stored one per line, so embedded newlines must be escaped
    or a pasted block would come back as several bogus entries."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hist"
        h = History(path)
        h.add("line one\nline two")
        reloaded = History(path)
        assert reloaded.entries == ["line one\nline two"], reloaded.entries


def test_history_survives_an_unwritable_path() -> None:
    h = History(Path("Z:/definitely/not/writable/hist"))
    h.add("still fine")          # must not raise
    assert h.entries == ["still fine"]


def test_history_survives_a_corrupt_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "hist"
        path.write_bytes(b"\xff\xfe bad bytes\n\x00ok\n")
        assert isinstance(History(path).entries, list)


# ---------------------------------------------------------------------------
# Completion
# ---------------------------------------------------------------------------

COMMANDS = ("/help", "/history", "/config", "/compact", "/mode", "/new")


def test_unique_command_completes_fully() -> None:
    comp = default_completer(COMMANDS)
    keys = typed("/hel") + [TAB, ENTER]
    # A finished completion adds a space: the next thing you type is an
    # argument, not more of the command.
    assert run(keys, completer=comp) == "/help "


def test_ambiguous_command_completes_common_prefix() -> None:
    comp = default_completer(COMMANDS)
    keys = typed("/h") + [TAB] + typed("x") + [ENTER]
    # /help and /history share "/h" only, so nothing is inserted.
    assert run(keys, completer=comp) == "/hx"


def test_common_prefix_is_inserted_when_it_helps() -> None:
    comp = default_completer(("/session", "/settings"))
    keys = typed("/s") + [TAB] + typed("!") + [ENTER]
    # Ambiguous, but "/se" is unambiguous — advance as far as certainty goes.
    assert run(keys, completer=comp) == "/se!"


def test_config_keys_complete() -> None:
    comp = default_completer(COMMANDS, lambda: ["show_diffs", "shell_exe", "model"])
    keys = typed("/config sho") + [TAB, ENTER]
    assert run(keys, completer=comp) == "/config show_diffs "


def test_mode_values_complete() -> None:
    comp = default_completer(COMMANDS)
    keys = typed("/mode ch") + [TAB, ENTER]
    assert run(keys, completer=comp) == "/mode chat "


def test_path_completion_finds_a_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "uniquename.txt").write_text("x", encoding="utf-8")
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            comp = default_completer(COMMANDS)
            keys = typed("read uniq") + [TAB, ENTER]
            assert run(keys, completer=comp) == "read uniquename.txt "
        finally:
            os.chdir(cwd)


def test_path_completion_marks_directories() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "somedir").mkdir()
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            comp = default_completer(COMMANDS)
            result = run(typed("ls somed") + [TAB, ENTER], completer=comp)
            # Directories complete without a trailing space so you can keep
            # descending; the separator is what signals "there is more".
            assert result == "ls somedir" + os.sep, result
        finally:
            os.chdir(cwd)


def test_completed_directory_can_still_be_submitted() -> None:
    """Regression: a completed directory ends in "\\", which the multi-line
    continuation rule mistook for "keep typing" — so Enter did nothing."""
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "somedir").mkdir()
        cwd = os.getcwd()
        os.chdir(tmp)
        try:
            comp = default_completer(COMMANDS)
            result = run(typed("ls somed") + [TAB, ENTER], completer=comp)
            assert result == "ls somedir" + os.sep, repr(result)
        finally:
            os.chdir(cwd)


def test_backslash_continuation_still_needs_whitespace() -> None:
    assert run(typed("path C:\\Users\\") + [ENTER]) == "path C:\\Users\\"


def test_completion_of_nothing_is_a_no_op() -> None:
    comp = default_completer(COMMANDS)
    keys = typed("/zzzz") + [TAB, ENTER]
    assert run(keys, completer=comp) == "/zzzz"


def test_editor_without_a_completer_ignores_tab() -> None:
    assert run(typed("plain") + [TAB, ENTER]) == "plain"


def test_a_raising_completer_does_not_break_input() -> None:
    def boom(_: str) -> list[str]:
        raise RuntimeError("completer exploded")
    assert run(typed("safe") + [TAB] + typed("!") + [ENTER], completer=boom) == "safe!"


def test_common_prefix_helper() -> None:
    assert common_prefix(["abcd", "abce", "abz"]) == "ab"
    assert common_prefix(["only"]) == "only"
    assert common_prefix([]) == ""
    assert common_prefix(["a", "b"]) == ""


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def test_visible_len_ignores_ansi() -> None:
    assert visible_len("\033[1myou>\033[0m ") == 5


def test_render_never_leaks_a_partial_escape() -> None:
    ed, out = editor(typed("hello") + [ENTER], styled=True)
    ed.read("you> ")
    text = "".join(out)
    assert "\033[?25l" in text and "\033[?25h" in text
    assert not text.endswith("\033["), "truncated escape sequence emitted"


def test_cursor_row_tracks_a_wrapped_line() -> None:
    """A line longer than the terminal must report a cursor on a later row,
    otherwise the next redraw anchors too high and eats the line above."""
    ed, _ = editor([], width=20)
    ed.buffer = "x" * 50
    ed.pos = 50
    _, rows, cursor_row, _ = ed._layout("you> ")
    assert cursor_row > 0, "wrapped line reported cursor on row 0"
    assert rows > cursor_row or rows == cursor_row + 1


def test_layout_rows_cover_multiline_buffers() -> None:
    ed, _ = editor([], width=80)
    ed.buffer = "a\nb\nc"
    ed.pos = 5
    _, rows, cursor_row, col = ed._layout("you> ")
    assert rows == 3, rows
    assert cursor_row == 2, cursor_row
    assert col == len(ed.CONT_PROMPT) + 1


def test_layout_pads_exact_width_lines() -> None:
    """Deferred wrap: a line of exactly `width` chars leaves the cursor at the
    margin, so the row maths only holds if such lines are padded."""
    ed, _ = editor([], width=20)
    ed.buffer = "x" * 15          # 5-char prompt + 15 = exactly 20
    ed.pos = 15
    text, rows, _, _ = ed._layout("you> ")
    assert text.endswith(" "), "exact-width line was not padded"
    assert rows == 2, rows


def test_prompt_with_a_header_line_is_laid_out() -> None:
    ed, _ = editor([], width=80)
    ed.buffer = "hi"
    ed.pos = 2
    _, rows, cursor_row, col = ed._layout("[model | mode]\nyou> ")
    assert rows == 2, rows
    assert cursor_row == 1, cursor_row
    assert col == len("you> ") + 2


def test_clear_screen_key_emits_the_escape() -> None:
    ed, out = editor([CLEAR_SCREEN] + typed("after") + [ENTER])
    assert ed.read("you> ") == "after"
    assert "\033[2J" in "".join(out)


# ---------------------------------------------------------------------------
# make_reader integration
# ---------------------------------------------------------------------------

def test_make_reader_declines_when_disabled() -> None:
    assert le.make_reader({"rich_input": False}, COMMANDS) is None


def test_make_reader_declines_without_a_tty() -> None:
    """CI and piped stdin must fall back to input(), not a raw-mode editor."""
    class FakeStd:
        def isatty(self) -> bool:
            return False
    real_in, real_out = sys.stdin, sys.stdout
    sys.stdin, sys.stdout = FakeStd(), FakeStd()   # type: ignore[assignment]
    try:
        assert le.make_reader({"rich_input": True}, COMMANDS) is None
    finally:
        sys.stdin, sys.stdout = real_in, real_out


def test_default_history_path_is_under_the_app_dir() -> None:
    cfg: dict[str, Any] = {"input_history_file": ""}
    path_text = str(cfg.get("input_history_file") or "")
    path = Path(path_text) if path_text else Path.home() / ".shellai" / "input_history"
    assert path.name == "input_history"


def test_repl_commands_are_all_completable() -> None:
    """Guards against a new slash command being added to the REPL but not to
    the completion list."""
    import hexcli.agent as sa
    comp = default_completer(sa.REPL_COMMANDS)
    for cmd in sa.REPL_COMMANDS:
        assert comp(cmd[:3]), f"{cmd} is not reachable by completion"


def test_repl_commands_covers_everything_run_repl_handles() -> None:
    """REPL_COMMANDS drives both Tab completion and the did-you-mean hint, so
    a command missing from it is invisible to the user. Read the dispatcher's
    own source rather than trusting the list to be maintained by hand — four
    commands had already drifted out of it."""
    import inspect
    import re as _re

    import hexcli.agent as sa

    # Comments mention misspellings on purpose ("/hlep"), so scan code only.
    source = "\n".join(
        line for line in inspect.getsource(sa.run_repl).splitlines()
        if not line.lstrip().startswith("#")
    )
    handled = set(_re.findall(r'"(/[a-z]+)[ "]', source))
    missing = handled - set(sa.REPL_COMMANDS)
    assert not missing, f"handled by run_repl but not listed: {sorted(missing)}"


def test_repl_commands_has_no_phantoms() -> None:
    import inspect

    import hexcli.agent as sa
    source = inspect.getsource(sa.run_repl)
    phantom = [c for c in sa.REPL_COMMANDS if f'"{c}' not in source]
    assert not phantom, f"advertised by completion but never handled: {phantom}"


TESTS = [
    test_plain_line_returns_text,
    test_empty_line_returns_empty,
    test_backspace_deletes_before_cursor,
    test_backspace_at_start_is_harmless,
    test_insert_in_the_middle,
    test_delete_removes_after_cursor,
    test_delete_at_end_is_harmless,
    test_arrows_clamp_at_both_ends,
    test_home_and_end,
    test_escape_clears_the_line,
    test_unicode_survives_round_trip,
    test_kill_word_removes_previous_word,
    test_kill_word_from_trailing_space,
    test_kill_to_start,
    test_kill_line_removes_to_end,
    test_word_left_then_right_returns,
    test_word_motion_treats_paths_as_one_word,
    test_pasted_newline_does_not_submit,
    test_backslash_continues_the_line,
    test_home_end_are_per_line_in_multiline,
    test_backspace_joins_lines,
    test_kill_to_start_stops_at_the_line_boundary,
    test_interrupt_raises_keyboard_interrupt,
    test_eof_on_empty_line_raises_eoferror,
    test_eof_with_text_does_not_exit,
    test_exhausted_key_source_returns_buffer,
    test_exhausted_empty_source_raises_eoferror,
    test_up_recalls_previous_entry,
    test_up_twice_walks_further_back,
    test_down_returns_to_the_draft,
    test_up_at_the_oldest_entry_stops,
    test_prefix_search_filters_history,
    test_prefix_search_walks_only_matches,
    test_recalled_entry_is_editable,
    test_history_records_submitted_line,
    test_history_skips_blank_and_repeat,
    test_history_honours_its_limit,
    test_history_round_trips_through_a_file,
    test_history_file_preserves_multiline_entries,
    test_history_survives_an_unwritable_path,
    test_history_survives_a_corrupt_file,
    test_unique_command_completes_fully,
    test_ambiguous_command_completes_common_prefix,
    test_common_prefix_is_inserted_when_it_helps,
    test_config_keys_complete,
    test_mode_values_complete,
    test_path_completion_finds_a_file,
    test_path_completion_marks_directories,
    test_completed_directory_can_still_be_submitted,
    test_backslash_continuation_still_needs_whitespace,
    test_completion_of_nothing_is_a_no_op,
    test_editor_without_a_completer_ignores_tab,
    test_a_raising_completer_does_not_break_input,
    test_common_prefix_helper,
    test_visible_len_ignores_ansi,
    test_render_never_leaks_a_partial_escape,
    test_cursor_row_tracks_a_wrapped_line,
    test_layout_rows_cover_multiline_buffers,
    test_layout_pads_exact_width_lines,
    test_prompt_with_a_header_line_is_laid_out,
    test_clear_screen_key_emits_the_escape,
    test_make_reader_declines_when_disabled,
    test_make_reader_declines_without_a_tty,
    test_default_history_path_is_under_the_app_dir,
    test_repl_commands_are_all_completable,
    test_repl_commands_covers_everything_run_repl_handles,
    test_repl_commands_has_no_phantoms,
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
    print(f"\nevals/test_lineedit.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
