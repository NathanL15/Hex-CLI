#!/usr/bin/env python3
"""hexcli.safety — Command safety classifier and append-only audit log."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

# Patterns checked in order: first match wins.  Destructive > safe > caution.

_DESTRUCTIVE: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in [
    r"\bremove-item\b",
    r"(?<![a-z])rm\s",              # rm <args> but not 'strm' etc.
    r"(?<![a-z])del\s",
    r"(?<![a-z])rd\s",
    r"\berase\b",
    r"\bformat-\w+",                # Format-Volume, Format-Disk …
    r"git\s+reset\s+--hard\b",
    r"git\s+push\s+(-f\b|--force\b)",
    r"git\s+clean\s+-[a-z]*f",     # git clean -f / -df / -xf
    r"\breg\s+delete\b",
    r"\bclear-recyclebin\b",
    r"\b(stop|restart)-computer\b",
    r"\bdiskpart\b",
    # -Force and -Recurse together — almost always Remove-Item level danger
    r"-force\b[^|&\n]*-recurse\b|-recurse\b[^|&\n]*-force\b",
    # Invoke-Expression / iex: evaluates arbitrary strings as code
    r"\b(invoke-expression|iex)\b",
]]

_SAFE: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in [
    r"^\s*get-\w+",                          # Get-Process, Get-ChildItem …
    r"^\s*(ls|dir)\b",
    r"^\s*(cat|type)\s",
    r"^\s*git\s+(status|log|diff|show|branch|stash list|tag|remote -v|describe)\b",
    r"^\s*(python|python3|py)\s+(--version|-V)\b",
    r"^\s*pip\s+(list|show|freeze)\b",
    r"^\s*(where|where\.exe)\b",
    r"^\s*(echo|write-output|write-host|pwd|test-path)\b",
    r"^\s*(node|npm)\s+(--version|-v)\b",
    r"^\s*select-string\b",                  # grep equivalent — read-only
]]


def classify_command(cmd: str) -> str:
    """Return 'safe', 'caution', or 'destructive'. Destructive takes priority."""
    s = cmd.strip()
    for pat in _DESTRUCTIVE:
        if pat.search(s):
            return "destructive"
    for pat in _SAFE:
        if pat.match(s):
            return "safe"
    return "caution"


def append_audit_log(
    session_id: str | None,
    classification: str,
    cmd: str,
    exit_code: int | str | None = None,
) -> None:
    """Append one JSON line to .shellai/audit.log.  Best-effort — never raises."""
    try:
        log_dir = Path.cwd() / ".shellai"
        log_dir.mkdir(parents=True, exist_ok=True)
        entry: dict[str, object] = {
            "ts": datetime.now(UTC).isoformat(),
            "session": session_id or "",
            "classification": classification,
            "cmd": cmd,
        }
        if exit_code is not None:
            entry["exit_code"] = exit_code
        with (log_dir / "audit.log").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry) + "\n")
    except Exception:
        pass
