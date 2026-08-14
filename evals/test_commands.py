#!/usr/bin/env python3
"""evals/test_commands.py — custom slash commands (.shellai/commands/*.md).

Covers discovery (global + project, precedence, name validation), template
expansion ($ARGUMENTS substitution and append fallback), and the REPL
integration invariants: built-ins must dispatch before the custom lookup,
and the source-parsing drift guard in test_lineedit must not be confused by
the custom-command code path.

Usage:
    python evals/test_commands.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from hexcli import commands  # noqa: E402

# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def test_discover_finds_global_and_project() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cwd = Path(tmp) / "proj"
        (home / ".shellai" / "commands").mkdir(parents=True)
        (cwd / ".shellai" / "commands").mkdir(parents=True)
        (home / ".shellai" / "commands" / "review.md").write_text("global review", encoding="utf-8")
        (cwd / ".shellai" / "commands" / "deploy.md").write_text("project deploy", encoding="utf-8")
        found = commands.discover(home=home, cwd=cwd)
        assert set(found) == {"/review", "/deploy"}, found


def test_project_wins_name_collision() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cwd = Path(tmp) / "proj"
        (home / ".shellai" / "commands").mkdir(parents=True)
        (cwd / ".shellai" / "commands").mkdir(parents=True)
        (home / ".shellai" / "commands" / "review.md").write_text("GLOBAL", encoding="utf-8")
        (cwd / ".shellai" / "commands" / "review.md").write_text("PROJECT", encoding="utf-8")
        assert commands.load("/review", home=home, cwd=cwd) == "PROJECT"


def test_invalid_names_are_skipped() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp) / "home"
        cmd_dir = home / ".shellai" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "has space.md").write_text("x", encoding="utf-8")
        (cmd_dir / "-leading.md").write_text("x", encoding="utf-8")
        (cmd_dir / "ok-name_2.md").write_text("x", encoding="utf-8")
        found = commands.discover(home=home, cwd=home)
        assert set(found) == {"/ok-name_2"}, found


def test_discover_survives_missing_dirs() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        nowhere = Path(tmp) / "empty"
        nowhere.mkdir()
        assert commands.discover(home=nowhere, cwd=nowhere) == {}


def test_load_unknown_returns_none() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp)
        assert commands.load("/nope", home=p, cwd=p) is None


def test_lookup_is_case_insensitive() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        home = Path(tmp)
        cmd_dir = home / ".shellai" / "commands"
        cmd_dir.mkdir(parents=True)
        (cmd_dir / "review.md").write_text("body", encoding="utf-8")
        assert commands.load("/REVIEW", home=home, cwd=home) == "body"


# ---------------------------------------------------------------------------
# Expansion
# ---------------------------------------------------------------------------

def test_expand_substitutes_arguments() -> None:
    out = commands.expand("Review $ARGUMENTS carefully.", "src/main.py")
    assert out == "Review src/main.py carefully."


def test_expand_substitutes_every_occurrence() -> None:
    out = commands.expand("$ARGUMENTS then $ARGUMENTS", "x")
    assert out == "x then x"


def test_expand_appends_when_no_placeholder() -> None:
    out = commands.expand("Fix the failing tests.", "in evals/")
    assert out == "Fix the failing tests.\n\nin evals/"


def test_expand_empty_args() -> None:
    assert commands.expand("Do the thing with $ARGUMENTS.", "") == "Do the thing with ."
    assert commands.expand("Just do the thing.", "") == "Just do the thing."


# ---------------------------------------------------------------------------
# REPL integration invariants
# ---------------------------------------------------------------------------

def test_builtins_dispatch_before_custom_lookup() -> None:
    """A custom /help file must never shadow the real /help: in run_repl's
    source, every built-in dispatch must appear before the custom-command
    lookup."""
    import inspect

    source = inspect.getsource(sa.run_repl)
    custom_at = source.index("custom_commands.load")
    for builtin in sa.REPL_COMMANDS:
        handled_at = source.index(f'"{builtin}')
        assert handled_at < custom_at, \
            f"{builtin} is dispatched after the custom lookup — a custom file could shadow it"


def test_every_builtin_name_claims_its_bare_form() -> None:
    """Behavioural version of the ordering test above, which only checked
    source position. /save, /load and /model matched ONLY their "<cmd> <arg>"
    form, so typing the bare word fell through to the custom lookup and ran a
    user template as an agent task. Every advertised built-in must handle its
    bare word."""
    import inspect
    import re as _re

    source = "\n".join(
        line for line in inspect.getsource(sa.run_repl).splitlines()
        if not line.lstrip().startswith("#")
    )
    # Forms that constitute claiming the bare word:
    #   norm == "/x"      |  norm in {"/x", ...}  |  guarded by REPL_COMMANDS
    bare_claimed = set(_re.findall(r'norm == "(/[a-z]+)"', source))
    bare_claimed |= set(_re.findall(r'"(/[a-z]+)"[,}]', source))
    missing = [c for c in sa.REPL_COMMANDS if c not in bare_claimed]
    assert not missing, f"built-ins that never match their bare form: {missing}"


def test_builtin_names_are_never_looked_up_as_custom() -> None:
    """The structural guard: even if a dispatch branch someday stops claiming
    its bare form, a built-in NAME must not reach the custom lookup."""
    import inspect

    source = inspect.getsource(sa.run_repl)
    assert "cmd_word.lower() in REPL_COMMANDS" in source, \
        "the custom-command lookup must exclude built-in names"


def test_closest_command_includes_custom_names() -> None:
    got = sa._closest_command("/reviwe", extra=("/review",))
    assert got == "/review"


def test_closest_command_still_finds_builtins() -> None:
    assert sa._closest_command("/hlep") == "/help"


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(fn) -> bool:
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


TESTS = [
    test_discover_finds_global_and_project,
    test_project_wins_name_collision,
    test_invalid_names_are_skipped,
    test_discover_survives_missing_dirs,
    test_load_unknown_returns_none,
    test_lookup_is_case_insensitive,
    test_expand_substitutes_arguments,
    test_expand_substitutes_every_occurrence,
    test_expand_appends_when_no_placeholder,
    test_expand_empty_args,
    test_builtins_dispatch_before_custom_lookup,
    test_every_builtin_name_claims_its_bare_form,
    test_builtin_names_are_never_looked_up_as_custom,
    test_closest_command_includes_custom_names,
    test_closest_command_still_finds_builtins,
]


def main() -> int:
    print(f"\nevals/test_commands.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
