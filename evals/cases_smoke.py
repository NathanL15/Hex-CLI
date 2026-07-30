#!/usr/bin/env python3
"""evals/cases_smoke.py — Eval v2 smoke suite (fast merge gate).

Port of the v1 harness.py 9-case matrix onto the v2 instrument: every case now
drives the production run_autopilot and gets a strict binary verdict from
state + content + behavior assertions. Coverage is a superset of v1's:
  * casual/factual cases now assert answer CONTENT (v1 only counted tool calls)
  * factual-1 is graded by EXECUTING the model's code answer
  * agentic-3 additionally asserts the pre-existing JSON key survived the edit

Usage:
    python evals/cases_smoke.py                 # 1 run/case (fast gate)
    python evals/cases_smoke.py --runs 5        # stochastic pass^5 mode
    python evals/cases_smoke.py --case agentic-1
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import checks as ck  # noqa: E402
from evals.runner import Case, run_suite_cli  # noqa: E402


def _count_files_except(exclude: str):
    def _fn(sandbox: Path) -> int:
        return len([f for f in sandbox.iterdir()
                    if f.name != exclude and not f.name.startswith(".")])
    return _fn


SMOKE_CASES: list[Case] = [
    # ---- Casual — direct answer, no tools, non-trivial reply ----
    Case("casual-1", "casual", "Hey, how's it going?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty()), max_steps=2),
    Case("casual-2", "casual", "Thanks, that's really helpful!",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=5)), max_steps=2),
    Case("casual-3", "casual", "What's a fun fact about space?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=30)), max_steps=2),

    # ---- Factual — direct, CORRECT answer, no tools ----
    Case("factual-1", "factual",
         "What's the Python list comprehension to square the even numbers from 1 to 10?",
         verify=ck.all_of(
             ck.no_tool_calls(),
             ck.python_expression_equals([4, 16, 36, 64, 100]),
         ), max_steps=2),
    Case("factual-2", "factual",
         "In PowerShell, what cmdlet returns the current date and time?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains("get-date")), max_steps=2),
    Case("factual-3", "factual",
         "What is the time complexity of binary search on a sorted array?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains_any("o(log", "log n", "logarithmic")),
         max_steps=2),

    # ---- Agentic — real tool sequences, graded on disk state ----
    Case("agentic-1", "agentic",
         "Create a file called notes.txt containing the line 'hello world', "
         "then read it back to confirm it saved correctly.",
         verify=ck.all_of(
             ck.file_contains("notes.txt", "hello world"),
             ck.tools_called("write_file", "read_file"),
         ),
         expected_tools=("write_file", "read_file")),
    Case("agentic-2", "agentic",
         "List the files in the current directory, then write a file called "
         "summary.txt containing just the count of files you found.",
         setup={"a.txt": "a", "b.txt": "b", "c.txt": "c"},
         verify=ck.all_of(
             ck.tools_called("list_directory"),
             ck.file_has_int("summary.txt", _count_files_except("summary.txt")),
         ),
         expected_tools=("list_directory", "write_file")),
    Case("agentic-3", "agentic",
         'Read config.json, then add a "version": "1.0" key to it, then read it '
         "again to confirm the change.",
         setup={"config.json": '{\n  "name": "demo"\n}\n'},
         verify=ck.all_of(
             ck.tools_called("read_file", "edit_file"),
             ck.json_file_expect("config.json", {"version": "1.0"}, preserved={"name": "demo"}),
         ),
         expected_tools=("read_file", "edit_file")),

    # ---- lint_code — only when ruff is on PATH (mirrors production gating) ----
    *(
        [Case("lint-1", "agentic",
              "Run the linter on bad_imports.py and tell me what issues it found.",
              setup={"bad_imports.py": "import os\nimport sys\n\nx = 1\n"},
              max_steps=4, tag="lint",
              verify=ck.all_of(
                  ck.tools_called("lint_code"),
                  ck.message_contains_any("f401", "unused", "import"),
              ),
              expected_tools=("lint_code",))]
        if shutil.which("ruff") else []
    ),
]


def main() -> int:
    return run_suite_cli("smoke_v2", cases=SMOKE_CASES, default_runs=1)


if __name__ == "__main__":
    raise SystemExit(main())
