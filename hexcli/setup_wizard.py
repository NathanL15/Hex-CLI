"""hexcli/setup_wizard.py — /setup: interactive configuration wizard.

A handful of questions covering the config decisions that actually matter
day one (safety posture, network policy, UI). Each shows the current value
as the default, so Enter-Enter-Enter through the wizard changes nothing.

It writes ONLY the keys the user was asked about, merged over whatever the
config file already contains — never a full DEFAULT_CONFIG dump. A config
file full of copied defaults is exactly the drift bug the generated example
config exists to prevent: values silently pinned at whatever release they
were written by.

IO is injected (ask/echo) so the whole flow is testable offline; EOF or
Ctrl+C at any question aborts without writing.
"""
from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hexcli.ui import C, cprint

# (key, question, kind, choices) — kind is "bool" or "choice".
QUESTIONS: list[tuple[str, str, str, tuple[str, ...]]] = [
    ("autopilot_confirm_destructive",
     "Confirm before running destructive commands (rm, format, …)?", "bool", ()),
    ("autopilot_confirm_sensitive",
     "Confirm before touching sensitive paths (ssh keys, credentials)?", "bool", ()),
    ("workspace_write_scope",
     "Block file writes outside the project directory?", "bool", ()),
    ("network_access",
     "Network access for fetch_url", "choice", ("ask", "deny", "allow")),
    ("show_diffs",
     "Show a diff after every file change?", "bool", ()),
    ("rich_input",
     "Rich input line (history, Tab completion, word editing)?", "bool", ()),
]


def _ask_bool(ask: Callable[[str], str], question: str, current: bool) -> bool:
    hint = "[Y/n]" if current else "[y/N]"
    answer = ask(f"  {question} {hint} ").strip().lower()
    if not answer:
        return current
    return answer in ("y", "yes", "1", "true", "on")


def _ask_choice(ask: Callable[[str], str], question: str,
                choices: tuple[str, ...], current: str) -> str:
    menu = "/".join(c.upper() if c == current else c for c in choices)
    while True:
        answer = ask(f"  {question} ({menu})? ").strip().lower()
        if not answer:
            return current
        if answer in choices:
            return answer
        cprint(f"    Please answer one of: {', '.join(choices)}", C.DIM)


def write_config_keys(path: Path, chosen: dict[str, Any]) -> None:
    """Merge the chosen keys over the existing file, atomically."""
    existing: dict[str, Any] = {}
    if path.exists():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing = data
        except (json.JSONDecodeError, OSError):
            pass  # unreadable file: the wizard's keys become the file
    existing.update(chosen)
    payload = json.dumps(existing, indent=2) + "\n"
    tmp = path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(path)


def overridden_by_project(chosen: dict[str, Any], project_cfg: Path) -> list[str]:
    """Keys the wizard just saved that a project .shellai/config.json will
    still override on every load (it deep-merges on top of the user config).

    Without this, the wizard's closing "applies on every launch" line is a
    lie in any repo that ships its own config.
    """
    if not project_cfg.exists():
        return []
    try:
        data = json.loads(project_cfg.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    if not isinstance(data, dict):
        return []
    return [k for k in chosen if k in data and data[k] != chosen[k]]


def run_wizard(
    config: dict[str, Any],
    config_path: Path,
    ask: Callable[[str], str] = input,
    project_cfg: Path | None = None,
) -> bool:
    """Run the wizard. Returns True if the config file was written.

    Updates `config` in place on success so answers apply to the running
    session immediately, not just the next launch.
    """
    print()
    cprint("  Hex CLI setup  (Enter keeps the current value)", C.BOLD)
    print()
    chosen: dict[str, Any] = {}
    try:
        for key, question, kind, choices in QUESTIONS:
            if kind == "bool":
                chosen[key] = _ask_bool(ask, question, bool(config.get(key, True)))
            else:
                current = str(config.get(key, choices[0]))
                if current not in choices:
                    current = choices[0]
                chosen[key] = _ask_choice(ask, question, choices, current)
        print()
        for key, value in chosen.items():
            marker = "" if config.get(key) == value else "  (changed)"
            cprint(f"    {key} = {value!r}{marker}", C.DIM)
        print()
        confirm = ask(f"  Save to {config_path.name}? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        cprint("  Setup cancelled; nothing written.", C.YELLOW)
        return False
    if confirm not in ("", "y", "yes"):
        cprint("  Nothing written.", C.YELLOW)
        return False
    write_config_keys(config_path, chosen)
    config.update(chosen)
    cprint(f"  Saved to {config_path}. Applied to this session.", C.BCYAN)
    shadowed = overridden_by_project(
        chosen, project_cfg if project_cfg is not None else Path.cwd() / ".shellai" / "config.json"
    )
    if shadowed:
        cprint(f"  Note: this project's .shellai/config.json overrides "
               f"{', '.join(sorted(shadowed))} on every load — edit it there too.", C.YELLOW)
    return True
