#!/usr/bin/env python3
"""hexcli.cancel — Esc-to-cancel primitives, lifted out of agent.py.

UserCancelled, the msvcrt-polling CancelMonitor, and run_cancellable. The
eval runner replaces CancelMonitor and Spinner with no-ops so unattended
runs never poll the keyboard — code in THIS module resolves both names
module-locally, so the runner patches hexcli.cancel as well as hexcli.agent
(evals/runner.py, _SilencedUI). Spinner is re-bound here from ui for exactly
that patchability.

Split stage 3a (docs/V2X_ROADMAP.md, "The Split"). Bodies moved verbatim.
"""
from __future__ import annotations

import msvcrt
import threading
from typing import Any

from hexcli import ui

Spinner = ui.Spinner


class UserCancelled(Exception):
    pass


def clear_keyboard_buffer() -> None:
    while msvcrt.kbhit():
        msvcrt.getwch()


class CancelMonitor:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self) -> None:
        while not self._stop.wait(0.05):
            if msvcrt.kbhit():
                if msvcrt.getwch() == "\x1b":
                    self.cancelled.set()

    def __enter__(self) -> CancelMonitor:
        clear_keyboard_buffer()
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        clear_keyboard_buffer()


def run_cancellable(label: str, work: Any) -> Any:
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            result["value"] = work()
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    with CancelMonitor() as monitor, Spinner(f"{label} (Esc to cancel)"):
        thread.start()
        while thread.is_alive():
            if monitor.cancelled.is_set():
                raise UserCancelled()
            thread.join(0.05)

    if "value" in error:
        raise error["value"]
    return result.get("value")
