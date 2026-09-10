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
    return len(_ANSI_RE.sub("", text))


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
        if seen >= width:
            break
        out.append(text[i])
        seen += 1
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

    def __init__(self, interval: float = _SAMPLE_INTERVAL_S) -> None:
        self.interval = interval
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
        gap = width - left_vis - len(right)
        if gap >= 2:
            return left + " " * gap + f"{C.DIM}{right}{C.RESET}"
    return clip_visible(left, width)


def rule(width: int) -> str:
    return f"{C.DIM}{'─' * max(0, width)}{C.RESET}"


# ── the live area ────────────────────────────────────────────────────────────


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
    ) -> None:
        self.margin = margin
        self._context_percent = context_percent
        self.sampler = sampler or SystemSampler()
        self.prompt = prompt
        self.lock = threading.RLock()
        self.enabled = False
        self.activity: str | None = None
        self.frame = ""
        self.location = ""
        self._drawn = 0            # rows currently on screen below the transcript
        self._inner: Any = None    # the stream repaints go through (set by install)

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
        return status_line(
            width, context_percent=pct, npu_percent=npu, mem_used_gb=used,
            mem_total_gb=total, activity=self.activity, frame=self.frame,
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
        if self._drawn:
            inner.write("\033[J")
            self._drawn = 0

    def _draw(self, inner: Any) -> None:
        m = self.margin
        saved = (m.col, m.word, m.word_vis)
        rows = [clip_visible(r, m.usable) for r in self.rows(m.usable)]
        out = ["\033[?25l", "\n", "\n".join(rows), "\r", f"\033[{len(rows)}A"]
        if saved[0]:
            out.append(f"\033[{saved[0]}C")
        out.append("\033[?25h")
        inner.write("".join(out))
        m.col, m.word, m.word_vis = saved
        self._drawn = len(rows)

    def write(self, inner: Any, text: str) -> None:
        """A transcript write from one of the wrapped streams."""
        with self.lock:
            if not self.enabled:
                inner.write(text)
                return
            self._erase(inner)
            inner.write(text)
            self._draw(inner)

    def repaint(self) -> None:
        with self.lock:
            if self.enabled and self._inner is not None:
                self._erase(self._inner)
                self._draw(self._inner)

    # -- state ----------------------------------------------------------------

    def enable(self) -> None:
        """Show the box below the transcript until `disable`."""
        with self.lock:
            self.enabled = True
            self.activity, self.frame = None, ""
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
        self.refresh_location()

    def set_activity(self, label: str | None) -> None:
        with self.lock:
            self.activity = label
            if label is None:
                self.frame = ""
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
