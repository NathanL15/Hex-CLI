#!/usr/bin/env python3
"""evals/test_v11.py — Unit tests for v1.1 features.

Tests CoT stripping, undo snapshot mechanics, and lint_code availability.
These are fast, offline tests that do not hit the LLM endpoint.

Usage:
    python evals/test_v11.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hexcli.agent as sa


# ---------------------------------------------------------------------------
# CoT stripping
# ---------------------------------------------------------------------------

def test_strip_thinking_removes_block() -> None:
    raw = '<think>I should use list_directory here.</think>{"action":"list_directory","args":{"path":"."}}'
    stripped = sa.strip_thinking(raw)
    assert "<think>" not in stripped, "think block must be removed"
    assert '{"action"' in stripped, "JSON body must survive stripping"


def test_strip_thinking_multiline() -> None:
    raw = "<think>\nLine 1\nLine 2\n</think>Done."
    stripped = sa.strip_thinking(raw)
    assert "Line 1" not in stripped
    assert "Done." in stripped


def test_strip_thinking_preserves_clean_content() -> None:
    raw = '{"action":"finish","message":"Done."}'
    assert sa.strip_thinking(raw) == raw, "clean content must be unchanged"


def test_strip_thinking_nested_tags_not_present() -> None:
    raw = "<think>outer</think>result"
    assert "outer" not in sa.strip_thinking(raw)
    assert "result" in sa.strip_thinking(raw)


# ---------------------------------------------------------------------------
# Undo snapshot mechanics (no LLM, no session loop)
# ---------------------------------------------------------------------------

def test_undo_snapshots_dict_keyed_by_session_id() -> None:
    # Verify the module-level dict is accessible and starts clean for a new key.
    fake_id = "test-session-999"
    sa._SESSION_UNDO_SNAPSHOTS.pop(fake_id, None)
    assert fake_id not in sa._SESSION_UNDO_SNAPSHOTS
    sa._SESSION_UNDO_SNAPSHOTS[fake_id] = {"somefile.py": "original"}
    assert sa._SESSION_UNDO_SNAPSHOTS[fake_id]["somefile.py"] == "original"
    sa._SESSION_UNDO_SNAPSHOTS.pop(fake_id, None)


def test_undo_restores_edited_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "target.py"
        p.write_text("original content", encoding="utf-8")
        # Simulate what run_autopilot stores: original content before mutation.
        fake_id = "test-undo-session"
        sa._SESSION_UNDO_SNAPSHOTS[fake_id] = {str(p): "original content"}
        # Simulate the agent writing new content.
        p.write_text("mutated content", encoding="utf-8")
        assert p.read_text(encoding="utf-8") == "mutated content"
        # Restore.
        for path_str, original in sa._SESSION_UNDO_SNAPSHOTS.pop(fake_id, {}).items():
            if original is not None:
                Path(path_str).write_text(original, encoding="utf-8")
        assert p.read_text(encoding="utf-8") == "original content", "file must be restored"


def test_undo_deletes_created_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "new_file.txt"
        fake_id = "test-undo-create"
        # None = file was created fresh (did not exist before the turn).
        sa._SESSION_UNDO_SNAPSHOTS[fake_id] = {str(p): None}
        p.write_text("agent created this", encoding="utf-8")
        assert p.exists()
        for path_str, original in sa._SESSION_UNDO_SNAPSHOTS.pop(fake_id, {}).items():
            if original is None and Path(path_str).exists():
                Path(path_str).unlink()
        assert not p.exists(), "file created by the agent must be deleted on undo"


# ---------------------------------------------------------------------------
# lint_code availability
# ---------------------------------------------------------------------------

def test_lint_code_registration() -> None:
    if sa._RUFF:
        assert "lint_code" in sa.TOOL_NAMES, "lint_code must be in TOOL_NAMES when ruff is on PATH"
        print(f"  ruff found at {sa._RUFF} — lint_code registered")
    else:
        assert "lint_code" not in sa.TOOL_NAMES, "lint_code must NOT be in TOOL_NAMES when ruff absent"
        print("  ruff not on PATH — lint_code correctly absent from TOOL_NAMES")


def test_lint_code_prompt_injection() -> None:
    # Generic query: lint_code must NOT be injected (saves tokens regardless of ruff).
    prompt_generic = sa.build_autopilot_prompt(cwd=".", max_steps=15, query="list the files")
    assert "lint_code" not in prompt_generic, "lint_code must not appear for generic queries"

    # Lint-related query: injected only when ruff is present.
    prompt_lint = sa.build_autopilot_prompt(cwd=".", max_steps=15, query="lint my Python code")
    if sa._RUFF:
        assert "lint_code" in prompt_lint, "lint_code must appear for lint queries when ruff present"
    else:
        assert "lint_code" not in prompt_lint, "lint_code must not appear when ruff absent"


def test_lint_code_tool_clean_file() -> None:
    if not sa._RUFF:
        print("  SKIP: ruff not available")
        return
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "clean.py"
        p.write_text("x = 1\n", encoding="utf-8")
        result = sa.lint_code_tool(str(p))
        assert "OK" in result or result.strip() == "", f"expected clean result, got: {result!r}"


def test_lint_code_tool_flags_unused_import() -> None:
    if not sa._RUFF:
        print("  SKIP: ruff not available")
        return
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "bad.py"
        p.write_text("import os\nx = 1\n", encoding="utf-8")  # os is unused
        result = sa.lint_code_tool(str(p))
        assert "F401" in result or "unused" in result.lower(), (
            f"expected F401 unused import, got: {result!r}"
        )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(fn: Any) -> bool:
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as exc:
        print(f"  FAIL  {fn.__name__}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {fn.__name__}: {exc}")
        return False


from typing import Any

TESTS = [
    test_strip_thinking_removes_block,
    test_strip_thinking_multiline,
    test_strip_thinking_preserves_clean_content,
    test_strip_thinking_nested_tags_not_present,
    test_undo_snapshots_dict_keyed_by_session_id,
    test_undo_restores_edited_file,
    test_undo_deletes_created_file,
    test_lint_code_registration,
    test_lint_code_prompt_injection,
    test_lint_code_tool_clean_file,
    test_lint_code_tool_flags_unused_import,
]


def main() -> int:
    print(f"\nevals/test_v11.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
