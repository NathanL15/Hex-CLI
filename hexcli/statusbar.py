#!/usr/bin/env python3
"""hexcli.statusbar — the bottom section: an input box above a status line.

The REPL used to print its prompt inline, so the place you type moved with
the transcript and the context gauge sat in the prompt header. This module
gives the terminal the Claude Code shape instead: the transcript scrolls
above, and the last rows of the screen are always

    ────────────────────────────────────────────────────────────
    > the line being typed
    ────────────────────────────────────────────────────────────
    ⠹ thinking   context ◔ 32%   npu 96%   mem 10.1 GB      ~\\proj (main)

How it stays at the bottom
--------------------------
No scroll region and no full-screen redraw: the transcript keeps going into
the terminal's scrollback exactly as before. Two states share the drawing:

* While the line editor is active it renders the box itself (``chrome`` in
  ``lineedit.LineEditor``): the rule above, the rule and status below, the
  cursor on the input row. An idle tick from the key reader re-renders when
  the status text changes, so the numbers move while nothing is typed.
* Between reads (a turn is running, a command is printing) ``LiveArea``
  owns the box. It wraps ``sys.stdout``/``sys.stderr``: every write first
  erases the box (the cursor is parked at the transcript position, so one
  clear-to-end-of-screen does it), writes the text, then redraws the box
  below the new cursor and walks back up. The spinner's frames and the
  metrics refresh through the same repaint. All of it is serialised on one
  lock, since the spinner ticks from its own thread.

The wrappers sit outside ``ui._MarginStream`` and borrow its column
bookkeeping to put the cursor back where the transcript left off.

Metrics
-------
``npu`` is Windows' own NPU load: the ``GPU Engine`` performance counters
for the adapter that DirectX does not list (the NPU is an MCDM compute
device; Task Manager's NPU graph reads the same counters). Read through
``pdh.dll`` with ctypes, no dependency. Measured on the Hexagon: 0 % idle,
90–97 % during decode, attributed to npurun's pid. ``mem`` is physical
memory in use system-wide (``GlobalMemoryStatusEx``). Both are sampled once
a second on a daemon thread that never touches the terminal.
"""
from __future__ import annotations

import os
import re
import sys
import threading
import time
from collections.abc import Callable
from typing import Any

from hexcli import ui
from hexcli.ui import C

_ANSI_RE = re.compile(r"\033\[[0-9;?]*[A-Za-z]")
_SAMPLE_INTERVAL_S = 1.0
_REDISCOVER_S = 30.0
_LUID_RE = re.compile(r"luid_(0x[0-9A-Fa-f]+_0x[0-9A-Fa-f]+).*engtype_(.+)$")

# ── text helpers ─────────────────────────────────────────────────────────────


def visible_len(text: str) -> int:
    """Terminal cells, not characters: CJK and other wide glyphs take two."""
    return ui._visible_cells(text)


def clip_visible(text: str, width: int) -> str:
    """Cut `text` to `width` visible cells, keeping ANSI styling intact and
    closing it so nothing leaks onto the next row."""
    if width <= 0:
        return ""
    out: list[str] = []
    seen = 0
    i = 0
    while i < len(text):
        m = _ANSI_RE.match(text, i)
        if m:
            out.append(m.group())
            i = m.end()
            continue
        w = ui._cell_width(text[i])
        if seen + w > width:
            seen = width
            break
        out.append(text[i])
        seen += w
        i += 1
    clipped = "".join(out)
    if seen >= width and i < len(text) and C.RESET:
        clipped += C.RESET
    return clipped


# ── metrics ──────────────────────────────────────────────────────────────────


def pick_npu_luid(engines: dict[str, set[str]], known: set[str]) -> str | None:
    """The NPU's adapter LUID, from the GPU Engine instance set.

    DirectX registers the GPU and the Basic Render Driver under
    ``HKLM\\SOFTWARE\\Microsoft\\DirectX`` with their LUIDs; the NPU is not
    there. Of the LUIDs left over, prefer the one that exposes only a
    Compute engine, which is what an MCDM NPU looks like.
    """
    known_l = {k.lower() for k in known}
    rest = [luid for luid in engines if luid.lower() not in known_l]
    if not rest:
        return None
    compute_only = [luid for luid in rest if engines[luid] == {"Compute"}]
    return (compute_only or rest)[0]


def _directx_luids() -> set[str]:
    out: set[str] = set()
    try:
        import winreg
    except ImportError:
        return out
    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\DirectX") as key:
            i = 0
            while True:
                try:
                    name = winreg.EnumKey(key, i)
                except OSError:
                    break
                i += 1
                try:
                    with winreg.OpenKey(key, name) as sub:
                        value, _ = winreg.QueryValueEx(sub, "AdapterLuid")
                    out.add(f"0x{int(value) >> 32:08X}_0x{int(value) & 0xFFFFFFFF:08X}")
                except OSError:
                    continue
    except OSError:
        pass
    return out


class _NpuCounter:
    """One open PDH query over ``\\GPU Engine(*luid_<npu>*)\\Utilization Percentage``."""

    _FMT_DOUBLE = 0x200

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ct = ctypes
        self._wt = wintypes
        self._pdh = ctypes.windll.pdh
        self._query = ctypes.c_void_p()
        self._counter = ctypes.c_void_p()
        self.luid: str | None = None

        class _Fmt(ctypes.Structure):
            _fields_ = [("CStatus", wintypes.DWORD), ("doubleValue", ctypes.c_double)]

        class _Item(ctypes.Structure):
            _fields_ = [("szName", ctypes.c_wchar_p), ("FmtValue", _Fmt)]

        self._item = _Item

    def _engine_luids(self) -> dict[str, set[str]]:
        ct, wt, pdh = self._ct, self._wt, self._pdh
        clen, ilen = wt.DWORD(0), wt.DWORD(0)
        pdh.PdhEnumObjectItemsW(None, None, "GPU Engine", None, ct.byref(clen),
                                None, ct.byref(ilen), 400, 0)
        if not ilen.value:
            return {}
        cbuf = ct.create_unicode_buffer(max(clen.value, 1))
        ibuf = ct.create_unicode_buffer(ilen.value)
        if pdh.PdhEnumObjectItemsW(None, None, "GPU Engine", cbuf, ct.byref(clen),
                                   ibuf, ct.byref(ilen), 400, 0):
            return {}
        engines: dict[str, set[str]] = {}
        for name in ct.wstring_at(ibuf, ilen.value).split("\0"):
            m = _LUID_RE.search(name)
            if m:
                engines.setdefault(m.group(1), set()).add(m.group(2))
        return engines

    def open(self) -> bool:
        """Find the NPU and add its counter. False when there is none."""
        luid = pick_npu_luid(self._engine_luids(), _directx_luids())
        if luid is None:
            return False
        ct, pdh = self._ct, self._pdh
        if pdh.PdhOpenQueryW(None, 0, ct.byref(self._query)):
            return False
        path = f"\\GPU Engine(*luid_{luid}*)\\Utilization Percentage"
        if pdh.PdhAddEnglishCounterW(self._query, path, 0, ct.byref(self._counter)):
            pdh.PdhCloseQuery(self._query)
            self._query = ct.c_void_p()
            return False
        self.luid = luid
        pdh.PdhCollectQueryData(self._query)   # rate counter: first sample primes it
        return True

    def read(self) -> float:
        """Percent busy since the previous read, summed over every process."""
        ct, wt, pdh = self._ct, self._wt, self._pdh
        if pdh.PdhCollectQueryData(self._query):
            return 0.0
        size, count = wt.DWORD(0), wt.DWORD(0)
        pdh.PdhGetFormattedCounterArrayW(self._counter, self._FMT_DOUBLE,
                                         ct.byref(size), ct.byref(count), None)
        if not size.value:
            return 0.0
        buf = ct.create_string_buffer(size.value)
        if pdh.PdhGetFormattedCounterArrayW(self._counter, self._FMT_DOUBLE,
                                            ct.byref(size), ct.byref(count), buf):
            return 0.0
        items = ct.cast(buf, ct.POINTER(self._item))
        total = sum(items[i].FmtValue.doubleValue for i in range(count.value))
        return max(0.0, min(100.0, total))

    def close(self) -> None:
        if self._query:
            self._pdh.PdhCloseQuery(self._query)
            self._query = self._ct.c_void_p()


def memory_status() -> tuple[float, float] | None:
    """(used GiB, total GiB) of physical memory, or None off Windows."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _MemStat(ctypes.Structure):
        _fields_ = [("dwLength", wintypes.DWORD), ("dwMemoryLoad", wintypes.DWORD),
                    ("ullTotalPhys", ctypes.c_uint64), ("ullAvailPhys", ctypes.c_uint64),
                    ("ullTotalPageFile", ctypes.c_uint64), ("ullAvailPageFile", ctypes.c_uint64),
                    ("ullTotalVirtual", ctypes.c_uint64), ("ullAvailVirtual", ctypes.c_uint64),
                    ("ullAvailExtendedVirtual", ctypes.c_uint64)]

    stat = _MemStat()
    stat.dwLength = ctypes.sizeof(stat)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat)):
        return None
    total = stat.ullTotalPhys / 2 ** 30
    return (stat.ullTotalPhys - stat.ullAvailPhys) / 2 ** 30, total


class SystemSampler:
    """Background sampler for the status line. ``snapshot()`` is cheap and
    lock-free (one tuple swap); the thread never writes to the terminal."""

    def __init__(self, interval: float = _SAMPLE_INTERVAL_S,
                 on_update: Callable[[], None] | None = None) -> None:
        self.interval = interval
        # Called after each fresh sample so the status line can refresh even
        # when nothing else writes to the terminal (a long tool subprocess
        # with no spinner). The callback must be cheap and take its own lock.
        self.on_update = on_update
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        # (npu percent or None, mem used GiB or None, mem total GiB or None)
        self._snap: tuple[float | None, float | None, float | None] = (None, None, None)

    def snapshot(self) -> tuple[float | None, float | None, float | None]:
        return self._snap

    def start(self) -> None:
        if self._thread is not None or os.name != "nt":
            return
        self._thread = threading.Thread(target=self._run, name="hexcli-statusbar", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        counter: _NpuCounter | None = None
        next_discover = 0.0
        while not self._stop.is_set():
            npu: float | None = None
            try:
                if counter is None and time.monotonic() >= next_discover:
                    probe = _NpuCounter()
                    if probe.open():
                        counter = probe
                    else:
                        next_discover = time.monotonic() + _REDISCOVER_S
                if counter is not None:
                    npu = counter.read()
            except Exception:  # noqa: BLE001 — a broken counter must never take the REPL down
                counter = None
                next_discover = time.monotonic() + _REDISCOVER_S
            mem = None
            try:
                mem = memory_status()
            except Exception:  # noqa: BLE001
                pass
            self._snap = (npu, mem[0] if mem else None, mem[1] if mem else None)
            if self.on_update is not None:
                try:
                    self.on_update()
                except Exception:  # noqa: BLE001 — never let a repaint kill the sampler
                    pass
            self._stop.wait(self.interval)
        if counter is not None:
            counter.close()


# ── the status line ──────────────────────────────────────────────────────────


def status_line(
    width: int,
    *,
    context_percent: int | None,
    npu_percent: float | None,
    mem_used_gb: float | None,
    mem_total_gb: float | None = None,
    activity: str | None = None,
    frame: str = "",
    right: str = "",
) -> str:
    """One row: activity (when a turn is running), the three metrics, and
    the location right-aligned when it fits. Never wider than `width`."""
    parts: list[str] = []
    if activity:
        head = f"{C.BCYAN}{frame}{C.RESET} " if frame else ""
        parts.append(f"{head}{C.DIM}{activity}{C.RESET}")
    if context_percent is not None:
        pct = max(0, min(100, int(context_percent)))
        tone = C.BRED if pct >= 100 else C.BYELLOW if pct >= 75 else C.DIM
        parts.append(f"{tone}context {ui.context_gauge(pct)}{C.RESET}")
    if npu_percent is not None:
        tone = C.BCYAN if npu_percent >= 50 else C.DIM
        parts.append(f"{tone}npu {npu_percent:.0f}%{C.RESET}")
    if mem_used_gb is not None:
        hot = mem_total_gb is not None and mem_total_gb > 0 and mem_used_gb / mem_total_gb >= 0.9
        tone = C.BYELLOW if hot else C.DIM
        total = f"/{mem_total_gb:.1f}" if mem_total_gb else ""
        parts.append(f"{tone}mem {mem_used_gb:.1f}{total} GB{C.RESET}")
    left = "   ".join(parts)
    left_vis = visible_len(left)
    if right:
        gap = width - left_vis - visible_len(right)
        if gap >= 2:
            return left + " " * gap + f"{C.DIM}{right}{C.RESET}"
    return clip_visible(left, width)


def rule(width: int) -> str:
    return f"{C.DIM}{'─' * max(0, width)}{C.RESET}"


# ── the live area ────────────────────────────────────────────────────────────


def console_geometry() -> tuple[int, int] | None:
    """(cursor row within the window, window height) from the console, or
    None off Windows / off a console. The box is pinned to the window's
    last rows with this: the transcript is padded down to it."""
    if os.name != "nt":
        return None
    import ctypes
    from ctypes import wintypes

    class _Coord(ctypes.Structure):
        _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]

    class _Rect(ctypes.Structure):
        _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                    ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]

    class _Info(ctypes.Structure):
        _fields_ = [("dwSize", _Coord), ("dwCursorPosition", _Coord), ("wAttributes", wintypes.WORD),
                    ("srWindow", _Rect), ("dwMaximumWindowSize", _Coord)]

    k32 = ctypes.windll.kernel32
    k32.GetStdHandle.restype = ctypes.c_void_p
    info = _Info()
    if not k32.GetConsoleScreenBufferInfo(ctypes.c_void_p(k32.GetStdHandle(-11)), ctypes.byref(info)):
        return None
    height = info.srWindow.Bottom - info.srWindow.Top + 1
    row = info.dwCursorPosition.Y - info.srWindow.Top
    if height <= 0 or row < 0 or row >= height:
        return None
    return row, height


class LiveArea:
    """The box between reads. See the module docstring for the mechanics.

    `margin` is the ``ui._Margin`` the wrapped streams pad and wrap with; its
    `col` is where the transcript cursor sits on its row, which is what lets
    a repaint put the cursor back after drawing rows below it.
    """

    def __init__(
        self,
        margin: Any,
        context_percent: Callable[[], int | None],
        sampler: SystemSampler | None = None,
        prompt: str = "> ",
        geometry: Callable[[], tuple[int, int] | None] | None = None,
    ) -> None:
        self.margin = margin
        self._context_percent = context_percent
        self.sampler = sampler or SystemSampler()
        self.prompt = prompt
        self._geometry = geometry or console_geometry
        self.editor_rows = 4       # rule, input row, rule, status: what the editor draws
        self._pad_top: int | None = None   # first row of the blank pad above the conversation
        self._pad_above = 0                # how many pad rows there are
        self._last_shape: tuple[Any, int] | None = None   # (window height, usable width) at the last draw
        # A resize while a turn runs: the owner reprints the banner and the
        # stored conversation (calling the `pin` it is handed in between,
        # which puts the box up and the pad under the banner); the turn's
        # own output so far is then replayed from `_turn_log`.
        self.on_relayout: Callable[[Callable[[], None]], None] | None = None
        self._turn_log: list[str] = []
        self._turn_log_len = 0
        self._in_turn = False
        self._relaying = False
        self.lock = threading.RLock()
        self.enabled = False
        self.activity: str | None = None
        self._activity_since: float | None = None   # when the current turn's activity began
        self.frame = ""
        self.location = ""
        self._drawn = 0            # rows currently on screen below the transcript
        self._inner: Any = None    # the stream repaints go through (set by install)
        self._signature: Any = None  # what _draw last put on screen, for repaint skips

    # -- content --------------------------------------------------------------

    def refresh_location(self) -> None:
        """cwd and branch for the right edge. Runs git, so only per read."""
        try:
            branch = ui.get_git_branch()
            cwd = ui.short_cwd()
        except Exception:  # noqa: BLE001
            self.location = ""
            return
        self.location = f"{cwd} ({branch})" if branch else cwd

    def status(self, width: int) -> str:
        npu, used, total = self.sampler.snapshot()
        try:
            pct = self._context_percent()
        except Exception:  # noqa: BLE001
            pct = None
        activity = self.activity
        if activity:
            # While a turn runs: the label with its elapsed time. The cancel
            # hint lives in the banner only (the owner asked for one, not two).
            elapsed = int(time.monotonic() - (self._activity_since or time.monotonic()))
            activity = f"{activity} {elapsed}s" if elapsed >= 1 else activity
        return status_line(
            width, context_percent=pct, npu_percent=npu, mem_used_gb=used,
            mem_total_gb=total, activity=activity, frame=self.frame,
            right=self.location,
        )

    def chrome(self, width: int) -> tuple[list[str], list[str]]:
        """What the line editor draws above and below the input row.

        One cell short of the width: a row that exactly fills it is the
        deferred-wrap case the editor pads with a spare row (lineedit,
        ``_pad``), which showed up as a blank line under every rule."""
        w = max(1, width - 1)
        return [rule(w)], [rule(w), self.status(w)]

    def rows(self, width: int) -> list[str]:
        """The whole box while nothing is being typed."""
        above, below = self.chrome(width)
        return above + [f"{C.DIM}{self.prompt.rstrip()}{C.RESET}"] + below

    # -- drawing --------------------------------------------------------------

    def _erase(self, inner: Any) -> None:
        # Straight to the base stream: the margin layer would otherwise file
        # the escape under its current word and replay it on a reflow.
        if self._drawn:
            inner._base.write("\033[J")
            self._drawn = 0

    def screen_cleared(self) -> None:
        """The screen was cleared by someone else (cls, a transcript redraw):
        nothing of ours is on it any more."""
        with self.lock:
            self._drawn = 0
            self._signature = None
            self.reset_pad()

    def note_scroll(self, rows: int) -> None:
        """The window scrolled up by `rows` (the editor grew past the bottom
        with a multi-line entry): the pad moved up with it, and any part of
        it that left the window is gone."""
        with self.lock:
            if rows <= 0 or self._pad_top is None:
                return
            self._pad_top -= rows
            if self._pad_top < 0:
                self._pad_above = max(0, self._pad_above + self._pad_top)
                self._pad_top = 0
            if self._pad_above == 0:
                self._pad_top = None

    def _geometry_changed(self) -> bool:
        """True when the window or the usable width differs from the last
        draw: the terminal reflowed, so the pad rows are not where we think."""
        geo = self._geo()
        now = (geo[1] if geo else None, self.margin.usable)
        changed = self._last_shape is not None and now != self._last_shape
        self._last_shape = now
        return changed

    def _rows_below_cursor(self) -> int | None:
        geo = self._geo()
        if geo is None:
            return None
        row, height = geo
        return height - 1 - row

    # The pad: blank rows between the content at the top of the window (the
    # banner, or whatever scrolled there) and the conversation, which is
    # anchored just above the box. New lines appear at the bottom and the
    # conversation grows UPWARD into the pad: each newline deletes one pad
    # row at its top (ESC[M) so everything below shifts up one, while the
    # banner keeps its place. Only when the pad is gone does a newline
    # scroll the whole window, taking the banner into scrollback. Pinning
    # the box when the cursor is high (start-up, a taller window) inserts
    # pad rows (ESC[L) at the pad's top, which is the cursor row when there
    # is no pad yet, so the banner above never moves.

    def _flush_base(self) -> None:
        """Cursor moves are escape sequences with no newline; a line-buffered
        stdout still holds them, so the console would report the cursor
        from before the move. Flush before every geometry read."""
        inner = self._inner
        base = getattr(inner, "_base", inner)
        try:
            base.flush()
        except Exception:  # noqa: BLE001
            pass

    def _geo(self) -> tuple[int, int] | None:
        self._flush_base()
        try:
            return self._geometry()
        except Exception:  # noqa: BLE001
            return None

    def _cursor_col(self) -> int:
        return int(self.margin.pad) + int(self.margin.col)

    def reset_pad(self) -> None:
        """Forget the pad after something redrew the screen (a resize, a
        zoom redraw): its rows are no longer where they were."""
        self._pad_top = None
        self._pad_above = 0

    def _insert_pad_rows(self, inner: Any, n: int) -> None:
        geo = self._geo()
        if geo is None or n <= 0:
            return
        row, _height = geo
        fresh = self._pad_top is None or self._pad_top > row
        top = row if fresh else self._pad_top
        inner.write(f"\033[{top + 1};1H\033[{n}L\033[{row + n + 1};{self._cursor_col() + 1}H")
        self._pad_top = top
        # A stale or absent pad is replaced, never added to.
        self._pad_above = n if fresh else self._pad_above + n

    def _delete_pad_rows(self, inner: Any, n: int, col: int | None = None) -> int:
        """Remove up to `n` rows from the top of the pad; returns how many.
        `col` is the cursor's real column to return to (the margin's column
        may already reflect text that is about to be written)."""
        geo = self._geo()
        if geo is None or n <= 0 or self._pad_above <= 0 or self._pad_top is None:
            return 0
        row, _height = geo
        if self._pad_top > row:
            self.reset_pad()
            return 0
        m = min(n, self._pad_above)
        if col is None:
            col = self._cursor_col()
        inner.write(f"\033[{self._pad_top + 1};1H\033[{m}M\033[{row - m + 1};{col + 1}H")
        self._pad_above -= m
        if self._pad_above == 0:
            self._pad_top = None
        return m

    def _compose(self) -> list[str]:
        """The box rows clipped to width."""
        m = self.margin
        return [clip_visible(r, m.usable) for r in self.rows(m.usable)]

    def _draw(self, inner: Any, rows: list[str] | None = None) -> None:
        """Draw the box on the rows below the cursor, at the bottom of the
        window: pad rows are inserted or removed so it lands there."""
        m = self.margin
        if rows is None:
            rows = self._compose()
        if self._geometry_changed():
            self.reset_pad()
        below = self._rows_below_cursor()
        if below is not None:
            if below > len(rows):
                self._insert_pad_rows(inner, below - len(rows))
            elif below < len(rows):
                self._delete_pad_rows(inner, len(rows) - below)   # the rest scrolls
        saved = (m.col, m.word, m.word_vis)
        out = ["\033[?25l", "\n", "\n".join(rows), "\r", f"\033[{len(rows)}A"]
        if saved[0]:
            out.append(f"\033[{saved[0]}C")
        out.append("\033[?25h")
        inner.write("".join(out))
        m.col, m.word, m.word_vis = saved
        self._drawn = len(rows)
        self._signature = (tuple(rows), saved[0])

    def make_room(self, n: int) -> int:
        """The editor's entry is about to run past the bottom: delete up to
        `n` pad rows at the pad's top so the rows below (the conversation and
        the editor) move up and the window need not scroll. Returns how many
        rows were freed; the banner keeps its place for those."""
        with self.lock:
            if self._inner is None or n <= 0:
                return 0
            return self._delete_pad_rows(self._inner, n)

    def give_room(self, n: int) -> int:
        """The entry shrank again: put up to `n` rows back into the pad, so
        the conversation and the editor move down to where they were."""
        with self.lock:
            if self._inner is None or n <= 0 or self._pad_top is None:
                return 0
            self._insert_pad_rows(self._inner, n)
            return n

    def pad_for_editor(self) -> None:
        """Put the cursor on the row where the editor's first row must go for
        its rows to land on the window's last rows. Called with the box down
        and the cursor on a fresh row; the content above stays put."""
        geo = self._geo()
        if geo is None or self._inner is None:
            return
        row, height = geo
        target = height - self.editor_rows
        if row < target:
            self._insert_pad_rows(self._inner, target - row)

    def write(self, inner: Any, text: str) -> None:
        """A transcript write from one of the wrapped streams. The newlines
        it carries consume pad rows first, so the text appears above the box
        and the conversation grows upward."""
        with self.lock:
            if self.enabled and not self._relaying and self._geometry_changed():
                self._relayout(inner)
            if self._in_turn and not self._relaying:
                self._log_turn(text)
            if self.enabled:
                self._erase(inner)
            # The column to come back to after a pad delete is where the
            # cursor is NOW; rendering advances the margin's column to where
            # it will be after the write, so read it first.
            col_before = self._cursor_col()
            rendered = self.margin.render(text)
            newlines = rendered.count("\n")
            if newlines and self._pad_above and self._geometry_changed():
                self.reset_pad()
            if newlines and self._pad_above:
                geo = self._geo()
                if geo is not None:
                    row, height = geo
                    need = newlines + (self._drawn_rows_needed() if self.enabled else 0)
                    deficit = need - (height - 1 - row)
                    if deficit > 0:
                        self._delete_pad_rows(inner, deficit, col=col_before)
            inner._base.write(rendered)
            if self.enabled:
                self._draw(inner)

    def _drawn_rows_needed(self) -> int:
        return len(self.rows(self.margin.usable))

    _TURN_LOG_MAX = 400_000   # characters kept for a replay; the oldest go first

    def _log_turn(self, text: str) -> None:
        self._turn_log.append(text)
        self._turn_log_len += len(text)
        while self._turn_log_len > self._TURN_LOG_MAX and len(self._turn_log) > 1:
            self._turn_log_len -= len(self._turn_log.pop(0))

    def _relayout(self, inner: Any) -> None:
        """The window changed shape while the box is up. The terminal
        re-wrapped the rows; the ones that no longer fit went into its
        scrollback (Windows Terminal), out of reach. Clear, let the owner
        reprint the banner and the conversation, then replay this turn's
        output so far, all through the pad-consuming path, so the layout
        is what it would have been at this size from the start."""
        if self.on_relayout is None:
            self.reset_pad()
            return
        self._relaying = True
        try:
            inner._base.write("\033[2J\033[3J\033[H\r")
            m = self.margin
            m.col, m.word, m.word_vis = 0, "", 0
            self._drawn = 0
            self._signature = None
            self.reset_pad()
            # The box stays down while the banner prints: a write with the
            # box up pins it under the cursor, and the pad would land
            # between the banner's rows instead of under them.
            self.enabled = False

            def pin() -> None:
                self.enabled = True
                self._draw(inner)

            try:
                self.on_relayout(pin)
            finally:
                self.enabled = True
            replay = "".join(self._turn_log)
            if replay:
                self.write(inner, replay)
        finally:
            self._relaying = False

    def repaint(self) -> None:
        """Redraw the box in place (a spinner tick, a metrics sample). Skips
        the erase and rewrite when nothing visible changed, so a quiet
        second or an identical sample costs nothing."""
        with self.lock:
            if not (self.enabled and self._inner is not None):
                return
            rows = self._compose()
            if not self._relaying and self._geometry_changed():
                self._relayout(self._inner)   # and never skip: the box must move with the window
                rows = self._compose()
            elif self._drawn and (tuple(rows), self.margin.col) == self._signature:
                return
            self._erase(self._inner)
            self._draw(self._inner, rows)

    # -- state ----------------------------------------------------------------

    def enable(self) -> None:
        """Show the box below the transcript until `disable`. A turn's clock
        starts with its first activity label and runs until disable()."""
        with self.lock:
            self.enabled = True
            self.activity, self.frame = None, ""
            self._activity_since = None
            self._turn_log, self._turn_log_len, self._in_turn = [], 0, True
            if self._inner is not None:
                self._erase(self._inner)
                self._draw(self._inner)

    def disable(self) -> None:
        """Take the box down and leave the cursor on a fresh transcript row,
        which is where the line editor expects to start."""
        with self.lock:
            if self._inner is not None:
                self._erase(self._inner)
                if self.margin.col:
                    self._inner.write("\n")
            self.enabled = False
            self.activity, self.frame = None, ""
            self._turn_log, self._turn_log_len, self._in_turn = [], 0, False
            self.pad_for_editor()
        self.refresh_location()

    def suspend(self) -> None:
        """Take the box down for an inline prompt (a y/N confirm) without
        padding to the bottom, so the question prints right where the turn
        is, not pushed to the last row. resume() puts the box back."""
        with self.lock:
            if self._inner is not None:
                self._erase(self._inner)
                if self.margin.col:
                    self._inner.write("\n")
            self.enabled = False

    def resume(self) -> None:
        """Put the box back after suspend(), keeping the current activity."""
        with self.lock:
            self.enabled = True
            if self._inner is not None:
                self._erase(self._inner)
                self._draw(self._inner)

    def set_activity(self, label: str | None) -> None:
        with self.lock:
            if label is None:
                self.frame = ""
            elif self._activity_since is None:
                self._activity_since = time.monotonic()   # per turn: enable() resets it
            self.activity = label
        self.repaint()

    def tick(self, frame: str) -> None:
        with self.lock:
            self.frame = frame
        self.repaint()


class _LiveStream:
    """stdout/stderr wrapper: transcript writes go through the live area."""

    def __init__(self, inner: Any, live: LiveArea) -> None:
        self._inner = inner
        self._live = live

    def write(self, s: str) -> int:
        if s:
            self._live.write(self._inner, s)
        return len(s)

    def writelines(self, lines: Any) -> None:
        for line in lines:
            self.write(line)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)


_LIVE: LiveArea | None = None


def current() -> LiveArea | None:
    return _LIVE


def install(config: dict[str, Any], context_percent: Callable[[], int | None]) -> LiveArea | None:
    """Wrap the console streams and start the sampler. None when the REPL
    is not on an interactive console (piped stdin, CI, --raw) or the user
    turned the box off with `status_bar: false`; the plain prompt is used
    then, exactly as before."""
    global _LIVE
    if _LIVE is not None:
        return _LIVE
    if not bool(config.get("status_bar", True)) or not bool(config.get("rich_input", True)):
        return None
    try:
        if not (sys.stdin.isatty() and sys.stdout.isatty()):
            return None
        import msvcrt  # noqa: F401
    except (ImportError, Exception):
        return None
    if not isinstance(sys.stdout, ui._MarginStream):
        # side_padding 0: the live area still needs the column bookkeeping.
        margin = ui._Margin(0)
        sys.stdout = ui._MarginStream(sys.stdout, margin)
        sys.stderr = ui._MarginStream(sys.stderr, margin)
    margin = sys.stdout._margin
    live = LiveArea(margin, context_percent)
    live._inner = sys.stdout
    sys.stdout = _LiveStream(sys.stdout, live)
    sys.stderr = _LiveStream(sys.stderr, live)
    # The sampler refreshes the status line once a second even when nothing
    # else writes (a long tool subprocess with no spinner); the repaint skips
    # itself when the numbers did not move.
    live.sampler.on_update = live.repaint
    live.sampler.start()
    ui.LIVE_AREA = live
    _LIVE = live
    return live




def uninstall() -> None:
    """Erase the box if it is up, stop the sampler and unwrap the streams."""
    global _LIVE
    live = _LIVE
    if live is None:
        return
    with live.lock:
        if live._inner is not None:
            live._erase(live._inner)
            if live.margin.col:
                live._inner.write("\n")
        live.enabled = False
    live.sampler.stop()
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name)
        if isinstance(stream, _LiveStream):
            setattr(sys, name, stream._inner)
    ui.LIVE_AREA = None
    _LIVE = None
