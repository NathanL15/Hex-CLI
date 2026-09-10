#!/usr/bin/env python3
"""evals/test_statusbar.py — the input box and status line.

The live area is driven through a fake inner stream and a real ui._Margin,
so every escape sequence it emits is asserted without a terminal. The
metric readers are Windows-only ctypes; only their pure parts (LUID choice,
formatting) are tested here, the counter itself was verified live.

Usage:
    python evals/test_statusbar.py
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli import statusbar as sb  # noqa: E402
from hexcli import ui  # noqa: E402


class _Stream:
    def __init__(self) -> None:
        self.chunks: list[str] = []

    def write(self, s: str) -> int:
        self.chunks.append(s)
        return len(s)

    def flush(self) -> None:
        pass

    def isatty(self) -> bool:
        return True

    @property
    def text(self) -> str:
        return "".join(self.chunks)


class _Sampler:
    def __init__(self, snap: tuple[Any, Any, Any] = (12.0, 9.3, 15.6)) -> None:
        self.snap = snap
        self.started = False

    def snapshot(self) -> tuple[Any, Any, Any]:
        return self.snap

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        pass


def _live(width: int = 40, pad: int = 2, pct: int = 32) -> tuple[sb.LiveArea, _Stream, Any]:
    """A live area over a margin stream over a capturing base stream."""
    base = _Stream()
    margin = ui._Margin(pad, width=lambda: width)
    inner = ui._MarginStream(base, margin)
    live = sb.LiveArea(margin, lambda: pct, sampler=_Sampler())
    live._inner = inner
    return live, base, inner


def _no_color() -> Any:
    ui.set_color_enabled(False)
    return lambda: ui.set_color_enabled(True)


# ── metrics ──────────────────────────────────────────────────────────────────

def test_npu_luid_is_the_compute_only_adapter_directx_does_not_list() -> None:
    engines = {
        "0x00000000_0x00011493": {"3D", "Compute", "VideoDecode"},   # the GPU
        "0x00000000_0x0001285F": {"3D"},                             # Basic Render Driver
        "0x00000000_0x00012885": {"Compute"},                        # the NPU
    }
    known = {"0X00000000_0X00011493", "0X00000000_0X0001285F", "0X00000000_0X00000000"}
    assert sb.pick_npu_luid(engines, known) == "0x00000000_0x00012885"
    # Two unknowns: the Compute-only one wins; none: nothing.
    engines["0x00000000_0x0FFFFFFF"] = {"3D"}
    assert sb.pick_npu_luid(engines, known) == "0x00000000_0x00012885"
    assert sb.pick_npu_luid({"0x00000000_0x00011493": {"3D"}}, known) is None
    assert sb.pick_npu_luid({}, set()) is None


def test_memory_status_reads_used_and_total_on_windows() -> None:
    import os
    mem = sb.memory_status()
    if os.name != "nt":
        assert mem is None
        return
    assert mem is not None
    used, total = mem
    assert 0 < used <= total and total > 1, mem


# ── the status line ──────────────────────────────────────────────────────────

def test_status_line_lists_the_three_metrics_and_right_aligns_the_location() -> None:
    restore = _no_color()
    try:
        line = sb.status_line(60, context_percent=32, npu_percent=96.4, mem_used_gb=9.34,
                              mem_total_gb=15.6, right="~\\proj (main)")
        assert line.startswith("context " + ui.context_gauge(32) + "   npu 96%   mem 9.3/15.6 GB"), line
        assert line.endswith("~\\proj (main)") and len(line) == 60, (len(line), line)
        # Activity goes first, with the spinner frame.
        line = sb.status_line(80, context_percent=0, npu_percent=0, mem_used_gb=8,
                              activity="thinking (Esc to cancel)", frame="⠹")
        assert line.startswith("⠹ thinking (Esc to cancel)   context "), line
        # Unavailable metrics are simply absent.
        line = sb.status_line(80, context_percent=None, npu_percent=None, mem_used_gb=None)
        assert line == "", repr(line)
        # No total known: just the figure.
        assert sb.status_line(80, context_percent=None, npu_percent=None, mem_used_gb=9.34) == "mem 9.3 GB"
    finally:
        restore()


def test_status_line_never_exceeds_the_width() -> None:
    restore = _no_color()
    try:
        # Location dropped when it does not fit, then the metrics clipped.
        line = sb.status_line(40, context_percent=32, npu_percent=5, mem_used_gb=9.3,
                              right="~\\a\\very\\long\\path\\indeed (main)")
        assert "path" not in line and len(line) <= 40, line
        line = sb.status_line(12, context_percent=32, npu_percent=5, mem_used_gb=9.3)
        assert len(line) == 12, repr(line)
    finally:
        restore()
    # With colour on, clipping keeps the escapes and closes the styling.
    styled = sb.clip_visible("\033[2mabcdef\033[0m", 3)
    assert sb.visible_len(styled) == 3 and styled.startswith("\033[2mabc") and styled.endswith("\033[0m"), repr(styled)


def test_tones_follow_the_thresholds() -> None:
    hot = sb.status_line(80, context_percent=80, npu_percent=60, mem_used_gb=14.5, mem_total_gb=15.6)
    assert ui.C.BYELLOW + "context" in hot and ui.C.BCYAN + "npu" in hot and ui.C.BYELLOW + "mem" in hot, hot
    cool = sb.status_line(80, context_percent=10, npu_percent=3, mem_used_gb=8, mem_total_gb=15.6)
    assert ui.C.DIM + "context" in cool and ui.C.DIM + "npu" in cool and ui.C.DIM + "mem" in cool, cool
    full = sb.status_line(80, context_percent=100, npu_percent=None, mem_used_gb=None)
    assert ui.C.BRED + "context" in full


# ── the live area ────────────────────────────────────────────────────────────

def test_chrome_is_a_rule_above_and_a_rule_plus_status_below() -> None:
    restore = _no_color()
    try:
        live, _, _ = _live(width=60)
        above, below = live.chrome(56)
        # One cell short of the width, or the editor pads a spare row.
        assert above == ["─" * 55] and below[0] == "─" * 55, (above, below)
        assert below[1].startswith("context ") and "npu 12%" in below[1] and "mem 9.3/15.6 GB" in below[1], below[1]
        rows = live.rows(56)
        assert rows == above + [">"] + below, rows
    finally:
        restore()


def test_enable_draws_the_box_below_the_transcript_and_returns_the_cursor() -> None:
    restore = _no_color()
    try:
        live, base, _ = _live(width=40, pad=2)
        live.enable()
        text = base.text
        rows = live.rows(36)
        # Hidden cursor, a fresh (padded) row per box row, then back up
        # four rows to the transcript row's column 0 (plus the margin fill).
        assert text.startswith("\033[?25l\n  " + rows[0]), repr(text[:60])
        assert text.endswith("\r  \033[4A\033[?25h"), repr(text[-30:])
        assert all(r in text for r in rows)
        assert live._drawn == 4 and live.margin.col == 0
    finally:
        restore()


def test_a_transcript_write_erases_writes_and_redraws_keeping_the_column() -> None:
    restore = _no_color()
    try:
        live, base, inner = _live(width=40, pad=2)
        live.enable()
        base.chunks.clear()
        live.write(inner, "hello")
        text = base.text
        assert text.startswith("\033[J"), repr(text[:10])
        assert "\033[Jhello\033[?25l\n" in text, repr(text[:40])
        # The margin column is restored so later wrapping is right, and the
        # cursor is moved back to it after the box is drawn.
        assert live.margin.col == 5 and text.endswith("\r  \033[4A\033[5C\033[?25h"), (live.margin.col, repr(text[-30:]))
        # A newline-terminated write leaves the cursor at column 0: no move.
        base.chunks.clear()
        live.write(inner, " world\n")
        assert live.margin.col == 0 and base.text.endswith("\r  \033[4A\033[?25h"), repr(base.text[-30:])
    finally:
        restore()


def test_disabled_area_passes_writes_straight_through() -> None:
    live, base, inner = _live()
    live.write(inner, "plain\n")
    assert base.text == "plain\n  " and live._drawn == 0, repr(base.text)


def test_disable_erases_the_box_and_starts_a_fresh_row() -> None:
    restore = _no_color()
    try:
        live, base, inner = _live()
        live.enable()
        live.write(inner, "partial")
        base.chunks.clear()
        live.disable()
        # Erase from the transcript cursor, then a newline because the row
        # had text on it: the line editor starts on an empty row.
        assert base.text == "\033[J\n  ", repr(base.text)
        assert not live.enabled and live._drawn == 0 and live.margin.col == 0
        base.chunks.clear()
        live.enable()
        live.write(inner, "done\n")
        base.chunks.clear()
        live.disable()
        assert base.text == "\033[J", repr(base.text)
    finally:
        restore()


def test_activity_and_ticks_repaint_the_status_row() -> None:
    restore = _no_color()
    try:
        live, base, inner = _live(width=80)
        live.enable()
        base.chunks.clear()
        live.set_activity("thinking (Esc to cancel)")
        assert "thinking (Esc to cancel)   context" in base.text, base.text
        base.chunks.clear()
        live.tick("⠹")
        assert "⠹ thinking" in base.text and base.text.startswith("\033[J"), base.text
        base.chunks.clear()
        live.set_activity(None)
        assert "thinking" not in base.text and "context" in base.text, base.text
        # Nothing is painted while disabled.
        live.disable()
        base.chunks.clear()
        live.tick("⠸")
        live.set_activity("x")
        assert base.text == "", repr(base.text)
    finally:
        restore()


def test_spinner_uses_the_live_area_when_one_is_up() -> None:
    restore = _no_color()
    live, base, inner = _live(width=80)
    live.enable()
    old = ui.LIVE_AREA
    ui.LIVE_AREA = live
    try:
        base.chunks.clear()
        with ui.Spinner("thinking (Esc to cancel)"):
            assert live.activity == "thinking (Esc to cancel)"
        assert live.activity is None and live.frame == ""
        assert "\r\033[K" not in base.text, "the transcript-row clear belongs to the old path"
    finally:
        ui.LIVE_AREA = old
        restore()


def test_install_declines_off_a_console_and_when_turned_off() -> None:
    assert sb.install({"status_bar": False}, lambda: 0) is None
    assert sb.install({"rich_input": False}, lambda: 0) is None
    # Under the test runner stdout is captured or piped in CI; either way
    # the decision must be a clean None, never a wrapped stream.
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        assert sb.install({}, lambda: 0) is None
    assert sb.current() is None


def test_status_bar_is_a_settable_config_key() -> None:
    from hexcli import config
    assert config.DEFAULT_CONFIG["status_bar"] is True
    assert config._CONFIG_SETTABLE["status_bar"] == "bool"


TESTS = [
    test_npu_luid_is_the_compute_only_adapter_directx_does_not_list,
    test_memory_status_reads_used_and_total_on_windows,
    test_status_line_lists_the_three_metrics_and_right_aligns_the_location,
    test_status_line_never_exceeds_the_width,
    test_tones_follow_the_thresholds,
    test_chrome_is_a_rule_above_and_a_rule_plus_status_below,
    test_enable_draws_the_box_below_the_transcript_and_returns_the_cursor,
    test_a_transcript_write_erases_writes_and_redraws_keeping_the_column,
    test_disabled_area_passes_writes_straight_through,
    test_disable_erases_the_box_and_starts_a_fresh_row,
    test_activity_and_ticks_repaint_the_status_row,
    test_spinner_uses_the_live_area_when_one_is_up,
    test_install_declines_off_a_console_and_when_turned_off,
    test_status_bar_is_a_settable_config_key,
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
    print(f"\nevals/test_statusbar.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
