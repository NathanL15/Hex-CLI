#!/usr/bin/env python3
"""hexcli.shell_session — persistent PowerShell session for the v2 `shell` tool.

v1 spawned a fresh `powershell.exe -Command` per call, so `cd`, environment
variables, and venv activation evaporated between steps. This module keeps ONE
PowerShell process alive per agent session and multiplexes commands through it
with a sentinel protocol:

    <command>
    Write-Output "<sentinel> <exit-ish code>"

stdout+stderr are merged at the command level (2>&1) so the model sees errors
inline, in order. A timeout kills the whole process tree (taskkill /T) and the
next call transparently respawns a fresh session — a hung command can never
wedge the agent loop.
"""
from __future__ import annotations

import base64
import queue
import subprocess
import threading
import uuid
from typing import Any

_DEFAULT_TIMEOUT_S = 60
_OUTPUT_CAP_CHARS = 200_000  # hard runaway guard; the agent loop trims further


class ShellSession:
    """One persistent PowerShell process; safe to reuse across commands."""

    def __init__(self, cwd: str | None = None, shell_exe: str = "powershell.exe") -> None:
        self._cwd = cwd
        self._shell_exe = shell_exe
        self._proc: subprocess.Popen[str] | None = None
        self._out_queue: queue.Queue[str | None] = queue.Queue()
        self._reader: threading.Thread | None = None
        self._lock = threading.Lock()

    # -- lifecycle ----------------------------------------------------------

    def _spawn(self) -> None:
        self._proc = subprocess.Popen(
            [self._shell_exe, "-NoProfile", "-NoLogo", "-NonInteractive", "-Command", "-"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=self._cwd,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
        )
        self._out_queue = queue.Queue()
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    def _read_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        q = self._out_queue
        for line in proc.stdout:
            q.put(line)
        q.put(None)  # EOF marker

    def _send(self, text: str) -> None:
        assert self._proc is not None and self._proc.stdin is not None
        self._proc.stdin.write(text + "\n")
        self._proc.stdin.flush()

    def _alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def close(self) -> None:
        with self._lock:
            self._kill_tree()

    def _kill_tree(self) -> None:
        if self._proc is None:
            return
        pid = self._proc.pid
        try:
            subprocess.run(
                ["taskkill", "/T", "/F", "/PID", str(pid)],
                capture_output=True, timeout=10,
            )
        except Exception:
            try:
                self._proc.kill()
            except Exception:
                pass
        self._proc = None

    # -- command execution --------------------------------------------------

    def run(self, command: str, timeout_s: int = _DEFAULT_TIMEOUT_S) -> dict[str, Any]:
        """Run one command; returns {"output": str, "exit_code": int|None,
        "timed_out": bool, "restarted": bool}."""
        with self._lock:
            restarted = False
            if not self._alive():
                self._kill_tree()
                self._spawn()
                restarted = self._proc is not None

            sentinel = f"__HEX_DONE_{uuid.uuid4().hex}__"
            # Windows PowerShell 5.1 decodes piped stdin — and encodes piped
            # stdout — with the OEM codepage, mangling any non-ASCII content.
            # So the command travels IN as UTF-16LE base64 (Invoke-Expression
            # keeps it in the session's own scope: cd, $env:, and variables
            # all persist), and the output travels OUT as UTF-8 base64 on the
            # sentinel line. Base64 is pure ASCII and survives any codepage.
            #
            # Exit-code detection: native commands set $LASTEXITCODE; cmdlet
            # failures are detected via $Error growth ($? is useless here —
            # it would reflect the last pipeline stage, not the command).
            cmd_b64 = base64.b64encode(command.encode("utf-16-le")).decode("ascii")
            wrapped = (
                "$global:LASTEXITCODE = $null; $__hex_errs = $Error.Count; "
                f"$__hex_cmd = [System.Text.Encoding]::Unicode.GetString([System.Convert]::FromBase64String('{cmd_b64}')); "
                # Dot-sourcing a created ScriptBlock runs in the CURRENT scope
                # (cd/$env:/variables persist) while letting 2>&1 capture the
                # invoked command's error stream — Invoke-Expression's redirect
                # cannot see errors raised inside the expression.
                "try { $__hex_out = . ([System.Management.Automation.ScriptBlock]::Create($__hex_cmd)) 2>&1 | Out-String } "
                "catch { $__hex_out = ($_ | Out-String) ; $__hex_errs = -1 }; "
                "$__hex_code = if ($null -ne $global:LASTEXITCODE) { $global:LASTEXITCODE } "
                "elseif ($__hex_errs -lt 0 -or $Error.Count -gt $__hex_errs) { 1 } else { 0 }; "
                f"if ($__hex_out.Length -gt {_OUTPUT_CAP_CHARS}) "
                f"{{ $__hex_out = $__hex_out.Substring(0, {_OUTPUT_CAP_CHARS}) + \"`n[output truncated]\" }}; "
                "$__hex_b64 = [System.Convert]::ToBase64String([System.Text.Encoding]::UTF8.GetBytes($__hex_out)); "
                f"Write-Output \"{sentinel}:$__hex_code`:$__hex_b64\""
            )
            try:
                self._send(wrapped)
            except (OSError, AssertionError):
                self._kill_tree()
                return {"output": "shell session died while sending the command; it will restart on the next call",
                        "exit_code": None, "timed_out": False, "restarted": restarted}

            lines: list[str] = []
            total = 0
            deadline = timeout_s
            import time
            start = time.monotonic()
            while True:
                remaining = deadline - (time.monotonic() - start)
                if remaining <= 0:
                    self._kill_tree()
                    return {
                        "output": "".join(lines)[:_OUTPUT_CAP_CHARS]
                        + f"\n[timeout] command exceeded {timeout_s}s; the shell session was killed and will restart on the next call",
                        "exit_code": None, "timed_out": True, "restarted": restarted,
                    }
                try:
                    line = self._out_queue.get(timeout=min(remaining, 0.5))
                except queue.Empty:
                    continue
                if line is None:
                    # Process exited underneath us (e.g. the command ran `exit`).
                    self._kill_tree()
                    return {"output": "".join(lines)[:_OUTPUT_CAP_CHARS],
                            "exit_code": None, "timed_out": False, "restarted": restarted}
                if line.startswith(sentinel):
                    parts = line.strip().split(":", 2)
                    exit_code: int | None
                    output: str
                    try:
                        exit_code = int(parts[1])
                    except (IndexError, ValueError):
                        exit_code = None
                    try:
                        output = base64.b64decode(parts[2]).decode("utf-8", errors="replace") if len(parts) > 2 else ""
                    except Exception:
                        output = ""
                    # Anything that leaked outside the sentinel protocol (e.g.
                    # wrapper-level parse errors) is prepended so it's never lost.
                    if lines:
                        output = "".join(lines) + output
                    return {"output": output[:_OUTPUT_CAP_CHARS],
                            "exit_code": exit_code, "timed_out": False, "restarted": restarted}
                total += len(line)
                if total <= _OUTPUT_CAP_CHARS:
                    lines.append(line)
