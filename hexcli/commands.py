"""hexcli/commands.py — user-authored custom slash commands.

A custom command is a markdown file whose stem is the command name:

    ~/.shellai/commands/review.md        →  /review   (global)
    <cwd>/.shellai/commands/review.md    →  /review   (project; wins collisions)

The file body is a prompt template that runs as a normal agent turn.
``$ARGUMENTS`` inside the template is replaced with whatever the user typed
after the command; a template with no placeholder gets the arguments
appended instead, so both styles just work.

Built-in commands always win: run_repl only consults this module after its
own dispatch has not matched, so a custom ``/help`` can exist but can never
shadow the real one. Templates are the user's own local files — the same
trust level as their config — so their content is not sanitised.
"""
from __future__ import annotations

import re
from pathlib import Path

# Command names stay boring on purpose: they share a namespace with
# built-ins and Tab completion, and "my command.md" or "café.md" would
# produce names the parser splits apart.
_VALID_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

ARGUMENTS_PLACEHOLDER = "$ARGUMENTS"


def _command_dirs(home: Path | None = None, cwd: Path | None = None) -> list[Path]:
    """Global dir first, project dir last — later entries win collisions."""
    home = home or Path.home()
    cwd = cwd or Path.cwd()
    dirs = [home / ".shellai" / "commands"]
    project = cwd / ".shellai" / "commands"
    if project.resolve() != dirs[0].resolve():
        dirs.append(project)
    return dirs


def discover(home: Path | None = None, cwd: Path | None = None) -> dict[str, Path]:
    """Map of ``/name`` → template path for every valid command file."""
    found: dict[str, Path] = {}
    for directory in _command_dirs(home, cwd):
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.md")):
            name = path.stem.lower()
            if _VALID_NAME.match(name):
                found["/" + name] = path
    return found


def load(command_word: str, home: Path | None = None, cwd: Path | None = None) -> str | None:
    """Template text for ``/name``, or None if no such custom command."""
    path = discover(home, cwd).get(command_word.lower())
    if path is None:
        return None
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def expand(template: str, args: str) -> str:
    """Substitute the user's arguments into the template."""
    template = template.strip()
    if ARGUMENTS_PLACEHOLDER in template:
        return template.replace(ARGUMENTS_PLACEHOLDER, args)
    if args:
        return f"{template}\n\n{args}"
    return template
