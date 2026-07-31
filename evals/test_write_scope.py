#!/usr/bin/env python3
"""evals/test_write_scope.py — Workspace write-scoping (docs/V2_PLAN.md §7).

The containment half of the safety story: the sensitive-command gate stops
exfiltration; this stops collateral damage. Mutations are confined to the
working directory (plus explicitly allowed roots only — NOT system temp);
READS stay unrestricted so the agent can still consult docs and libraries.

Usage:
    python evals/test_write_scope.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402

_CFG: dict[str, Any] = {
    **sa.DEFAULT_CONFIG,
    "backend": "mock",
    "max_agent_steps": 4,
    "autopilot_confirm_destructive": False,
    "memory_enabled": False,
    "telemetry_enabled": False,
}


class _Workspace:
    """cwd = a fresh workspace; a sibling dir stands in for 'outside'."""

    def __enter__(self):
        self._prev = Path.cwd()
        self._root = tempfile.mkdtemp(prefix="hexws_")
        self._outside = tempfile.mkdtemp(prefix="hexout_")
        os.chdir(self._root)
        sa.set_active_config(_CFG)
        return Path(self._root), Path(self._outside)

    def __exit__(self, *a):
        os.chdir(self._prev)
        sa.set_active_config(None)


def _blocked(fn, *args) -> str:
    try:
        fn(*args)
    except RuntimeError as exc:
        return str(exc)
    raise AssertionError("expected the write to be blocked")


# ---------------------------------------------------------------------------
# Inside the workspace: unchanged behaviour
# ---------------------------------------------------------------------------

def test_write_inside_workspace_allowed() -> None:
    with _Workspace() as (root, _):
        sa.write_file_tool("notes.txt", "hello")
        assert (root / "notes.txt").read_text(encoding="utf-8") == "hello"


def test_nested_write_inside_workspace_allowed() -> None:
    with _Workspace() as (root, _):
        sa.write_file_tool("src/deep/file.py", "x = 1")
        assert (root / "src" / "deep" / "file.py").exists()


def test_edit_and_append_inside_workspace_allowed() -> None:
    with _Workspace() as (root, _):
        sa.write_file_tool("a.txt", "one")
        sa.edit_file_tool("a.txt", "one", "two")
        sa.append_file_tool("a.txt", "-three")
        assert (root / "a.txt").read_text(encoding="utf-8") == "two-three"


# ---------------------------------------------------------------------------
# Outside the workspace: blocked
# ---------------------------------------------------------------------------

def test_write_outside_workspace_blocked() -> None:
    with _Workspace() as (_, outside):
        target = outside / "escaped.txt"
        msg = _blocked(sa.write_file_tool, str(target), "should not land")
        assert "outside the workspace" in msg, msg
        assert not target.exists(), "file must not be created"


def test_parent_traversal_blocked() -> None:
    with _Workspace() as (root, _):
        escaped = root.parent / "escaped.txt"
        escaped.unlink(missing_ok=True)  # never assert against stale artifacts
        msg = _blocked(sa.write_file_tool, "../escaped.txt", "nope")
        assert "outside the workspace" in msg, msg
        assert not escaped.exists(), "traversal must not create the file"


def test_absolute_home_path_blocked() -> None:
    with _Workspace() as (_, _o):
        msg = _blocked(sa.write_file_tool, str(Path.home() / "hexcli_should_not_exist.txt"), "x")
        assert "outside the workspace" in msg
        assert not (Path.home() / "hexcli_should_not_exist.txt").exists()


def test_edit_outside_workspace_blocked() -> None:
    with _Workspace() as (_, outside):
        victim = outside / "victim.txt"
        victim.write_text("original", encoding="utf-8")
        msg = _blocked(sa.edit_file_tool, str(victim), "original", "tampered")
        assert "outside the workspace" in msg
        assert victim.read_text(encoding="utf-8") == "original", "content must be untouched"


# ---------------------------------------------------------------------------
# Reads stay unrestricted
# ---------------------------------------------------------------------------

def test_read_outside_workspace_still_allowed() -> None:
    with _Workspace() as (_, outside):
        doc = outside / "reference.md"
        doc.write_text("library docs", encoding="utf-8")
        out = sa.read_file_tool(str(doc), 4000)
        assert "library docs" in out, "reads must NOT be scoped — agents need docs"


# ---------------------------------------------------------------------------
# Escape hatches
# ---------------------------------------------------------------------------

def test_explicit_allow_list_permits_write() -> None:
    with _Workspace() as (_, outside):
        sa.set_active_config({**_CFG, "workspace_write_allow": [str(outside)]})
        target = outside / "allowed.txt"
        sa.write_file_tool(str(target), "permitted")
        assert target.read_text(encoding="utf-8") == "permitted"


def test_scope_can_be_disabled_entirely() -> None:
    with _Workspace() as (_, outside):
        sa.set_active_config({**_CFG, "workspace_write_scope": False})
        target = outside / "unscoped.txt"
        sa.write_file_tool(str(target), "no scope")
        assert target.exists()


def test_system_temp_is_not_a_blanket_exception() -> None:
    """%TEMP% is shared with other apps and agents; exempting it would put a
    hole through containment for no benefit (sandboxes run with the workspace
    AS cwd, so they are already covered)."""
    with _Workspace() as (_, _o):
        target = Path(tempfile.gettempdir()) / "hexcli_scope_probe.txt"
        target.unlink(missing_ok=True)
        msg = _blocked(sa.write_file_tool, str(target), "scratch")
        assert "outside the workspace" in msg
        assert not target.exists()


# ---------------------------------------------------------------------------
# Through the production loop
# ---------------------------------------------------------------------------

def test_agent_loop_reports_block_to_model() -> None:
    with _Workspace() as (_, outside):
        target = outside / "loop_escape.txt"
        sa.set_mock_responses([
            json.dumps({"action": "write_file",
                        "args": {"path": str(target), "content": "escape"}}),
            json.dumps({"action": "finish",
                        "message": "I could not write outside the workspace."}),
        ])
        result = sa.run_autopilot(_CFG, [], "write a file over there please", "powershell.exe")
        assert not target.exists(), "loop must not create files outside the workspace"
        assert "could not write" in result.lower()


# ---------------------------------------------------------------------------
# Protocol v2 must enforce the SAME boundary
# ---------------------------------------------------------------------------

def test_v2_edit_outside_workspace_blocked() -> None:
    """Regression: loop_v2._tool_edit reimplements the edit and originally
    called only the sensitive-path guard, so protocol v2 could edit any file
    on disk while v1 was contained."""
    import hexcli.agent as agent_mod
    from hexcli import loop_v2
    with _Workspace() as (_, outside):
        victim = outside / "v2_victim.txt"
        victim.write_text("original", encoding="utf-8")
        raised = ""
        try:
            out = loop_v2._tool_edit(
                agent_mod, {"path": str(victim)}, [("original", "tampered")])
            raised = out  # dispatch converts raises to "Error: ..." strings
        except RuntimeError as exc:
            raised = str(exc)
        assert "outside the workspace" in raised, raised
        assert victim.read_text(encoding="utf-8") == "original"


def test_v2_edit_inside_workspace_allowed() -> None:
    import hexcli.agent as agent_mod
    from hexcli import loop_v2
    with _Workspace() as (root, _):
        target = root / "ok.txt"
        target.write_text("original", encoding="utf-8")
        out = loop_v2._tool_edit(
            agent_mod, {"path": str(target)}, [("original", "updated")])
        assert "Edited" in out, out
        assert target.read_text(encoding="utf-8") == "updated"


# ---------------------------------------------------------------------------
# Exhaustive: EVERY mutating entry point, both protocols
# ---------------------------------------------------------------------------

def _mutating_entry_points() -> list[tuple[str, Any]]:
    """Every way the model can change a file, as (label, call-with-a-path).

    Enumerated here so the guarantee is exhaustive rather than remembered. The
    v1/v2 split has produced this exact bug once already: a reimplemented tool
    that carried only half the gate.
    """
    import hexcli.agent as agent_mod
    from hexcli import loop_v2

    return [
        ("v1 write_file", lambda p: sa.write_file_tool(str(p), "x")),
        ("v1 append_file", lambda p: sa.append_file_tool(str(p), "x")),
        ("v1 edit_file", lambda p: sa.edit_file_tool(str(p), "original", "t")),
        ("v2 write", lambda p: loop_v2._tool_write(
            agent_mod, {"path": str(p)}, "x")),
        ("v2 edit", lambda p: loop_v2._tool_edit(
            agent_mod, {"path": str(p)}, [("original", "tampered")])),
    ]


def test_every_mutating_tool_is_write_scoped() -> None:
    """The one that would have caught the v2 edit hole on the day it landed."""
    for label, call in _mutating_entry_points():
        with _Workspace() as (_, outside):
            victim = outside / "victim.txt"
            victim.write_text("original", encoding="utf-8")
            try:
                result = str(call(victim))
            except RuntimeError as exc:
                result = str(exc)
            assert "outside the workspace" in result, f"{label} was NOT scoped: {result}"
            assert victim.read_text(encoding="utf-8") == "original", \
                f"{label} mutated a file outside the workspace"


def test_every_mutating_tool_refuses_sensitive_paths() -> None:
    """Same enumeration against the other half of the gate. A key path must be
    refused even when it is inside the workspace."""
    for label, call in _mutating_entry_points():
        with _Workspace() as (root, _):
            fake_home = root / "home"
            (fake_home / ".ssh").mkdir(parents=True)
            victim = fake_home / ".ssh" / "id_rsa"
            victim.write_text("original", encoding="utf-8")
            prev_home = sa._HOME
            # MUST be resolved. The tools resolve their path argument, and on
            # the CI runner TEMP is an 8.3 short path ("RUNNER~1") that expands
            # to a different string. An unresolved _HOME then fails
            # relative_to(), the sensitive check silently never fires, and the
            # test reports the tool as unguarded. Production is unaffected —
            # _HOME comes from Path.home(), which is already canonical.
            sa._HOME = fake_home.resolve()
            try:
                # Precondition: the fixture really does look like a key path to
                # the guard. Without this, a broken fixture is indistinguishable
                # from an unguarded tool — which is exactly how the CI run above
                # read before the .resolve() fix.
                try:
                    sa._check_sensitive_path(sa.resolve_path(str(victim)), "probe")
                    raise AssertionError(
                        "fixture broken: _check_sensitive_path does not treat "
                        f"{victim} as sensitive under _HOME={sa._HOME}")
                except RuntimeError:
                    pass
                try:
                    result = str(call(victim))
                except RuntimeError as exc:
                    result = str(exc)
            finally:
                sa._HOME = prev_home
            assert "blocked" in result.lower(), f"{label} allowed a key path: {result}"
            assert victim.read_text(encoding="utf-8") == "original", \
                f"{label} mutated an SSH key"


def test_guard_mutation_applies_both_checks() -> None:
    """guard_mutation exists so the pair cannot come apart again."""
    import inspect
    src = inspect.getsource(sa.guard_mutation)
    assert "_check_sensitive_path" in src and "_check_write_scope" in src


TESTS = [
    test_every_mutating_tool_is_write_scoped,
    test_every_mutating_tool_refuses_sensitive_paths,
    test_guard_mutation_applies_both_checks,
    test_v2_edit_outside_workspace_blocked,
    test_v2_edit_inside_workspace_allowed,
    test_write_inside_workspace_allowed,
    test_nested_write_inside_workspace_allowed,
    test_edit_and_append_inside_workspace_allowed,
    test_write_outside_workspace_blocked,
    test_parent_traversal_blocked,
    test_absolute_home_path_blocked,
    test_edit_outside_workspace_blocked,
    test_read_outside_workspace_still_allowed,
    test_explicit_allow_list_permits_write,
    test_scope_can_be_disabled_entirely,
    test_system_temp_is_not_a_blanket_exception,
    test_agent_loop_reports_block_to_model,
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
    print(f"\nevals/test_write_scope.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
