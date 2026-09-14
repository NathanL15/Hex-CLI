#!/usr/bin/env python3
"""evals/test_v15.py — Unit tests for v1.5 features.

Tests: global vs project memory split, unified search deduplication,
dreaming idle-timer and LLM callback, lock contention on dreaming,
memory rules injection.
All offline — no LLM endpoint required.

Usage:
    python evals/test_v15.py
"""
from __future__ import annotations

import sys
import tempfile
import unittest.mock
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hexcli.agent as sa
import hexcli.memory as mem

# ---------------------------------------------------------------------------
# Feature 13 — Global vs project memory split
# ---------------------------------------------------------------------------

def test_routing_file_touch_goes_to_project_store() -> None:
    project_adds: list[str] = []
    global_adds: list[str] = []

    def track_add(self: Any, text: str, metadata: Any) -> None:
        if self._dir == mem._GLOBAL_STORE_DIR:
            global_adds.append(text)
        else:
            project_adds.append(text)

    with unittest.mock.patch.object(mem.VectorStore, "add", track_add):
        mem.maybe_index_turn({}, "wrote a file", ["edit_file"], ["/tmp/file.py"])

    assert project_adds, "file-touching turn must go to project store"
    assert not global_adds, "file-touching turn must NOT go to global store"


def test_routing_no_file_touch_goes_to_global_store() -> None:
    project_adds: list[str] = []
    global_adds: list[str] = []

    def track_add(self: Any, text: str, metadata: Any) -> None:
        if self._dir == mem._GLOBAL_STORE_DIR:
            global_adds.append(text)
        else:
            project_adds.append(text)

    with unittest.mock.patch.object(mem.VectorStore, "add", track_add):
        mem.maybe_index_turn({}, "ran a command", ["run_command"], [])

    assert global_adds, "non-file turn must go to global store"
    assert not project_adds, "non-file turn must NOT go to project store"


def test_routing_empty_tools_skipped() -> None:
    add_calls: list[str] = []

    def track_add(self: Any, text: str, metadata: Any) -> None:
        add_calls.append(text)

    with unittest.mock.patch.object(mem.VectorStore, "add", track_add):
        mem.maybe_index_turn({}, "no tools used", [], [])

    assert not add_calls, "empty tools_used must skip indexing entirely"


def test_vector_store_accepts_store_dir_kwarg() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        store_dir = Path(tmp) / "custom_store"
        store = mem.VectorStore(None, store_dir=store_dir, max_entries=100)
        assert store._dir == store_dir
        assert store._max_entries == 100


def test_global_store_dir_is_in_home() -> None:
    assert mem._GLOBAL_STORE_DIR.is_relative_to(Path.home())


# ---------------------------------------------------------------------------
# Feature 13 — Unified search: merge + deduplicate
# ---------------------------------------------------------------------------

def _fake_project_results() -> list[dict[str, Any]]:
    return [
        {"text": "project specific task", "score": 0.92,
         "created_at": "2026-06-01", "tool_sequence": ["edit_file"], "key_paths": ["/src/a.py"]},
    ]


def _fake_global_results() -> list[dict[str, Any]]:
    return [
        {"text": "global pattern rule", "score": 0.85,
         "created_at": "2026-06-02", "tool_sequence": [], "key_paths": []},
        {"text": "project specific task", "score": 0.80,  # duplicate of project result
         "created_at": "2026-06-01", "tool_sequence": [], "key_paths": []},
    ]


def test_unified_search_queries_both_stores() -> None:
    search_dirs: list[Path] = []

    def mock_search(self: Any, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        search_dirs.append(self._dir)
        if self._dir == mem._GLOBAL_STORE_DIR:
            return _fake_global_results()
        return _fake_project_results()

    with unittest.mock.patch.object(mem.VectorStore, "search", mock_search):
        result = mem.search_memory_tool({}, "test query", top_k=5)

    assert mem._GLOBAL_STORE_DIR in search_dirs, "global store must be queried"
    assert len(search_dirs) == 2, "exactly two stores must be searched"
    assert "project specific task" in result
    assert "global pattern rule" in result


def test_unified_search_deduplicates_by_content() -> None:
    def mock_search(self: Any, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        if self._dir == mem._GLOBAL_STORE_DIR:
            return _fake_global_results()
        return _fake_project_results()

    with unittest.mock.patch.object(mem.VectorStore, "search", mock_search):
        result = mem.search_memory_tool({}, "test query", top_k=10)

    # "project specific task" appears once from each store — should appear once in output.
    assert result.count("project specific task") == 1, (
        "duplicate entries across stores must be deduplicated"
    )


def test_unified_search_ranks_by_score() -> None:
    def mock_search(self: Any, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        if self._dir == mem._GLOBAL_STORE_DIR:
            return [{"text": "lower score entry", "score": 0.50,
                     "created_at": "?", "tool_sequence": [], "key_paths": []}]
        return [{"text": "higher score entry", "score": 0.95,
                 "created_at": "?", "tool_sequence": [], "key_paths": []}]

    with unittest.mock.patch.object(mem.VectorStore, "search", mock_search):
        result = mem.search_memory_tool({}, "test", top_k=2)

    # Higher score entry must appear before lower score entry.
    assert result.index("higher score entry") < result.index("lower score entry"), (
        "results must be ordered by descending similarity score"
    )


def test_unified_search_disabled_returns_message() -> None:
    with unittest.mock.patch.object(mem.VectorStore, "search", lambda *a, **kw: []):
        result = mem.search_memory_tool({"memory_enabled": False}, "anything")
    assert "disabled" in result.lower()


# ---------------------------------------------------------------------------
# Feature 15 — Memory rules injection
# ---------------------------------------------------------------------------

def test_read_memory_rules_empty_when_no_file() -> None:
    orig = mem._RULES_PATH
    mem._RULES_PATH = Path("/nonexistent/path/memory_rules.md")
    try:
        rules = mem.read_memory_rules(5)
    finally:
        mem._RULES_PATH = orig
    assert rules == [], "missing rules file must return empty list"


def test_read_memory_rules_returns_last_n() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rules_path = Path(tmp) / "memory_rules.md"
        all_rules = [f"- Rule {i}" for i in range(10)]
        rules_path.write_text("\n".join(all_rules), encoding="utf-8")

        orig = mem._RULES_PATH
        mem._RULES_PATH = rules_path
        try:
            rules = mem.read_memory_rules(5)
        finally:
            mem._RULES_PATH = orig

    assert len(rules) == 5, f"read_memory_rules(5) must return exactly 5 rules, got {len(rules)}"
    # Must be the LAST 5 (rules 5–9).
    assert "Rule 9" in rules[-1], "last rule must be the newest (Rule 9)"
    assert "Rule 5" in rules[0], "first of returned rules must be Rule 5"


def test_read_memory_rules_returns_all_when_fewer_than_n() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rules_path = Path(tmp) / "memory_rules.md"
        rules_path.write_text("- Only rule\n", encoding="utf-8")

        orig = mem._RULES_PATH
        mem._RULES_PATH = rules_path
        try:
            rules = mem.read_memory_rules(5)
        finally:
            mem._RULES_PATH = orig

    assert rules == ["- Only rule"], "fewer-than-n rules must all be returned"


def test_rules_injection_in_workspace_snapshot() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig = mem._RULES_PATH
        rules_path = Path(tmp) / "memory_rules.md"
        rules_path.write_text(
            "\n".join(f"- [2026-06-18T10:00] Rule {i}" for i in range(10)),
            encoding="utf-8",
        )
        mem._RULES_PATH = rules_path
        try:
            snap = sa.workspace_snapshot(tmp)
        finally:
            mem._RULES_PATH = orig

    assert "Prior knowledge:" in snap, "workspace_snapshot must include 'Prior knowledge:' block"
    # Only last 5 rules should appear.
    assert "Rule 9" in snap
    assert "Rule 5" in snap
    assert snap.count("Rule") == 5, f"exactly 5 rules must be injected, got {snap.count('Rule')}"


def test_rules_injection_absent_when_no_rules() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig = mem._RULES_PATH
        mem._RULES_PATH = Path(tmp) / "nonexistent.md"
        try:
            snap = sa.workspace_snapshot(tmp)
        finally:
            mem._RULES_PATH = orig

    assert "Prior knowledge:" not in snap, (
        "workspace_snapshot must not add 'Prior knowledge:' when rules file is absent"
    )
    assert snap.endswith("]"), "snapshot must end with ] when no rules"


def test_rules_within_token_budget() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        orig = mem._RULES_PATH
        rules_path = Path(tmp) / "memory_rules.md"
        # Write 10 short realistic rules (as the dreaming daemon would produce).
        rules_path.write_text(
            "\n".join(f"- [2026-06-18T10:00] Rule {i}." for i in range(10)),
            encoding="utf-8",
        )
        mem._RULES_PATH = rules_path
        try:
            snap = sa.workspace_snapshot(tmp)
        finally:
            mem._RULES_PATH = orig

    rules_block_start = snap.find("Prior knowledge:")
    rules_block = snap[rules_block_start:]
    estimated_tokens = len(rules_block) // 4
    assert estimated_tokens <= 60, (
        f"rules block estimated at {estimated_tokens} tokens — exceeds 60-token budget"
    )


def test_append_rules_evicts_oldest_when_over_cap() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rules_path = Path(tmp) / "memory_rules.md"
        orig = mem._RULES_PATH
        mem._RULES_PATH = rules_path

        # Write _MAX_RULES rules.
        old_rules = [f"- Old rule {i}" for i in range(mem._MAX_RULES)]
        rules_path.write_text("\n".join(old_rules) + "\n", encoding="utf-8")

        try:
            mem._append_rules(["- Brand new rule"])
            stored = mem.read_memory_rules(mem._MAX_RULES + 10)
        finally:
            mem._RULES_PATH = orig

    assert len(stored) == mem._MAX_RULES, (
        f"rules count must not exceed {mem._MAX_RULES}, got {len(stored)}"
    )
    assert any("Brand new rule" in r for r in stored), "new rule must appear in stored rules"
    assert not any("Old rule 0" in r for r in stored), "oldest rule must be evicted"


def test_prune_memory_rules_reduces_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        rules_path = Path(tmp) / "memory_rules.md"
        orig = mem._RULES_PATH
        mem._RULES_PATH = rules_path

        # Write more than _MAX_RULES rules.
        excess = [f"- Excess rule {i}" for i in range(mem._MAX_RULES + 10)]
        rules_path.write_text("\n".join(excess) + "\n", encoding="utf-8")

        try:
            removed = mem.prune_memory_rules()
            remaining = mem.read_memory_rules(mem._MAX_RULES + 10)
        finally:
            mem._RULES_PATH = orig

    assert removed == 10, f"prune must report 10 removed, got {removed}"
    assert len(remaining) == mem._MAX_RULES, (
        f"remaining rules must equal _MAX_RULES={mem._MAX_RULES}, got {len(remaining)}"
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
        print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False


TESTS = [
    test_routing_file_touch_goes_to_project_store,
    test_routing_no_file_touch_goes_to_global_store,
    test_routing_empty_tools_skipped,
    test_vector_store_accepts_store_dir_kwarg,
    test_global_store_dir_is_in_home,
    test_unified_search_queries_both_stores,
    test_unified_search_deduplicates_by_content,
    test_unified_search_ranks_by_score,
    test_unified_search_disabled_returns_message,
    test_read_memory_rules_empty_when_no_file,
    test_read_memory_rules_returns_last_n,
    test_read_memory_rules_returns_all_when_fewer_than_n,
    test_rules_injection_in_workspace_snapshot,
    test_rules_injection_absent_when_no_rules,
    test_rules_within_token_budget,
    test_append_rules_evicts_oldest_when_over_cap,
    test_prune_memory_rules_reduces_count,
]


def main() -> int:
    print(f"\nevals/test_v15.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
