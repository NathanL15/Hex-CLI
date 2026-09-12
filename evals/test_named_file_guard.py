#!/usr/bin/env python3
"""evals/test_named_file_guard.py — the loop refuses to change a different
file when the one the request named does not exist.

Live tour 2026-09-12: "in the file missing.py, change alpha to beta" ended
with notes.txt edited and success reported (missing-file-2: 1/5). A rule
sentence fixed that case but broke agentic-3 in the gated set, so the
guard lives in the loop, where it cannot change what the model reads on
any other request.

Usage:
    python evals/test_named_file_guard.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli import agent as sa  # noqa: E402


def test_named_files_are_extracted_from_the_request() -> None:
    assert sa._named_files("in the file missing.py, change alpha to beta") == ["missing.py"]
    assert sa._named_files("rename spread in processor.py and test_processor.py") == ["processor.py", "test_processor.py"]
    assert sa._named_files("fix src/app.js and README.md.") == ["src/app.js", "README.md"]
    assert sa._named_files("what is 2+2") == []
    assert sa._named_files("version 2.5.0 is out") == []          # not a file


def test_guard_fires_only_for_a_mutation_aimed_elsewhere() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "notes.txt").write_text("alpha\n", encoding="utf-8")
        q = "In the file missing.py, change the word alpha to beta."
        # Editing another file in its place: refused, with what is here.
        msg = sa._named_file_guard(q, tmp, "edit_file", {"path": "notes.txt", "old_string": "alpha", "new_string": "beta"})
        assert msg and "missing.py does not exist" in msg and "notes.txt" in msg and "do not edit notes.txt" in msg, msg
        # Creating the missing file to "change" it: refused too.
        msg = sa._named_file_guard(q, tmp, "write_file", {"path": "missing.py", "content": "beta"})
        assert msg and "do not create missing.py" in msg, msg
        # Looking around is fine.
        assert sa._named_file_guard(q, tmp, "list_directory", {"path": "."}) is None
        assert sa._named_file_guard(q, tmp, "read_file", {"path": "notes.txt"}) is None
        # The named file exists: no guard.
        assert sa._named_file_guard("change alpha to beta in notes.txt", tmp, "edit_file", {"path": "notes.txt"}) is None
        # Two named files, one missing, the edit goes to the one that exists: allowed.
        assert sa._named_file_guard("update notes.txt and missing.py", tmp, "edit_file", {"path": "notes.txt"}) is None
        # No edit intent (a create request) never guards.
        assert sa._named_file_guard("create missing.py with a hello function", tmp, "write_file", {"path": "missing.py"}) is None
        # agentic-1's wording: "correct" inside "correctly" is not edit intent,
        # and a request to create the named file may create it.
        q1 = "Create a file called new.txt containing the line 'hello world', then read it back to confirm it saved correctly."
        assert sa._named_file_guard(q1, tmp, "write_file", {"path": "new.txt", "content": "hello world"}) is None
        assert sa._named_file_guard("create and then update report.md with a title", tmp, "write_file", {"path": "report.md"}) is None
        # ...but it still may not create a different file in its place.
        assert sa._named_file_guard("create report.md, then fix missing.py", tmp, "edit_file", {"path": "notes.txt"}) is not None
        # No file named: nothing to compare against.
        assert sa._named_file_guard("fix the bug", tmp, "edit_file", {"path": "notes.txt"}) is None


def test_loop_feeds_the_refusal_back_and_leaves_the_file_alone() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "notes.txt").write_text("alpha\n", encoding="utf-8")
        seen: list[str] = []
        replies = iter([
            json.dumps({"action": "edit_file", "args": {"path": "missing.py", "old_string": "alpha", "new_string": "beta"}}),
            json.dumps({"action": "edit_file", "args": {"path": "notes.txt", "old_string": "alpha", "new_string": "beta"}}),
            json.dumps({"action": "finish", "message": "missing.py was not found here; notes.txt is present. Which file did you mean?"}),
        ])

        def fake_llm(config, messages, *args, **kw):
            seen.append(messages[-1]["content"])
            return next(replies), 0

        orig_cwd = Path.cwd()
        os.chdir(root)
        orig_call, orig_home = sa.call_llm, sa.tools._HOME
        sa.call_llm = fake_llm
        sa.tools._HOME = root
        try:
            cfg = {"require_verification": False, "max_agent_steps": 6, "prompt_split": False,
                   "live_streaming": False, "memory_enabled": False, "chat_log_enabled": False}
            out = sa.run_autopilot(cfg, [], "In the file missing.py, change the word alpha to beta.",
                                   "powershell.exe", session=None)
        finally:
            sa.call_llm, sa.tools._HOME = orig_call, orig_home
            os.chdir(orig_cwd)
        assert any("do not edit notes.txt in its place" in m for m in seen), seen
        assert (root / "notes.txt").read_text(encoding="utf-8") == "alpha\n"
        assert not (root / "missing.py").exists()
        assert "not found" in out


TESTS = [
    test_named_files_are_extracted_from_the_request,
    test_guard_fires_only_for_a_mutation_aimed_elsewhere,
    test_loop_feeds_the_refusal_back_and_leaves_the_file_alone,
]


def main() -> int:
    failed = 0
    for t in TESTS:
        try:
            t()
            print(f"  PASS  {t.__name__}")
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"  FAIL  {t.__name__}: {exc}")
    total = len(TESTS)
    print(f"\n{total - failed}/{total} passed" + (" ✓" if not failed else f" — {failed} FAILED"))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
