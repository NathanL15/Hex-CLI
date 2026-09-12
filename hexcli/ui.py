#!/usr/bin/env python3
"""hexcli.ui — presentation layer for Hex CLI.

Pure rendering/formatting: no imports from hexcli.agent (one-way dependency,
hexcli.agent -> hexcli.ui). Functions here take plain data (dicts, strings,
lists) rather than calling back into the data/backend layer.
"""
from __future__ import annotations

import contextlib
import msvcrt
import os
import re
import subprocess
import sys
import textwrap
import threading
import time
import unicodedata
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COLOR_ON = sys.stdout.isatty() and __import__("os").environ.get("NO_COLOR") is None


class C:
    RESET   = "\033[0m"   if _COLOR_ON else ""
    BOLD    = "\033[1m"   if _COLOR_ON else ""
    DIM     = "\033[2m"   if _COLOR_ON else ""
    RED     = "\033[31m"  if _COLOR_ON else ""
    GREEN   = "\033[32m"  if _COLOR_ON else ""
    YELLOW  = "\033[33m"  if _COLOR_ON else ""
    BLUE    = "\033[34m"  if _COLOR_ON else ""
    MAGENTA = "\033[35m"  if _COLOR_ON else ""
    CYAN    = "\033[36m"  if _COLOR_ON else ""
    GRAY    = "\033[90m"  if _COLOR_ON else ""
    BRED    = "\033[91m"  if _COLOR_ON else ""
    BGREEN  = "\033[92m"  if _COLOR_ON else ""
    BYELLOW = "\033[93m"  if _COLOR_ON else ""
    BBLUE   = "\033[94m"  if _COLOR_ON else ""
    BMAGENTA = "\033[95m" if _COLOR_ON else ""
    BCYAN   = "\033[96m"  if _COLOR_ON else ""
    BWHITE  = "\033[97m"  if _COLOR_ON else ""


def cprint(text: str, color: str = "", bold: bool = False, file: Any = None) -> None:
    prefix = (C.BOLD if bold else "") + color
    suffix = C.RESET if prefix else ""
    print(f"{prefix}{text}{suffix}", file=file or sys.stdout)


def set_color_enabled(enabled: bool) -> None:
    """Force ANSI styling on/off, overriding the isatty/NO_COLOR autodetect.

    Used by shellai.py's --raw flag, which must take effect before any
    output is printed.
    """
    global _COLOR_ON
    _COLOR_ON = enabled
    codes = {
        "RESET": "\033[0m", "BOLD": "\033[1m", "DIM": "\033[2m",
        "RED": "\033[31m", "GREEN": "\033[32m", "YELLOW": "\033[33m",
        "BLUE": "\033[34m", "MAGENTA": "\033[35m", "CYAN": "\033[36m",
        "GRAY": "\033[90m", "BRED": "\033[91m", "BGREEN": "\033[92m",
        "BYELLOW": "\033[93m", "BBLUE": "\033[94m", "BMAGENTA": "\033[95m",
        "BCYAN": "\033[96m", "BWHITE": "\033[97m",
    }
    for name, code in codes.items():
        setattr(C, name, code if enabled else "")


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

# The status bar's live area when one is installed (hexcli.statusbar). The
# spinner then animates in the status line instead of on the transcript row.
LIVE_AREA: Any = None


def _live_area() -> Any:
    live = LIVE_AREA
    return live if live is not None and live.enabled else None


@contextlib.contextmanager
def paused_status() -> Any:
    """Take the status box down for an inline console prompt (a y/N confirm),
    then restore it. A no-op when there is no box up, and safe to nest."""
    live = _live_area()
    if live is None:
        yield
        return
    live.suspend()
    try:
        yield
    finally:
        live.resume()


class Spinner:
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.08):
            frame = self._FRAMES[i % len(self._FRAMES)]
            live = _live_area()
            if live is not None:
                live.tick(frame)
            elif self._on_terminal():
                sys.stderr.write(f"\r{C.BCYAN}{frame}{C.RESET} {C.DIM}{self.label}...{C.RESET}")
                sys.stderr.flush()
            i += 1

    @staticmethod
    def _on_terminal() -> bool:
        """A pipe or a log gets no animation: the frames would land in the
        captured output as a smear of carriage returns."""
        try:
            return sys.stderr.isatty()
        except Exception:  # noqa: BLE001
            return False

    def __enter__(self) -> Spinner:
        live = _live_area()
        if live is not None:
            live.set_activity(self.label)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        live = _live_area()
        if live is not None:
            # A "▸ tool" label set while the stream ran is for the tool
            # about to execute; leave it. Anything else was ours.
            if not (live.activity or "").startswith("▸ "):
                live.set_activity(None)
            return
        if self._on_terminal():
            sys.stderr.write("\r\033[K")
            sys.stderr.flush()


# ---------------------------------------------------------------------------
# Console QuickEdit
# ---------------------------------------------------------------------------

def enable_vt_processing() -> None:
    """Switch on ANSI/VT handling for stdout and stderr on a classic console.

    The launcher does this for the shortcut window; a direct `python -m
    hexcli` in a bare conhost would otherwise print the margin's reflow and
    clear sequences (`ESC[K`, `ESC[nD`) as text. No-op under Windows
    Terminal and on non-console streams.
    """
    if os.name != "nt":
        return
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        for std in (-11, -12):
            handle = k32.GetStdHandle(std)
            mode = ctypes.c_uint32()
            if k32.GetConsoleMode(handle, ctypes.byref(mode)):
                k32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def disable_quick_edit() -> None:
    """Turn off conhost QuickEdit for this console for the life of the REPL.

    With QuickEdit on (the classic-console default) a click inside the window
    starts a selection and EVERY console write blocks until a key is pressed:
    the answer streams into a frozen screen and Ctrl+C — "copy" while text is
    selected — is what releases it, without ever reaching Python. Measured
    2026-09-04 in a window launched exactly like the Start Menu shortcut.
    Windows Terminal ignores the flag; non-console stdin is left alone.
    The original mode is restored at exit so a shared cmd window is not
    changed permanently.
    """
    if os.name != "nt":
        return
    try:
        import atexit
        import ctypes
        k32 = ctypes.windll.kernel32
        stdin = k32.GetStdHandle(-10)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(stdin, ctypes.byref(mode)):
            return
        original = mode.value
        ENABLE_MOUSE_INPUT, ENABLE_QUICK_EDIT, ENABLE_EXTENDED_FLAGS = 0x0010, 0x0040, 0x0080
        # Mouse input off as well: with it on and QuickEdit off, Windows
        # Terminal routes the mouse to the application instead of selecting
        # text (Shift+drag was the only way to copy). Hex reads no mouse
        # events, so nothing is lost.
        wanted = (original & ~ENABLE_QUICK_EDIT & ~ENABLE_MOUSE_INPUT) | ENABLE_EXTENDED_FLAGS
        if k32.SetConsoleMode(stdin, wanted):
            atexit.register(lambda: k32.SetConsoleMode(stdin, original))
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Left margin
# ---------------------------------------------------------------------------

_ANSI_SEQ = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
_REFLOW_MAX_WORD = 30   # longer "words" (URLs, hashes) break where they fall


def _cell_width(ch: str) -> int:
    if ch == "\t":
        return 0  # handled by the caller (advance to the next tab stop)
    if unicodedata.combining(ch):
        return 0
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


class _Margin:
    """Shared state for the two wrapped streams: one screen, one cursor.

    Rows are wrapped HERE, at `width - 2*pad` visible cells, so the terminal
    never wraps for us — its continuation rows would start at column 0 with
    no margin (the first bug report: "only the first line is indented").
    Wrapping is word-aware even for text that arrives token by token: when a
    row fills mid-word, the partial word already on screen is erased (cursor
    left + clear to end of line) and reprinted at the start of the next row.
    """

    def __init__(self, pad: int, width: Callable[[], int] | None = None) -> None:
        self.pad = pad
        self.fill = " " * pad
        self._width = width
        self.col = 0          # visible cells printed on the current row
        self.word = ""        # raw text since the last break opportunity on this row
        self.word_vis = 0

    @property
    def usable(self) -> int:
        if self._width is not None:
            width = self._width()
        else:
            try:
                width = os.get_terminal_size().columns
            except OSError:
                width = 80
        return max(10, width - 2 * self.pad)

    def _newline(self, out: list[str], ctl: str = "\n") -> None:
        out.append(ctl + self.fill)
        self.col = 0
        self.word, self.word_vis = "", 0

    def render(self, s: str) -> str:
        out: list[str] = []
        usable = self.usable
        i, n = 0, len(s)
        while i < n:
            ch = s[i]
            if ch == "\033":
                m = _ANSI_SEQ.match(s, i)
                if m:
                    seq = m.group()
                    out.append(seq)
                    self.word += seq
                    i = m.end()
                    continue
            i += 1
            if ch == "\n" or ch == "\r":
                self._newline(out, ch)
                continue
            if ch == "\t":
                step = 8 - self.col % 8
                if self.col + step > usable:
                    continue  # a tab past the edge is invisible anyway
                out.append(ch)
                self.col += step
                self.word, self.word_vis = "", 0
                continue
            w = _cell_width(ch)
            if self.col + w > usable:
                if ch == " ":
                    self.word, self.word_vis = "", 0
                    continue  # the row ended on a space: nothing to show
                # Row full mid-word. Reflow the partial word if it started
                # after a space on this row and is short enough to bother.
                if 0 < self.word_vis < self.col and self.word_vis <= _REFLOW_MAX_WORD:
                    out.append(f"\033[{self.word_vis}D\033[K")
                    word = self.word
                    self._newline(out)
                    out.append(word)
                    self.word, self.word_vis = word, _visible_cells(word)
                    self.col = self.word_vis
                else:
                    self._newline(out)
            out.append(ch)
            self.col += w
            if ch == " ":
                self.word, self.word_vis = "", 0
            else:
                self.word += ch
                self.word_vis += w
        return "".join(out)


def _visible_cells(text: str) -> int:
    return sum(_cell_width(c) for c in _ANSI_SEQ.sub("", text))


class _MarginStream:
    """A console stream with a left AND right margin (see _Margin).

    Attribute access falls through to the wrapped stream (isatty, encoding,
    buffer, reconfigure, ...). The line editor is told the same margin so its
    wrap math and cursor moves agree (LineEditor.margin): its rows never
    exceed the usable width, so this layer never wraps them.
    """

    def __init__(self, base: Any, margin: _Margin) -> None:
        self._base = base
        self._margin = margin

    @property
    def pad(self) -> int:
        return self._margin.pad

    def write(self, s: str) -> int:
        if s:
            self._base.write(self._margin.render(s))
        return len(s)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._base, name)


def install_margin(pad: int) -> None:
    """Wrap stdout and stderr with a `pad`-column margin on both sides (tty only)."""
    pad = max(0, int(pad or 0))
    if not pad or isinstance(sys.stdout, _MarginStream):
        return
    try:
        if not sys.stdout.isatty():
            return
    except Exception:
        return
    margin = _Margin(pad)
    base = sys.stdout
    sys.stdout = _MarginStream(base, margin)
    sys.stderr = _MarginStream(sys.stderr, margin)
    base.write(margin.fill)   # the cursor is at column 0 right now


# ---------------------------------------------------------------------------
# Console font (classic conhost only; Windows Terminal zooms by itself)
# ---------------------------------------------------------------------------

_FONT_STATE_PATH = Path.home() / ".shellai" / "console_font"
_FONT_MIN, _FONT_MAX, _FONT_STEP = 8, 40, 2
_ZOOM_ANCHOR_PX: tuple[int, int] | None = None   # window size to keep across zooms


def _console_font_api() -> tuple[Any, Any, Any] | None:
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _Coord(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _FontInfo(ctypes.Structure):
        _fields_ = [("cbSize", wintypes.ULONG), ("nFont", wintypes.DWORD),
                    ("dwFontSize", _Coord), ("FontFamily", wintypes.UINT),
                    ("FontWeight", wintypes.UINT), ("FaceName", wintypes.WCHAR * 32)]

    k32 = ctypes.windll.kernel32
    handle = k32.GetStdHandle(-11)
    info = _FontInfo()
    info.cbSize = ctypes.sizeof(_FontInfo)
    if not k32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(info)):
        return None
    return k32, handle, info


def console_font_height() -> int | None:
    """Current console font height in pixels, or None outside a console."""
    api = _console_font_api()
    return int(api[2].dwFontSize.Y) if api else None


def set_console_font_height(height: int) -> bool:
    api = _console_font_api()
    if not api:
        return False
    import ctypes
    k32, handle, info = api
    info.dwFontSize.X = 0   # let the console pick the matching width
    info.dwFontSize.Y = max(_FONT_MIN, min(_FONT_MAX, int(height)))
    return bool(k32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(info)))


def _console_client_px() -> tuple[int, int] | None:
    """Pixel size of the console window's client area (classic conhost)."""
    if os.name != "nt" or os.environ.get("WT_SESSION"):
        return None
    import ctypes
    from ctypes import wintypes
    hwnd = ctypes.windll.kernel32.GetConsoleWindow()
    if not hwnd:
        return None
    rect = wintypes.RECT()
    if not ctypes.windll.user32.GetClientRect(hwnd, ctypes.byref(rect)):
        return None
    return rect.right - rect.left, rect.bottom - rect.top


def _refit_console_cells(client_px: tuple[int, int]) -> None:
    """After a font change, pick the column/row count that fills the SAME
    pixel area, so the window keeps its size and only the text scales.
    Without this conhost keeps the cell count and grows the window instead."""
    import ctypes
    from ctypes import wintypes

    class _Coord(ctypes.Structure):
        _fields_ = [("X", wintypes.SHORT), ("Y", wintypes.SHORT)]

    class _SmallRect(ctypes.Structure):
        _fields_ = [("Left", wintypes.SHORT), ("Top", wintypes.SHORT),
                    ("Right", wintypes.SHORT), ("Bottom", wintypes.SHORT)]

    class _BufferInfo(ctypes.Structure):
        _fields_ = [("dwSize", _Coord), ("dwCursorPosition", _Coord), ("wAttributes", wintypes.WORD),
                    ("srWindow", _SmallRect), ("dwMaximumWindowSize", _Coord)]

    api = _console_font_api()
    if not api:
        return
    k32, handle, info = api
    k32.GetConsoleFontSize.restype = _Coord
    cell = k32.GetConsoleFontSize(handle, info.nFont)
    if cell.X <= 0 or cell.Y <= 0:
        return
    cols = max(40, client_px[0] // cell.X)
    rows = max(10, client_px[1] // cell.Y)
    buf = _BufferInfo()
    if not k32.GetConsoleScreenBufferInfo(handle, ctypes.byref(buf)):
        return
    win = buf.srWindow
    cur_cols, cur_rows = win.Right - win.Left + 1, win.Bottom - win.Top + 1
    if (cols, rows) == (cur_cols, cur_rows):
        return
    height = max(buf.dwSize.Y, rows)          # keep the scrollback
    bottom = min(max(win.Bottom, rows - 1), height - 1)
    target = _SmallRect(0, bottom - rows + 1, cols - 1, bottom)
    if cols < cur_cols or rows < cur_rows:
        # Shrink the window first: a buffer narrower than the window is refused.
        shrink = _SmallRect(0, win.Bottom - min(rows, cur_rows) + 1,
                            min(cols, cur_cols) - 1, win.Bottom)
        k32.SetConsoleWindowInfo(handle, True, ctypes.byref(shrink))
    k32.SetConsoleScreenBufferSize(handle, _Coord(cols, height))
    k32.SetConsoleWindowInfo(handle, True, ctypes.byref(target))


def console_zoom(delta: int) -> int | None:
    """Ctrl+Plus / Ctrl+Minus: grow or shrink the console font by one step,
    keep the window the same size on screen, and remember the size for the
    next launch. Returns the new height, or None where the font cannot be
    changed (not a classic console)."""
    global _ZOOM_ANCHOR_PX
    current = console_font_height()
    if current is None:
        return None
    new = max(_FONT_MIN, min(_FONT_MAX, current + _FONT_STEP * (1 if delta > 0 else -1)))
    if new == current:
        return current
    client_px = _console_client_px()
    # Anchor on the window size the user had before the FIRST zoom, so
    # repeated zooms return to exactly the same cell count instead of
    # drifting a column per round trip from integer rounding. A window the
    # user resized by hand (off by more than a cell) re-anchors.
    if client_px:
        if _ZOOM_ANCHOR_PX is None or any(abs(a - b) > 2 * current for a, b in zip(_ZOOM_ANCHOR_PX, client_px)):
            _ZOOM_ANCHOR_PX = client_px
        client_px = _ZOOM_ANCHOR_PX
    if not set_console_font_height(new):
        return current
    if client_px:
        _refit_console_cells(client_px)
    try:
        _FONT_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _FONT_STATE_PATH.write_text(str(new), encoding="utf-8")
    except OSError:
        pass
    return new


_USER_BG = "\033[48;5;237m"   # a shade above One Half Dark's background; subtle, not a box


def user_row(row: str, width: int) -> str:
    """One physical row of the user's echoed message with a light background
    band across `width` cells, the way Claude Code marks the user's turns
    so the eye finds turn boundaries while scanning. Plain when colour is
    off. Rows already wider than `width` (terminal auto-wrap) get the band
    on their text only."""
    if not _COLOR_ON:
        return row
    fill = max(0, width - _visible_cells(row))
    # The prompt's own reset ("\033[1m>\033[0m") would end the band after the
    # marker; re-arm the background after every reset inside the row.
    body = row.replace(C.RESET, C.RESET + _USER_BG)
    return f"{_USER_BG}{body}{' ' * fill}{C.RESET}"


def user_echo(content: str, width: int) -> str:
    """The full echo for a stored user message: `> first line`, then
    continuation rows prefixed like the editor's, each on a band."""
    from hexcli.lineedit import _wrap_words_visible  # lazy: lineedit imports ui
    lines = content.split("\n") or [""]
    logical = [f"{C.BOLD}>{C.RESET} {lines[0]}"]
    logical += [f"...  {line}" for line in lines[1:]]
    # Broken at spaces, exactly as the editor leaves a finished line, so a
    # redraw reproduces the echo row for row.
    rows = [r for line in logical for r in _wrap_words_visible(line, width).split("\n")]
    return "\n".join(user_row(r, width) for r in rows)


def clear_screen(scrollback: bool = True) -> None:
    """Wipe the window (and, by default, the terminal's scrollback) and put
    the cursor at the top-left."""
    sys.stdout.write("\033[2J" + ("\033[3J" if scrollback else "") + "\033[H\r")
    sys.stdout.flush()


def redraw_transcript(session: dict[str, Any], clear: bool = True,
                      pending: str | None = None) -> None:
    """Reprint the conversation at the current width, after clearing the
    screen unless the caller already laid out something above it.

    Used after a zoom or a resize: the column count changed, and the
    terminal's own reflow of what was already on screen starts
    continuation rows at column 0, losing the margin. Reprinting through
    the margin layer lays every row out fresh. Tool banners and spinner
    lines are not part of the session and do not come back; the questions
    and answers do. `pending` is the question of a turn still running,
    which joins the session only when the turn ends; it is echoed last.

    Spacing matches the live flow: an answer ends on a blank row, so only
    the first echo needs one above it.
    """
    if clear:
        clear_screen()
    first = True
    for msg in session.get("messages", []):
        role, content = msg.get("role"), str(msg.get("content", ""))
        if role == "user":
            if first:
                print()
            print(user_echo(content, _usable_width()))
            first = False
        elif role == "assistant":
            render_result("Result", content)
            first = False
    if pending is not None:
        if first:
            print()
        print(user_echo(pending, _usable_width()))


def apply_saved_console_font() -> None:
    """Restore the size chosen with Ctrl+Plus / Ctrl+Minus last time."""
    try:
        height = int(_FONT_STATE_PATH.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    if _FONT_MIN <= height <= _FONT_MAX and height != console_font_height():
        set_console_font_height(height)


# ---------------------------------------------------------------------------
# Tool / error / command event rendering
# ---------------------------------------------------------------------------

def tool_event(tag: str, detail: str) -> None:
    cprint(f"{C.GRAY}▸{C.RESET} {C.DIM}[{tag}] {detail}{C.RESET}")


def tool_header(tool_name: str) -> None:
    """One blank line, then the card. Everything the tool prints attaches
    below it with no further blank lines."""
    cprint(f"\n{C.BCYAN}◆{C.RESET} {C.BOLD}{tool_name}{C.RESET}")


def command_echo(command: str) -> None:
    cprint(f"{C.DIM}${C.RESET} {C.BOLD}{command}{C.RESET}")


def tool_error(message: str) -> None:
    """A failed tool call, on one dim red line under its card. The model
    still receives the full error text; the transcript gets the first line."""
    first = str(message).strip().splitlines()[0] if str(message).strip() else "failed"
    # A first line that introduces detail on later lines ("...(similarity
    # 48%):") would end on a bare colon here; the detail is for the model.
    first = first.rstrip().rstrip(":").rstrip()
    cprint(f"{C.RED}▸ error{C.RESET} {first}", file=sys.stderr)


def _usable_width() -> int:
    try:
        width = os.get_terminal_size().columns
    except OSError:
        width = 80
    pad = getattr(sys.stdout, "pad", 0) or 0
    return max(20, width - 2 * int(pad))


def error_box(message: str, *, file: Any = None) -> None:
    lines = str(message).strip().splitlines() or [""]
    width = min(max(len(ln) for ln in lines) + 4, _usable_width() - 2)
    out = file or sys.stderr
    cprint("┌" + "─" * width, C.RED, file=out)
    for ln in lines:
        cprint(f"│ {ln}", C.RED, file=out)
    cprint("└" + "─" * width, C.RED, file=out)


def print_banner(model: str, backend: str, engine: str | None = None) -> None:
    """The one banner. `engine` names the hardware ("Hexagon NPU"); without
    it the line falls back to naming the server, never the transport."""
    title = "HEX CLI"
    try:
        cols = os.get_terminal_size().columns
    except OSError:
        cols = 80
    # Fit the box to the window so a narrow terminal does not wrap the rules
    # into broken fragments. Below the frame's minimum, drop it for a plain
    # heading.
    inner = max(len(title) + 4, 44)
    avail = cols - 6   # side padding + the two border cells, with slack
    print()
    if avail >= inner:
        cprint("┌" + "─" * inner + "┐", C.BCYAN)
        cprint("│" + title.center(inner) + "│", C.BOLD + C.BCYAN)
        cprint("└" + "─" * inner + "┘", C.BCYAN)
    elif avail >= len(title) + 2:
        w = max(len(title) + 2, avail)
        cprint("┌" + "─" * w + "┐", C.BCYAN)
        cprint("│" + title.center(w) + "│", C.BOLD + C.BCYAN)
        cprint("└" + "─" * w + "┘", C.BCYAN)
    else:
        cprint(title, C.BOLD + C.BCYAN)
    where = f"on the {engine}" if engine else f"via {backend}"
    usable = _usable_width()
    for tail in (f"{model} {where}  ·  /help  ·  press Esc to cancel",
                 f"{model} {where}  ·  /help",
                 f"{model}  ·  /help"):
        if len(tail) + 2 <= usable:
            break
    styled = tail.replace(model, f"{C.BWHITE}{model}{C.RESET}{C.DIM}", 1)
    cprint(f"  {styled}", C.DIM)
    print()


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = textwrap.dedent("""
    Hex CLI, a local agent on the Hexagon NPU

    Session
      /new                    start a new session, keep the screen
      /clear                  clear the screen and start a new session
      /history                list saved sessions
      /resume <n>             reopen session n
      /search <text>          find sessions by content
      /compact                compress the chat history
      /undo                   revert the last exchange, including files it wrote
      /diff                   show what the agent changed this turn

    Status
      /context                how full the context is and when it compacts
      /stats                  turns, time and tokens for this session
      /doctor                 check the install
      /tools                  list the agent's tools

    Setup
      /config [key [value]]   view or set a config value for this session
      /setup                  config wizard, writes the config file
      /memory [status|list|search|clear|prune]   the memory store
      /cwd [path]             show or change the working directory
      /exit                   quit

    Custom commands: a .md file in .shellai/commands/ or ~/.shellai/commands/
    becomes /<filename> and its text is sent as the prompt. $ARGUMENTS is
    replaced with what follows the command. Built-in names win.

    Keys
      Up / Down                 history, filtered by what is typed
      Tab                       complete commands, config keys and paths
      Shift+Enter               new line; \\ then Enter also works
      Ctrl+Left / Ctrl+Right    move by word
      Ctrl+W / Ctrl+U / Ctrl+K  delete the word, to line start, to line end
      Esc                       clear the line, or cancel a running turn
      Ctrl+L                    clear the screen
      Ctrl+Plus / Ctrl+Minus    text size in the classic console

    The agent runs qwen3-4b-instruct-2507 on the Hexagon NPU through npurun.
""").strip()

TOOLS_HELP = textwrap.dedent("""
    Tools available to the agent:
      run_command(command)                      run PowerShell; risky ones ask first
      read_file(path, offset, limit)            read a file, paged by offset and limit
      edit_file(path, old_string, new_string)   replace text; undoable
      write_file(path, content)                 write a file; undoable
      append_file(path, content)                append to a file
      list_directory(path)                      list files and folders
      search_files(pattern, path, glob)         search file contents
      find_files(glob, path)                    find files by glob
      verify_syntax(path, language)             syntax check for .py .json .ps1 .js
      run_code(path, args, timeout)             run a script in a sandbox
      lint_code(path)                           run ruff; needs ruff on PATH
      search_memory(query, top_k)               recall prior sessions
      fetch_url(url, max_chars)                 fetch a URL as readable text
      batch(actions)                            up to 8 read-only tools at once
      delegate(task)                            a sub-agent of up to 5 steps

    Writes stay in the working directory; see workspace_write_scope.
""").strip()


# ---------------------------------------------------------------------------
# History list
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(UTC)


def format_relative_time(timestamp: str) -> str:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((_utc_now() - moment).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def truncate_summary(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def render_history_list(sessions: list[dict[str, Any]], current_id: str) -> None:
    if not sessions:
        cprint("  No saved sessions.", C.DIM)
        return
    print()
    # The summary column takes whatever the window leaves after the fixed
    # columns (marker, number, two dates), so rows never wrap.
    width = max(16, _usable_width() - 30)
    header = f"  {'#':<4}{'Session':<{width + 4}}{'Modified':<12}{'Created'}"
    cprint(header, C.BOLD)
    cprint("  " + "─" * (len(header) - 2), C.DIM)
    for i, s in enumerate(sessions, start=1):
        current = s.get("id") == current_id
        marker = "▸" if current else " "
        summary = truncate_summary(str(s.get("title", "New session")), width)
        modified = format_relative_time(str(s.get("modified_at", "")))
        created = format_relative_time(str(s.get("created_at", "")))
        cprint(f"{marker} {i:>2}. {summary:<{width}}  {modified:<10}  {created}", C.BOLD if current else "")
    print()


def render_search_results(term: str, hits: list[dict[str, Any]]) -> None:
    """Render /search hits: same numbering as /history, matches highlighted."""
    if not hits:
        cprint(f'  No sessions match "{term}".', C.DIM)
        return
    print()
    cprint(f'  Sessions matching "{term}"', C.BOLD)
    for h in hits:
        s = h["session"]
        summary = truncate_summary(str(s.get("title", "New session")), 48)
        modified = format_relative_time(str(s.get("modified_at", "")))
        print()
        cprint(f"  {h['index']:>2}. {summary}  {C.DIM}{modified}{C.RESET}")
        for role, prefix, match, suffix in h["snippets"]:
            print(f"      {C.DIM}{role}{C.RESET}  {prefix}"
                  f"{C.BOLD}{C.BYELLOW}{match}{C.RESET}{suffix}")
    print()
    cprint("  /resume <n> reopens one.", C.DIM)
    print()


# ---------------------------------------------------------------------------
# Models list
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Context estimate
# ---------------------------------------------------------------------------

def show_context(
    session: dict[str, Any],
    config: dict[str, Any],
    budget: tuple[int, int] | None = None,
    system_prompt_tokens: int | None = None,
) -> None:
    """Show context usage against the REAL per-turn budget.

    Pre-v1.8 this printed hardcoded 1,300/1,600 thresholds that were calibrated
    to a system prompt half the actual size, so it told users they had headroom
    they did not have. Thresholds now come from the caller's measured budget.
    """
    messages: list[dict[str, str]] = session.get("messages", [])
    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = total_chars // 4
    compact_count = session.get("compact_count", 0)
    warn, crit = budget if budget else (1_300, 1_600)
    print()
    cprint("Context estimate", C.BOLD)
    print(f"  Messages:         {len(messages)}")
    print(f"  Chars (total):    {total_chars:,}")
    print(f"  History (est.):   ~{est_tokens:,} tokens  (budget {warn:,}, "
          f"{context_gauge(min(100, round(100 * est_tokens / max(warn, 1))))})")
    if system_prompt_tokens:
        print(f"  System prompt:    ~{system_prompt_tokens:,} tokens")
        print(f"  Turn total:       ~{est_tokens + system_prompt_tokens:,} tokens")
    print(f"  Compact runs:     {compact_count}")
    print(f"  Max agent steps:  {config.get('max_agent_steps', 15)}")
    print(f"  Model:            {config.get('model', 'unknown')}")
    if est_tokens >= warn:
        cprint("  ⚠ Auto-compact runs after the next turn.", C.BYELLOW)
    print()


def show_context_brief(
    session: dict[str, Any],
    config: dict[str, Any],
    budget: tuple[int, int],
    system_prompt_tokens: int,
    history_tokens: int,
) -> None:
    """/context — just the numbers that decide what happens next."""
    messages: list[dict[str, str]] = session.get("messages", [])
    warn, _ = budget
    pct = max(0, min(100, round(100 * history_tokens / max(warn, 1))))
    window = int(config.get("context_window_tokens") or 0)
    compact_count = int(session.get("compact_count", 0))
    if history_tokens >= warn:
        nxt = "auto-compact runs after the next turn"
    else:
        nxt = f"auto-compact after about {warn - history_tokens:,} more tokens"
    print()
    cprint(f"  Context  {context_gauge(pct)}", C.BOLD)
    print(f"    history        {history_tokens:,} / {warn:,} tokens, {len(messages)} messages")
    print(f"    system prompt  {system_prompt_tokens:,} tokens")
    if window:
        print(f"    server budget  {window:,} tokens per call")
    if compact_count:
        print(f"    compactions    {compact_count}")
    print(f"    next           {nxt}")
    print()


# ---------------------------------------------------------------------------
# REPL prompt
# ---------------------------------------------------------------------------

def get_git_branch() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        branch = out.decode().strip()
        return branch if branch and branch != "HEAD" else None
    except Exception:
        return None


def short_cwd() -> str:
    cwd = Path.cwd()
    home = Path.home()
    try:
        rel = cwd.relative_to(home)
        return "~\\" + str(rel) if str(rel) != "." else "~"
    except ValueError:
        return str(cwd)


_GAUGE_GLYPHS = "○◔◑◕●"
# The quarter-pie glyphs are in Cascadia (Windows Terminal) but not in
# Consolas, and classic conhost does not fall back — the Start Menu
# shortcut runs there. Percentage only in that case, not boxes.
_PIE_OK = os.name != "nt" or bool(os.environ.get("WT_SESSION"))


def context_gauge(percent: int, pie: bool | None = None) -> str:
    """A five-step pie glyph plus the number: ○ 0%  ◔ 25%  ◑ 50%  ◕ 75%  ● 100%
    (just the number where the console font cannot draw the pie)."""
    pct = max(0, min(100, int(percent)))
    if not (_PIE_OK if pie is None else pie):
        return f"{pct}%"
    glyph = _GAUGE_GLYPHS[min(4, (pct + 12) // 25)]
    return f"{glyph} {pct}%"


def repl_prompt(config: dict[str, Any], context_percent: int | None = None,
                boxed: bool = False) -> str:
    """The input prompt. `boxed` is the status-bar layout: the bar carries
    the location and the gauge, so the prompt is just the marker. Without
    it the header line keeps the model, location and gauge as before."""
    if boxed:
        return f"{C.BOLD}>{C.RESET} " if _COLOR_ON else "> "
    model = str(config.get("model", "?"))
    cwd_str = short_cwd()
    branch = get_git_branch()
    branch_str = f" ({branch})" if branch else ""
    gauge = context_gauge(context_percent) if context_percent is not None else ""
    if _COLOR_ON:
        if gauge:
            tone = C.BRED if context_percent >= 100 else C.BYELLOW if context_percent >= 75 else C.DIM
            gauge = f"{C.DIM} | {tone}{gauge}"
        return (
            f"{C.DIM}[{C.BCYAN}{model}{C.DIM} | "
            f"{C.BYELLOW}{cwd_str}{branch_str}{gauge}{C.DIM}]{C.RESET}\n"
            f"{C.BOLD}>{C.RESET} "
        )
    if gauge:
        gauge = f" | {gauge}"
    return f"[{model} | {cwd_str}{branch_str}{gauge}]\n> "


# ---------------------------------------------------------------------------
# Consent prompts
# ---------------------------------------------------------------------------

CONFIRM_TIMEOUT_S: float = 120.0


def confirm_or_deny(prompt: str, timeout_s: float | None = None) -> bool:
    """Ask for y/N consent without ever blocking forever; anything but an explicit
    yes is a deny.

    Three ways there is no human to answer, all of which must fail closed:
      * stdin is a pipe / redirected file -> isatty() is False, deny at once;
      * stdin is at EOF -> the read yields nothing, deny;
      * stdin is a **hidden or detached console** -> isatty() is True and a normal
        read never returns. Not hypothetical: this shape hung an unattended eval
        for 7.5 hours on one prompt.

    That third case rules out both obvious implementations. ``input()`` blocks
    forever, and running it on a daemon thread does NOT help, because the Windows
    console read holds the GIL - the main thread never runs, so ``join(timeout)``
    is itself blocked (measured: a 3 s join took 60 s). So poll ``msvcrt`` for a
    keypress instead, the same way ``lineedit`` reads keys, and give up on time.

    The timeout is an **idle** timeout, reset by every keypress. A fixed deadline
    would also fire on an attended human who is mid-answer or reading the command
    carefully, throwing away characters they had already typed; only *silence*
    indicates the dead-console case this exists for.

    **Ctrl-C denies, it does not raise.** These prompts guard destructive and
    sensitive commands, where Ctrl-C is the most natural way to say "no". Raising
    would abort the whole turn, skipping the audit-log entry that records the
    refusal and discarding the turn's undo snapshots.
    """
    if timeout_s is None:  # resolved per call so the constant stays patchable
        timeout_s = CONFIRM_TIMEOUT_S
    if not sys.stdin.isatty():
        return False
    # Take the status box down while we own the console for the y/N read, so
    # the question is not printed on top of a still-live input box (nested
    # inside a confirm_* wrapper this is already down, so it is a no-op).
    with paused_status():
        allowed = _confirm_or_deny_read(prompt, timeout_s)
        cprint("  Allowed." if allowed else "  Denied.", C.DIM)
        return allowed


def ask_line(prompt: str, timeout_s: float | None = None) -> str | None:
    """Read one line at an inline prompt the way the confirms do: keys are
    polled and echoed through our own streams, so the status box can be
    lowered around it and the margin's column stays right (``input()`` echoes
    through the console itself, which left the box jumbled after Enter).
    Returns the text, or None when no human answered: not a console, Ctrl-C,
    or the idle timeout. Callers choose their own default for None."""
    if timeout_s is None:
        timeout_s = CONFIRM_TIMEOUT_S
    if not sys.stdin.isatty():
        return None
    with paused_status():
        # No note of its own: the caller says what a None answer meant.
        return _console_read_line(prompt, timeout_s, cancel_note="", timeout_suffix=".")


def _confirm_or_deny_read(prompt: str, timeout_s: float) -> bool:
    answer = _console_read_line(prompt, timeout_s, cancel_note="", timeout_suffix=".")
    return answer is not None and answer.strip().lower() in {"y", "yes"}


def _console_read_line(prompt: str, timeout_s: float, cancel_note: str,
                       timeout_suffix: str) -> str | None:
    sys.stdout.write(prompt)
    sys.stdout.flush()
    deadline = time.monotonic() + timeout_s
    buf = ""
    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        ch = msvcrt.getwch()
        deadline = time.monotonic() + timeout_s  # a human is here; start the clock over
        if ch in ("\r", "\n"):
            print()
            return buf
        if ch == "\x03":  # Ctrl-C — an emphatic no, not a crash
            print()
            if cancel_note:
                cprint(cancel_note, C.DIM)
            return None
        if ch in ("\b", "\x7f"):
            if buf:
                buf = buf[:-1]
                # Erase the echo too, or the screen shows an answer that is not
                # the one being evaluated.
                sys.stdout.write("\b \b")
                sys.stdout.flush()
            continue
        if ch == "\x00" or ch == "\xe0":  # function/arrow key: consume the scan code
            msvcrt.getwch()
            continue
        buf += ch
        sys.stdout.write(ch)
        sys.stdout.flush()
    print()
    cprint(f"  No answer after {timeout_s:.0f} s{timeout_suffix}", C.DIM)
    return None


def confirm_network_fetch(url: str) -> bool:
    """Outbound network access is the exception in an offline-first product;
    require explicit consent per fetch. Denied when non-interactive."""
    with paused_status():
        print()
        cprint("⚠ The agent wants to fetch a URL:", C.BYELLOW, bold=True)
        cprint(f"  {url}", C.CYAN)
        return confirm_or_deny("  Allow? [y/N] ")


def confirm_sensitive_command(cmd: str) -> bool:
    """Sensitive-data access (keys, credentials, security files, obfuscated
    execution) requires explicit consent; denied when non-interactive."""
    with paused_status():
        print()
        cprint("⚠ The agent wants to read sensitive data or run an obfuscated command:", C.BYELLOW, bold=True)
        cprint(f"  {cmd}", C.RED)
        cprint("  Deny unless you asked for exactly this.", C.DIM)
        return confirm_or_deny("  Allow? [y/N] ")


def confirm_destructive_command(cmd: str) -> bool:
    """Print a destructive-command warning and return True only if the user types
    y/yes; denied when non-interactive or unanswered."""
    with paused_status():
        print()
        cprint("⚠ The agent wants to run a destructive command:", C.BYELLOW, bold=True)
        cprint(f"  {cmd}", C.RED)
        return confirm_or_deny("  Allow? [y/N] ")


def render_result(title: str, body: str) -> None:
    """An answer that did not stream (or streamed differently). Printed the
    way a streamed answer lands: a blank line, the text, a blank line. The
    `title` is kept for callers but not shown; streamed answers carry none."""
    from hexcli.markdown_stream import render_markdown  # lazy: it imports C from here
    print()
    print(render_markdown(body))
    print()
