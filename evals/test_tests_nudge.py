#!/usr/bin/env python3
"""evals/test_tests_nudge.py — the harness-side "run the tests" nudge.

Live tour 2026-09-12: "fix the median and run the tests" ended with the
model writing "the test script will now pass" and never running it (0/5
on tests-claim-1). More prompt prose made the 4B copy the tool examples
literally, so the fix is mechanical: when the request asks for the tests
and no run tool executed a test this turn, the finish is sent back once
with the test file named.

Usage:
    python evals/test_tests_nudge.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hexcli import agent as sa  # noqa: E402


def test_request_detection() -> None:
    yes = ["fix it and run the tests", "Run the unit tests", "rerun tests", "make the tests pass",
           "run pytest on this", "execute all tests please", "fix it so the tests pass"]
    no = ["what is a test", "test_processor.py is wrong, fix it", "write a test for median",
          "the tests are slow", "run processor.py"]
    for q in yes:
        assert sa._asks_to_run_tests(q), q
    for q in no:
        assert not sa._asks_to_run_tests(q), q


def test_ran_tests_recognises_test_files_and_runners_only() -> None:
    assert sa._ran_tests(["test_processor.py"])
    assert sa._ran_tests([r"C:\proj\tests\test_api.py"])
    assert sa._ran_tests(["python -m pytest -q"])
    assert sa._ran_tests(["python processor_test.py"])
    assert not sa._ran_tests(["processor.py"])                 # the module under repair
    assert not sa._ran_tests(["Get-Process | Sort CPU -Desc"])
    assert not sa._ran_tests([])


def test_nudge_names_the_test_file_when_one_exists() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert "find_files" in sa._tests_nudge_text(tmp)
        (Path(tmp) / "test_processor.py").write_text("assert True\n", encoding="utf-8")
        text = sa._tests_nudge_text(tmp)
        assert "Run test_processor.py with run_code" in text, text
        assert text.endswith("Respond with JSON only.")


def test_loop_sends_the_finish_back_once_when_tests_were_requested() -> None:
    """Drive run_autopilot with a scripted model: it edits, then finishes
    without running the tests; the loop must hand it the nudge and accept
    the second finish after the test run."""
    import json

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / "processor.py").write_text("def median(d):\n    return sorted(d)[len(d) // 2]\n",
                                           encoding="utf-8")
        (root / "test_processor.py").write_text("from processor import median\n"
                                                "assert median([1, 2, 5, 6]) == 3.5\n",
                                                encoding="utf-8")
        seen: list[str] = []
        replies = iter([
            json.dumps({"action": "finish", "message": "Fixed; the tests will now pass."}),
            json.dumps({"action": "run_code", "args": {"path": "test_processor.py"}}),
            json.dumps({"action": "finish", "message": "Ran the tests: exit 1 (median still wrong)."}),
        ])

        def fake_llm(config, messages, *args, **kw):
            seen.append(messages[-1]["content"])
            return next(replies), 0

        orig_cwd = Path.cwd()
        import os
        os.chdir(root)
        orig_call, orig_home = sa.call_llm, sa.tools._HOME
        sa.call_llm = fake_llm
        sa.tools._HOME = root
        try:
            cfg = {"require_verification": True, "max_agent_steps": 6, "prompt_split": False,
                   "live_streaming": False, "memory_enabled": False, "chat_log_enabled": False}
            out = sa.run_autopilot(cfg, [], "fix the median in processor.py and run the tests",
                                   "powershell.exe", session=None)
        finally:
            sa.call_llm, sa.tools._HOME = orig_call, orig_home
            os.chdir(orig_cwd)
        assert any("The tests were not run. Run test_processor.py" in m for m in seen), seen
        assert "Ran the tests" in out, out


TESTS = [
    test_request_detection,
    test_ran_tests_recognises_test_files_and_runners_only,
    test_nudge_names_the_test_file_when_one_exists,
    test_loop_sends_the_finish_back_once_when_tests_were_requested,
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
