#!/usr/bin/env python3
"""hexcli.lockfile — Advisory PID-file process lock for the shellai entry point.

Prevents two shellai instances from sharing the same npurun backend at the
same time (the backend is a singleton: one KV-cache context, one CDSP
session). All operations are non-fatal: if the filesystem is read-only or
ctypes is unavailable, the lock is silently skipped and the agent still
starts.
"""
from __future__ import annotations

import atexit
import ctypes
import os
from pathlib import Path

_LOCK_PATH: Path | None = None
_SYNCHRONIZE = 0x00100000  # Windows PROCESS_SYNCHRONIZE access right


def _pid_alive(pid: int) -> bool:
    """Return True if the process with this PID is currently running (Windows)."""
    try:
        h = ctypes.windll.kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
        if h:
            ctypes.windll.kernel32.CloseHandle(h)
            return True
        return False
    except Exception:
        return False


def acquire(lock_dir: Path) -> str | None:
    """Write a PID lock file in lock_dir.

    Returns a warning string if another live shellai process is already
    running; returns None if the lock was acquired cleanly (or if the
    check could not be performed).
    """
    global _LOCK_PATH
    lock_path = lock_dir / "shellai.lock"
    _LOCK_PATH = lock_path
    warning: str | None = None

    if lock_path.exists():
        try:
            existing_pid = int(lock_path.read_text(encoding="utf-8").strip())
            if existing_pid != os.getpid() and _pid_alive(existing_pid):
                warning = (
                    f"⚠ Another shellai instance (PID {existing_pid}) appears to be running. "
                    "Two instances sharing one npurun backend may interfere with each other."
                )
        except Exception:
            pass  # stale or unreadable lock — overwrite silently

    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(os.getpid()), encoding="utf-8")
        atexit.register(_release)
    except Exception:
        pass  # read-only filesystem or permission error — advisory only

    return warning


def _release() -> None:
    """Remove the lock file if it still contains our PID."""
    if _LOCK_PATH is None or not _LOCK_PATH.exists():
        return
    try:
        if int(_LOCK_PATH.read_text(encoding="utf-8").strip()) == os.getpid():
            _LOCK_PATH.unlink()
    except Exception:
        pass
