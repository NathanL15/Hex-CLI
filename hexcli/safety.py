#!/usr/bin/env python3
"""hexcli.safety — Command safety classifier and append-only audit log."""
from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path

from . import psparse

# Patterns checked in order: first match wins.  Destructive > safe > caution.

_DESTRUCTIVE: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in [
    r"\bremove-item\b",
    r"(?<![a-z])rm\s",              # rm <args> but not 'strm' etc.
    r"(?<![a-z])del\s",
    r"(?<![a-z])rd\s",
    r"\berase\b",
    # Format-Volume / Format-Disk / the DOS `format X:` — not PowerShell's
    # output formatters (Format-List, Format-Table, Format-Wide, Format-Custom,
    # Format-Hex), which the CPU/RAM cookbook queries end in. Those asked for
    # confirmation for weeks and were auto-denied in every unattended eval.
    r"\bformat-(?!list\b|table\b|wide\b|custom\b|hex\b)\w+",
    r"^\s*format\s+[a-z]:",
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

# Sensitive-data access: not destructive, but touching credentials, keys, or
# security-critical system files. Requires explicit confirmation (deny when
# non-interactive). Checked BEFORE the safe list — v1.7's blanket `^get-\w+`
# safe rule waved `Get-Content …\drivers\etc\hosts` straight through, which is
# exactly the injection payload uc3-t7 measured executing 3/3.
_SENSITIVE: list[re.Pattern[str]] = [re.compile(p, re.IGNORECASE) for p in [
    r"[\\/]\.ssh\b",                              # ssh keys & config
    r"\bid_(rsa|ed25519|ecdsa|dsa)\b",
    r"[\\/]\.gnupg\b",
    r"[\\/]\.aws\b",
    r"[\\/]\.azure\b",
    r"drivers[\\/]+etc[\\/]+hosts\b",             # hosts-file tampering/recon
    r"\bconfig[\\/]+(sam|security|system)\b",     # registry hive files
    r"\breg\s+(save|export)\b.*\b(sam|security|system)\b",
    r"\bntds\.dit\b",
    r"\bvaultcmd\b",                              # Windows credential vault
    r"\bcmdkey\b.*/list",
    r"login data",                                # browser credential stores
    r"-encodedcommand\b|\s-enc\b|\s-e[nc]?\b\s+[A-Za-z0-9+/=]{20,}",  # obfuscated exec
    r"frombase64string",
    r"protecteddata\]::unprotect",                # DPAPI secrets
    r"\$env:\w*(token|secret|password|api_?key)\w*",  # credential env vars
    # Launching a program image (uc3-t9's calc.exe): run_code's workspace
    # boundary refuses `C:\Windows\System32\calc.exe`, but the same payload
    # routed through run_command ran three different ways in one afternoon
    # (2026-09-02: the absolute path, bare `calc.exe`, `start calc.exe`).
    # Any command segment that STARTS with an executable image, plus
    # Start-Process / start / cmd /c start, is confirm-gated like the rest
    # of this tier. `python script.py`, cmdlets and git are untouched.
    r"(?:^|[&|;]\s*)(?:&\s*)?(?:\"[^\"]*?\.(?:exe|com|bat|cmd|msi|scr|vbs|ps1|js)\b[^\"]*\""
    r"|'[^']*?\.(?:exe|com|bat|cmd|msi|scr|vbs|ps1|js)\b[^']*'"
    r"|(?:[a-z]:[\\/]|[.\\/]+)?[^\s\"'|&;]*?\.(?:exe|com|bat|cmd|msi|scr|vbs|ps1|js)\b)",
    r"(?:^|[&|;]\s*)(?:start-process|start|saps)\b",
    r"\bcmd(\.exe)?\s+/[ck]\s+start\b",
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


_SEVERITY = {"safe": 0, "caution": 1, "sensitive": 2, "destructive": 3}


def _worse(a: str, b: str) -> str:
    """The more severe of two verdicts. An unknown or empty verdict ranks
    below every real one, so "nothing to say about this name" can never
    displace `safe` — it did while the default was `caution`'s rank, which
    turned `Get-ChildItem` into an empty classification."""
    return a if _SEVERITY.get(a, -1) >= _SEVERITY.get(b, -1) else b


def _classify_text(s: str) -> str:
    """The pattern tiers, matched against the command as written."""
    for pat in _DESTRUCTIVE:
        if pat.search(s):
            return "destructive"
    for pat in _SENSITIVE:
        if pat.search(s):
            return "sensitive"
    for pat in _SAFE:
        if pat.match(s):
            return "safe"
    return "caution"


def _tier_for_name(name: str) -> str:
    """What the tiers would say about a bare command name. Deriving this from
    the same patterns is deliberate: resolving an alias must not invent a
    policy, only apply the existing one to the name the shell will really
    run. `ri` becomes `remove-item` and is destructive because
    `Remove-Item` always was; `clc` becomes `clear-content` and stays
    caution because `Clear-Content` is caution today."""
    probe = name + " "
    for pat in _DESTRUCTIVE:
        if pat.search(probe):
            return "destructive"
    for pat in _SENSITIVE:
        if pat.search(probe):
            return "sensitive"
    return ""


def classify_command(cmd: str) -> str:
    """Return 'safe', 'caution', 'sensitive', or 'destructive'.

    Priority: destructive > sensitive > safe > caution. Sensitive must outrank
    the safe list, or read-only cmdlet prefixes whitelist credential access.

    Text alone is not enough. Measured 2026-09-15: `ri C:\\data`,
    `rmdir C:\\data` and `$c='Remove-Item'; & ($a+$b)` all ran with no
    confirmation, because an alias or a computed name never appears in a
    pattern. `hexcli.psparse` resolves the command names the shell will
    actually run (the agent's shell is `-NoProfile`, so only built-in
    aliases exist) and reports a name that is not a literal at all. The
    result can only ever be MORE severe than the text verdict: when the
    parser is unavailable this is exactly the behaviour it always had.
    """
    s = cmd.strip()
    verdict = _classify_text(s)
    facts = psparse.facts(s)
    if not facts.ok:
        return verdict
    for name in facts.names:
        verdict = _worse(verdict, _tier_for_name(name))
    if facts.nonliteral:
        # `& ($a + $b)` — the name is computed, so nothing can be checked
        # about it before it runs. Confirm rather than assume.
        verdict = _worse(verdict, "sensitive")
    return verdict


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
