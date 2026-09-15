#!/usr/bin/env python3
"""hexcli.psparse — structural facts about a PowerShell command line.

The safety classifier matched patterns against raw text, which fails in both
directions (measured 2026-09-15): `ri C:\\data` and `clc notes.txt` ran with no
confirmation because the alias never appears in a pattern, while
`Write-Output 'this will erase nothing'` was called destructive because the
word sat inside a string. Both need to know which text is a command name and
which is an inert argument, and that needs a parser for the language.

PowerShell has one built in: `[Parser]::ParseInput`. This module runs a small
helper script inside ONE long-lived `pwsh -NoProfile` process, sends it a
command per line and reads back a JSON summary. The process starts lazily on
first use and is reused for the session; results are cached by command string,
so a repeated command costs nothing. Everything degrades to "unavailable",
and the caller then keeps its pattern verdict unchanged.

The agent runs commands with `-NoProfile`, so the only aliases that can exist
are PowerShell's built-ins; the helper resolves them from the live session's
own alias table rather than a table copied into Python.
"""
from __future__ import annotations

import base64
import json
import os
import queue
import shutil
import subprocess
import threading
from dataclasses import dataclass, field

# One command in, one JSON line out. Base64 keeps newlines and quoting out of
# the protocol. `Ping` is answered before anything is parsed, so the caller can
# tell "the helper is up" from "the helper is wedged".
_HELPER = r"""
$ErrorActionPreference = 'Stop'
$aliases = @{}
foreach ($a in Get-Alias) {
    # Definition, not ResolvedCommandName: the latter is null for most aliases
    # unless the target module is already loaded (measured 2026-09-15: 78 of
    # 135 came back empty and the table was useless).
    $d = $a.Definition
    if ($d) { $aliases[$a.Name.ToLowerInvariant()] = $d.ToLowerInvariant() }
}
Write-Output 'READY'
while ($null -ne ($line = [Console]::In.ReadLine())) {
    try {
        $cmd = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String($line))
        $errors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseInput($cmd, [ref]$null, [ref]$errors)
        $names = New-Object System.Collections.ArrayList
        $nonliteral = $false
        foreach ($c in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.CommandAst] }, $true)) {
            $n = $c.GetCommandName()
            if ($null -eq $n) { $nonliteral = $true; continue }
            $n = $n.ToLowerInvariant()
            if ($aliases.ContainsKey($n)) { $n = $aliases[$n] }
            [void]$names.Add($n)
        }
        $strings = New-Object System.Collections.ArrayList
        foreach ($s in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.StringConstantExpressionAst] }, $true)) {
            $p = $s.Parent
            $isName = ($p -is [System.Management.Automation.Language.CommandAst]) -and ($p.CommandElements.Count -gt 0) -and ($p.CommandElements[0] -eq $s)
            if (-not $isName) {
                [void]$strings.Add(@($s.Extent.StartOffset, $s.Extent.EndOffset))
            }
        }
        $out = @{ ok = $true; names = @($names); nonliteral = $nonliteral; strings = @($strings) }
    } catch {
        $out = @{ ok = $false; names = @(); nonliteral = $false; strings = @() }
    }
    Write-Output ($out | ConvertTo-Json -Compress -Depth 4)
}
"""

_START_TIMEOUT_S = 10.0
_PARSE_TIMEOUT_S = 5.0
_CACHE_MAX = 512


@dataclass
class Facts:
    """What the parser saw. `ok` False means nothing was learned and the
    caller must keep whatever verdict it already had."""
    ok: bool = False
    names: tuple[str, ...] = ()          # canonical command names, aliases resolved
    nonliteral: bool = False             # a command whose name is not a literal
    strings: tuple[tuple[int, int], ...] = field(default=())   # inert string-literal extents


UNAVAILABLE = Facts()

_lock = threading.Lock()
_proc: subprocess.Popen[str] | None = None
_replies: queue.Queue[str] | None = None
_disabled = False
_cache: dict[str, Facts] = {}


def _reader(proc: subprocess.Popen[str], q: queue.Queue[str]) -> None:
    assert proc.stdout is not None
    for line in iter(proc.stdout.readline, ""):
        q.put(line.strip())
    q.put("")


def _shell_exe() -> str | None:
    for candidate in ("pwsh.exe", "powershell.exe", "pwsh"):
        found = shutil.which(candidate)
        if found:
            return found
    return None


def _start() -> bool:
    """Bring the helper up. False (and disabled for good) when it cannot be."""
    global _proc, _replies, _disabled
    if _disabled:
        return False
    if _proc is not None and _proc.poll() is None:
        return True
    exe = _shell_exe()
    if exe is None:
        _disabled = True
        return False
    try:
        env = dict(os.environ, POWERSHELL_TELEMETRY_OPTOUT="1")
        proc = subprocess.Popen(
            [exe, "-NoLogo", "-NoProfile", "-NonInteractive", "-Command", "-"],
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, encoding="utf-8", errors="replace", env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        q: queue.Queue[str] = queue.Queue()
        threading.Thread(target=_reader, args=(proc, q), daemon=True).start()
        assert proc.stdin is not None
        proc.stdin.write(_HELPER + "\n")
        proc.stdin.flush()
        if q.get(timeout=_START_TIMEOUT_S) != "READY":
            raise RuntimeError("helper did not announce itself")
    except Exception:  # noqa: BLE001 — no parser is a supported state, never an error
        _disabled = True
        _proc = None
        return False
    _proc, _replies = proc, q
    return True


def _ask(cmd: str) -> Facts:
    global _proc, _disabled
    if not _start():
        return UNAVAILABLE
    assert _proc is not None and _proc.stdin is not None and _replies is not None
    try:
        payload = base64.b64encode(cmd.encode("utf-8")).decode("ascii")
        _proc.stdin.write(payload + "\n")
        _proc.stdin.flush()
        line = _replies.get(timeout=_PARSE_TIMEOUT_S)
        if not line:
            raise RuntimeError("helper closed")
        data = json.loads(line)
    except Exception:  # noqa: BLE001 — a wedged helper must not wedge the agent
        try:
            if _proc is not None:
                _proc.kill()
        except Exception:  # noqa: BLE001
            pass
        _proc = None
        _disabled = True      # one failure is enough; the patterns still hold
        return UNAVAILABLE
    if not data.get("ok"):
        return UNAVAILABLE
    strings = tuple((int(a), int(b)) for a, b in (data.get("strings") or []))
    return Facts(ok=True,
                 names=tuple(str(n) for n in (data.get("names") or [])),
                 nonliteral=bool(data.get("nonliteral")),
                 strings=strings)


def facts(cmd: str) -> Facts:
    """Structural facts about `cmd`, or UNAVAILABLE. Cached by command string:
    the parse costs a round trip once per distinct command, and an agent that
    retries the same call pays it once."""
    key = cmd.strip()
    if not key:
        return UNAVAILABLE
    with _lock:
        hit = _cache.get(key)
        if hit is not None:
            return hit
        got = _ask(key)
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = got
        return got


def shutdown() -> None:
    """Stop the helper (tests, and the REPL on exit)."""
    global _proc
    with _lock:
        proc, _proc = _proc, None
        _cache.clear()
    if proc is not None:
        try:
            if proc.stdin is not None:
                proc.stdin.close()
            proc.wait(timeout=2)
        except Exception:  # noqa: BLE001
            try:
                proc.kill()
            except Exception:  # noqa: BLE001
                pass


def _reset_for_tests() -> None:
    global _disabled
    shutdown()
    _disabled = False
