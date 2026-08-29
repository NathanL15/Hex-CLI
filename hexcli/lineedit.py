#!/usr/bin/env python3
"""hexcli.lineedit — a real input line for the REPL.

Replaces bare ``input()`` with persistent history, Tab completion, word-wise
editing and multi-line paste. Pure stdlib: the key source is ``msvcrt`` (which
hexcli.agent already depends on for Esc-to-cancel), so this adds no third-party
dependency and keeps the "stdlib + numpy/onnxruntime" rule intact.

Design notes
------------
*Testability.* The editor never touches ``msvcrt`` directly. It pulls tokens
from an injectable ``read_key`` callable and writes through an injectable
``write``. Tests drive it with a scripted token list and assert on the returned
string, so the whole editor is covered by the offline suite with no terminal.

*Rendering.* Every redraw is anchored on the cursor position we set ourselves,
so the anchor is always known: move up ``cursor_row`` rows, clear downward,
rewrite, then move to the computed (row, col).

The one subtlety is deferred wrap: a line of exactly ``width`` characters
leaves the cursor at the right margin rather than on the next row, so a naive
``ceil`` row count is off by one for exact multiples. Rather than special-case
it everywhere, ``_pad`` appends one space to any line whose visible length is
an exact positive multiple of the width. No line is then ever an exact
multiple, ``rows = n // width + 1`` holds universally, and the pad is
invisible.
"""
from __future__ import annotations

import os
import re
import sys
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from typing import Any

# ── key tokens ──────────────────────────────────────────────────────────────
# Multi-character names never collide with printable input, which is always a
# single character.
ENTER = "<enter>"
NEWLINE = "<newline>"        # a newline that came from a paste, not a keypress
BACKSPACE = "<backspace>"
DELETE = "<delete>"
TAB = "<tab>"
LEFT = "<left>"
RIGHT = "<right>"
UP = "<up>"
DOWN = "<down>"
HOME = "<home>"
END = "<end>"
WORD_LEFT = "<word-left>"
WORD_RIGHT = "<word-right>"
KILL_WORD = "<kill-word>"    # Ctrl+W
KILL_LINE = "<kill-line>"    # Ctrl+K
KILL_TO_START = "<kill-to-start>"  # Ctrl+U
CLEAR_SCREEN = "<clear-screen>"    # Ctrl+L
INTERRUPT = "<interrupt>"    # Ctrl+C
EOF_KEY = "<eof>"            # Ctrl+D on an empty buffer
ESCAPE = "<escape>"
EXHAUSTED = "<exhausted>"    # key source ran out (tests / closed stdin)

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")

# Extended (0x00 / 0xe0 prefixed) scancodes on Windows.
_EXTENDED = {
    "H": UP, "P": DOWN, "K": LEFT, "M": RIGHT,
    "G": HOME, "O": END, "S": DELETE,
    "s": WORD_LEFT, "t": WORD_RIGHT,
}
_CONTROL = {
    "\r": ENTER, "\n": NEWLINE, "\t": TAB,
    "\x08": BACKSPACE, "\x7f": BACKSPACE,
    "\x01": HOME, "\x05": END,
    "\x02": LEFT, "\x06": RIGHT,
    "\x0e": DOWN, "\x10": UP,
    "\x03": INTERRUPT, "\x04": EOF_KEY,
    "\x0b": KILL_LINE, "\x15": KILL_TO_START, "\x17": KILL_WORD,
    "\x0c": CLEAR_SCREEN, "\x1b": ESCAPE,
}


def visible_len(text: str) -> int:
    """Length ignoring ANSI styling — what the terminal actually shows."""
    return len(_ANSI_RE.sub("", text))


# ── key source ──────────────────────────────────────────────────────────────

def windows_key_reader() -> Callable[[], str]:
    """Token stream over ``msvcrt``.

    Paste detection: a pasted block arrives as a burst, so a carriage return
    with more input already buffered behind it is a line break inside pasted
    text, not the user pressing Enter. Without this, pasting a three-line
    traceback submits the first line and leaves the rest as stray commands.
    """
    import msvcrt

    def read() -> str:
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return _EXTENDED.get(msvcrt.getwch(), "")
        if ch == "\r":
            return NEWLINE if msvcrt.kbhit() else ENTER
        return _CONTROL.get(ch, ch)

    return read


# ── history ─────────────────────────────────────────────────────────────────

class History:
    """Newline-delimited history file, most recent last.

    Entries containing newlines are stored with literal ``\\n`` escapes so the
    file stays one-entry-per-line and survives hand editing.
    """

    def __init__(self, path: Path | None = None, limit: int = 500) -> None:
        self.path = path
        self.limit = limit
        self.entries: list[str] = []
        self.load()

    def load(self) -> None:
        if self.path is None or not self.path.exists():
            return
        try:
            raw = self.path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        self.entries = [
            line.replace("\\n", "\n")
            for line in raw.splitlines()
            if line.strip()
        ][-self.limit:]

    def add(self, entry: str) -> None:
        entry = entry.strip()
        # Skip blanks and immediate repeats; re-running the same command twice
        # should not need two Up presses to get past.
        if not entry or (self.entries and self.entries[-1] == entry):
            return
        self.entries.append(entry)
        del self.entries[:-self.limit]
        self.save()

    def save(self) -> None:
        if self.path is None:
            return
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(
                "\n".join(e.replace("\n", "\\n") for e in self.entries) + "\n",
                encoding="utf-8",
            )
        except OSError:
            pass  # history is a convenience; never break the REPL over it


# ── completion ──────────────────────────────────────────────────────────────

def _path_candidates(fragment: str) -> list[str]:
    frag = fragment.replace("/", os.sep)
    directory, _, stem = frag.rpartition(os.sep)
    base = Path(directory) if directory else Path(".")
    try:
        names = sorted(p.name + (os.sep if p.is_dir() else "")
                       for p in base.iterdir())
    except OSError:
        return []
    prefix = (directory + os.sep) if directory else ""
    low = stem.lower()
    return [prefix + n for n in names if n.lower().startswith(low)]


def default_completer(
    commands: Sequence[str],
    config_keys: Callable[[], Iterable[str]] | None = None,
) -> Callable[[str], list[str]]:
    """Completer over slash commands, their arguments, and file paths.

    Returns a function mapping the text left of the cursor to candidate
    completions *of the final word*.
    """

    def complete(text: str) -> list[str]:
        stripped = text.lstrip()
        parts = stripped.split()
        trailing_space = text.endswith((" ", "\t"))
        word = "" if trailing_space else (parts[-1] if parts else "")

        # First word, and it looks like a command → complete command names.
        if stripped.startswith("/") and len(parts) <= 1 and not trailing_space:
            return [c for c in commands if c.startswith(word.lower())]

        head = parts[0].lower() if parts else ""
        if head == "/config" and len(parts) <= 2 and config_keys is not None:
            return sorted(k for k in config_keys() if k.startswith(word))
        if head in {"/resume", "/memory"}:
            return []  # arguments are not on disk in a predictable place
        return _path_candidates(word)

    return complete


def common_prefix(items: Sequence[str]) -> str:
    if not items:
        return ""
    first, last = min(items), max(items)
    for i, ch in enumerate(first):
        if i >= len(last) or last[i] != ch:
            return first[:i]
    return first


# ── the editor ──────────────────────────────────────────────────────────────

def _is_word_char(ch: str) -> bool:
    return ch.isalnum() or ch in "_-."


class LineEditor:
    """A single-buffer line editor supporting embedded newlines."""

    CONT_PROMPT = "...  "

    def __init__(
        self,
        *,
        history: History | None = None,
        completer: Callable[[str], list[str]] | None = None,
        read_key: Callable[[], str] | None = None,
        write: Callable[[str], None] | None = None,
        width: int | None = None,
        styled: bool | None = None,
    ) -> None:
        self.history = history or History()
        self.completer = completer
        self._read_key = read_key or windows_key_reader()
        self._write = write or (lambda s: (sys.stdout.write(s), sys.stdout.flush()) and None)
        self._forced_width = width
        self.styled = sys.stdout.isatty() if styled is None else styled
        self.buffer = ""
        self.pos = 0
        self._rendered_rows = 0
        self._cursor_row = 0
        self._hist_index: int | None = None
        self._hist_prefix = ""
        self._saved_draft = ""

    # -- geometry -----------------------------------------------------------

    @property
    def width(self) -> int:
        if self._forced_width:
            return self._forced_width
        try:
            return max(20, os.get_terminal_size().columns)
        except OSError:
            return 80

    def _pad(self, visible: int) -> str:
        """See module docstring: kill the exact-multiple wrap ambiguity."""
        w = self.width
        return " " if visible and visible % w == 0 else ""

    def _rows(self, visible: int) -> int:
        return visible // self.width + 1

    # -- rendering ----------------------------------------------------------

    def _layout(self, prompt: str) -> tuple[str, int, int, int]:
        """Return (text_to_write, total_rows, cursor_row, cursor_col)."""
        prompt_lines = prompt.split("\n")
        buf_lines = self.buffer.split("\n")

        # Logical lines: the prompt's leading lines stand alone; its last line
        # is the prefix of the first buffer line; later buffer lines get the
        # continuation prompt.
        logical: list[tuple[str, int]] = []   # (rendered text, visible width)
        for line in prompt_lines[:-1]:
            logical.append((line, visible_len(line)))
        last_prompt = prompt_lines[-1]
        prefixes = [last_prompt] + [self.CONT_PROMPT] * (len(buf_lines) - 1)
        for prefix, line in zip(prefixes, buf_lines):
            logical.append((prefix + line, visible_len(prefix) + len(line)))

        # Cursor: which buffer line, and how far into it.
        before = self.buffer[:self.pos]
        cur_line = before.count("\n")
        col_in_line = len(before) - (before.rfind("\n") + 1)
        cursor_logical = len(prompt_lines) - 1 + cur_line
        cursor_vis = visible_len(prefixes[cur_line]) + col_in_line

        pieces: list[str] = []
        total_rows = 0
        cursor_row = 0
        for i, (text, vis) in enumerate(logical):
            if i == cursor_logical:
                cursor_row = total_rows + cursor_vis // self.width
            pieces.append(text + self._pad(vis))
            total_rows += self._rows(vis)
        return "\n".join(pieces), total_rows, cursor_row, cursor_vis % self.width

    def _move_to_anchor(self) -> str:
        """Cursor → column 0 of the first rendered row."""
        out = "\r"
        if self._cursor_row:
            out += f"\033[{self._cursor_row}A"
        return out

    def render(self, prompt: str) -> None:
        text, total_rows, cursor_row, cursor_col = self._layout(prompt)
        out = [self._move_to_anchor(), "\033[J", text]
        # We are now at the end of the last row; walk back to the anchor and
        # down to the cursor. Both legs are computed, so the next redraw's
        # anchor stays exact.
        end_row = total_rows - 1
        out.append("\r")
        if end_row > cursor_row:
            out.append(f"\033[{end_row - cursor_row}A")
        elif cursor_row > end_row:
            out.append(f"\033[{cursor_row - end_row}B")
        if cursor_col:
            out.append(f"\033[{cursor_col}C")
        self._rendered_rows = total_rows
        self._cursor_row = cursor_row
        payload = "".join(out)
        if self.styled:
            # Hide the cursor only for the duration of this redraw (kills the
            # mid-repaint flicker), then show it again at its final position.
            # Hiding it across the whole read() left users with no caret at
            # all while typing.
            payload = "\033[?25l" + payload + "\033[?25h"
        self._write(payload)

    def _finish_render(self, prompt: str) -> None:
        """Leave the finished line on screen and the cursor below it."""
        text, total_rows, cursor_row, _ = self._layout(prompt)
        self._write(self._move_to_anchor() + "\033[J" + text + "\n")
        self._rendered_rows = 0
        self._cursor_row = 0

    # -- editing primitives -------------------------------------------------

    def insert(self, text: str) -> None:
        self.buffer = self.buffer[:self.pos] + text + self.buffer[self.pos:]
        self.pos += len(text)

    def _word_start(self) -> int:
        i = self.pos
        while i > 0 and not _is_word_char(self.buffer[i - 1]):
            i -= 1
        while i > 0 and _is_word_char(self.buffer[i - 1]):
            i -= 1
        return i

    def _word_end(self) -> int:
        i, n = self.pos, len(self.buffer)
        while i < n and not _is_word_char(self.buffer[i]):
            i += 1
        while i < n and _is_word_char(self.buffer[i]):
            i += 1
        return i

    def _line_start(self) -> int:
        return self.buffer.rfind("\n", 0, self.pos) + 1

    def _line_end(self) -> int:
        nl = self.buffer.find("\n", self.pos)
        return len(self.buffer) if nl < 0 else nl

    # -- history navigation -------------------------------------------------

    def _history_move(self, delta: int) -> None:
        entries = self.history.entries
        if not entries:
            return
        if self._hist_index is None:
            if delta > 0:
                return  # already at the draft
            self._saved_draft = self.buffer
            # Prefix search: with text typed, Up walks only matching entries —
            # the single most useful history behaviour there is.
            self._hist_prefix = self.buffer[:self.pos]
            self._hist_index = len(entries)

        idx = self._hist_index
        step = -1 if delta < 0 else 1
        while True:
            idx += step
            if idx < 0:
                return
            if idx >= len(entries):
                self._hist_index = None
                self.buffer = self._saved_draft
                self.pos = len(self.buffer)
                return
            if not self._hist_prefix or entries[idx].startswith(self._hist_prefix):
                break
        self._hist_index = idx
        self.buffer = entries[idx]
        self.pos = len(self.buffer)

    # -- completion ---------------------------------------------------------

    def _complete(self, prompt: str) -> None:
        if self.completer is None:
            return
        left = self.buffer[:self.pos]
        try:
            candidates = self.completer(left)
        except Exception:
            return
        if not candidates:
            return
        word = "" if left.endswith((" ", "\t")) else left.split()[-1] if left.split() else ""
        # A path fragment's completions are full path strings, so replace the
        # whole fragment rather than appending to it.
        if len(candidates) == 1:
            replacement = candidates[0]
            suffix = "" if replacement.endswith(os.sep) else " "
            self.buffer = left[:len(left) - len(word)] + replacement + suffix + self.buffer[self.pos:]
            self.pos = len(left) - len(word) + len(replacement) + len(suffix)
            return
        shared = common_prefix(candidates)
        if len(shared) > len(word):
            self.buffer = left[:len(left) - len(word)] + shared + self.buffer[self.pos:]
            self.pos = len(left) - len(word) + len(shared)
            return
        self._show_candidates(candidates, prompt)

    def _show_candidates(self, candidates: Sequence[str], prompt: str) -> None:
        shown = list(candidates[:40])
        width = max((len(c) for c in shown), default=0) + 2
        per_row = max(1, self.width // width)
        lines = []
        for i in range(0, len(shown), per_row):
            lines.append("".join(c.ljust(width) for c in shown[i:i + per_row]).rstrip())
        if len(candidates) > len(shown):
            lines.append(f"... and {len(candidates) - len(shown)} more")
        self._write(self._move_to_anchor() + "\033[J" + "\n".join(lines) + "\n")
        self._cursor_row = 0
        self.render(prompt)

    # -- main loop ----------------------------------------------------------

    def read(self, prompt: str = "> ") -> str:
        """Read one logical input. Raises KeyboardInterrupt / EOFError like
        ``input()`` does, so callers keep their existing handlers."""
        self.buffer, self.pos = "", 0
        self._hist_index, self._hist_prefix, self._saved_draft = None, "", ""
        self._rendered_rows, self._cursor_row = 0, 0
        try:
            self.render(prompt)
            while True:
                key = self._read_key()
                if key == "":
                    continue
                result = self._handle(key, prompt)
                if result is not None:
                    self._finish_render(prompt)
                    self.history.add(result)
                    return result
                self.render(prompt)
        finally:
            if self.styled:
                self._write("\033[?25h")

    def _handle(self, key: str, prompt: str) -> str | None:
        """Apply one key. Returns the finished line, or None to keep editing."""
        buf = self.buffer

        if key == ENTER:
            # A trailing backslash is an explicit "keep going" — the one way to
            # get a multi-line entry without pasting. It must be preceded by
            # whitespace: on Windows every completed directory ends in "\", and
            # treating that as a continuation made Tab-completing a folder and
            # pressing Enter do nothing at all.
            if buf.endswith("\\") and (len(buf) == 1 or buf[-2] in " \t"):
                self.buffer = buf[:-1] + "\n"
                self.pos = len(self.buffer)
                return None
            return buf
        if key == NEWLINE:
            self.insert("\n")
            return None
        if key == INTERRUPT:
            raise KeyboardInterrupt
        if key in (EOF_KEY, EXHAUSTED):
            if buf:
                return buf if key == EXHAUSTED else None
            raise EOFError
        if key == TAB:
            self._complete(prompt)
            return None
        if key == BACKSPACE:
            if self.pos:
                self.buffer = buf[:self.pos - 1] + buf[self.pos:]
                self.pos -= 1
            return None
        if key == DELETE:
            self.buffer = buf[:self.pos] + buf[self.pos + 1:]
            return None
        if key == LEFT:
            self.pos = max(0, self.pos - 1)
            return None
        if key == RIGHT:
            self.pos = min(len(buf), self.pos + 1)
            return None
        if key == WORD_LEFT:
            self.pos = self._word_start()
            return None
        if key == WORD_RIGHT:
            self.pos = self._word_end()
            return None
        if key == HOME:
            self.pos = self._line_start()
            return None
        if key == END:
            self.pos = self._line_end()
            return None
        if key == KILL_WORD:
            start = self._word_start()
            self.buffer = buf[:start] + buf[self.pos:]
            self.pos = start
            return None
        if key == KILL_TO_START:
            start = self._line_start()
            self.buffer = buf[:start] + buf[self.pos:]
            self.pos = start
            return None
        if key == KILL_LINE:
            self.buffer = buf[:self.pos] + buf[self._line_end():]
            return None
        if key in (UP, DOWN):
            self._history_move(-1 if key == UP else 1)
            return None
        if key == CLEAR_SCREEN:
            self._write("\033[2J\033[H")
            self._cursor_row = 0
            return None
        if key == ESCAPE:
            self.buffer, self.pos = "", 0
            self._hist_index = None
            return None
        if len(key) == 1 and (key.isprintable() or key == " "):
            self.insert(key)
        return None


# ── integration helper ──────────────────────────────────────────────────────

def make_reader(
    config: dict[str, Any],
    commands: Sequence[str],
    config_keys: Callable[[], Iterable[str]] | None = None,
) -> Callable[[str], str] | None:
    """Build the REPL's input function, or None if a rich line is unavailable.

    Callers fall back to ``input()`` on None, so a non-tty (piped stdin, CI,
    ``--raw``) keeps working exactly as before.
    """
    if not bool(config.get("rich_input", True)):
        return None
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return None
    try:
        import msvcrt  # noqa: F401
    except ImportError:
        return None

    path_text = str(config.get("input_history_file", "") or "")
    path = Path(path_text).expanduser() if path_text else \
        Path.home() / ".shellai" / "input_history"
    editor = LineEditor(
        history=History(path, int(config.get("input_history_limit", 500))),
        completer=default_completer(commands, config_keys),
    )
    return editor.read
