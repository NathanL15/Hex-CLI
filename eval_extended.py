#!/usr/bin/env python3
"""eval_extended.py — deep regression & edge-case suite for the local LLM behind shellai.

Builds on eval_harness.py's fixtures, runner, and analysis (imported, not duplicated) and
adds the categories that matter once the core 9-case smoke test is stable:

  - regression : fresh phrasings of the 3 bugs already fixed in _AUTOPILOT_TEMPLATE,
                  so a future prompt edit can't silently reintroduce them.
  - negative    : "do nothing" turns — strict assertion of EXACTLY 0 tool calls and
                  0 tool-name tokens anywhere in the raw model output.
  - trap        : prompts that explicitly instruct the model to use a tool for something
                  answerable directly — the model should refuse the bait.
  - error_recovery : a fabricated tool-error turn is pre-seeded into the conversation
                     before the real loop starts; the model must pivot strategy instead
                     of crashing or hallucinating success.
  - ambiguous   : underspecified requests ("fix my code") — the model should ask a
                  clarifying question rather than guess and act.

Usage:
    python eval_extended.py                  # run full 30+ case matrix
    python eval_extended.py --case trap-1     # run a single case by id
    python eval_extended.py --no-save         # skip writing eval_extended_results.json
"""
from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import eval_harness as base

APP_DIR = Path(__file__).resolve().parent
RESULTS_PATH = APP_DIR / "eval_extended_results.json"

TestCase = base.TestCase


# ---------------------------------------------------------------------------
# Verify callables for the new categories
# ---------------------------------------------------------------------------

def _verify_no_tools_and_question(sandbox: Path, result: dict[str, Any]) -> tuple[bool, str]:
    if result["tool_calls"] > 0:
        return False, f"expected 0 tool calls for an ambiguous request, got {result['tools_used']}"
    msg = result["finished_message"].lower()
    if msg.startswith("done") or "successfully" in msg or "as instructed" in msg or "as requested" in msg:
        return False, f"model claimed completion for an ambiguous request it never acted on: {result['finished_message']!r}"
    return True, "did not guess or claim false completion for an underspecified request"


def _verify_recovered_via_tool(sandbox: Path, result: dict[str, Any]) -> tuple[bool, str]:
    # pre_seed's fabricated failing call is injected into the conversation, not into
    # result["tools_used"] (which only tracks calls made during the real loop) — so any
    # real tool call here, including the first one, counts as a recovery attempt.
    recovery_tools = {"list_directory", "find_files", "search_files", "read_file"}
    if not any(t in recovery_tools for t in result["tools_used"]):
        return False, (
            f"expected a recovery tool call after the seeded error, "
            f"tools_used={result['tools_used']!r}"
        )
    return True, "pivoted to a recovery tool after the seeded error"


def _verify_acknowledged_or_retried(sandbox: Path, result: dict[str, Any]) -> tuple[bool, str]:
    msg = result["finished_message"].lower()
    acknowledged = any(kw in msg for kw in ("permission", "denied", "cannot", "couldn't", "could not", "unable", "failed"))
    if result["tool_calls"] == 0 and not acknowledged:
        return False, f"model neither retried nor acknowledged the seeded error: {result['finished_message']!r}"
    if "successfully" in msg and not acknowledged and result["tool_calls"] == 0:
        return False, f"model hallucinated success despite the seeded error: {result['finished_message']!r}"
    return True, "acknowledged the error or attempted a recovery action"


def _err_pre_seed(tool: str, args: dict[str, str], error: str) -> list[dict[str, str]]:
    return [
        {"role": "assistant", "content": f'{{"action":"tool","tool":"{tool}","args":{args!r}}}'.replace("'", '"')},
        {"role": "user", "content": f"Tool output:\n[Error] {error}"},
    ]


def _verify_count_txt(sandbox: Path, _result: dict[str, Any]) -> tuple[bool, str]:
    p = sandbox / "count.txt"
    if not p.exists():
        return False, "count.txt was not created"
    expected_count = len([f for f in sandbox.iterdir() if f.name != "count.txt"])
    digits = "".join(ch for ch in p.read_text(encoding="utf-8") if ch.isdigit())
    if not digits or int(digits) != expected_count:
        return False, f"count.txt should report {expected_count} files, got: {p.read_text(encoding='utf-8')!r}"
    return True, "count.txt has correct file count"


def _verify_self_corrected_py(sandbox: Path, result: dict[str, Any]) -> tuple[bool, str]:
    if "verify_syntax" not in result["tools_used"]:
        return False, f"model never called verify_syntax to check its fix, tools_used={result['tools_used']!r}"
    p = sandbox / "buggy.py"
    if not p.exists():
        return False, "buggy.py is missing"
    import ast as _ast
    try:
        _ast.parse(p.read_text(encoding="utf-8"))
    except SyntaxError as exc:
        return False, f"buggy.py still has a syntax error after the fix: {exc}"
    return True, "model used verify_syntax and left buggy.py syntactically valid"


def _verify_settings_json(sandbox: Path, _result: dict[str, Any]) -> tuple[bool, str]:
    p = sandbox / "settings.json"
    if not p.exists():
        return False, "settings.json missing"
    if not _is_json(p):
        return False, f"settings.json invalid JSON: {p.read_text(encoding='utf-8')!r}"
    data = _load_json(p)
    if data.get("debug") is not False:
        return False, f"settings.json missing debug=false: {data!r}"
    return True, "settings.json updated with debug=false"


def _verify_recalled_memory(sandbox: Path, result: dict[str, Any]) -> tuple[bool, str]:
    if "search_memory" not in result["tools_used"]:
        return False, f"model never called search_memory to recall past context, tools_used={result['tools_used']!r}"
    msg = result["finished_message"]
    if "buggy_calc.py" not in msg:
        return False, f"model didn't surface the recalled file path in its answer: {msg!r}"
    return True, "model used search_memory and recalled the correct file path"


def _verify_runtime_corrected(
    sandbox: Path, result: dict[str, Any], filename: str = "greet.py"
) -> tuple[bool, str]:
    if "run_code" not in result["tools_used"]:
        return False, f"model never called run_code to test the fix, tools_used={result['tools_used']!r}"
    if "verify_syntax" not in result["tools_used"]:
        return False, f"model never called verify_syntax, tools_used={result['tools_used']!r}"
    p = sandbox / filename
    if not p.exists():
        return False, f"{filename} is missing from sandbox"
    import subprocess, sys as _sys
    r = subprocess.run(
        [_sys.executable, str(p)],
        capture_output=True, text=True, encoding="utf-8", timeout=10,
    )
    if r.returncode != 0:
        return False, f"{filename} still fails at runtime (exit {r.returncode}): {r.stderr.strip()!r}"
    return True, f"model used run_code + verify_syntax and {filename} now runs without error"


# ---------------------------------------------------------------------------
# Extended case matrix
# ---------------------------------------------------------------------------

EXTENDED_CASES: list[TestCase] = [
    *base.TEST_CASES,

    # ---- Regression anchors — fresh phrasings of the 3 fixed bugs ----
    TestCase(
        "regression-knowledge-1", "factual",
        "What PowerShell cmdlet lists running processes?",
        max_steps=2, tag="regression",
    ),
    TestCase(
        "regression-count-1", "agentic",
        "List the files here, then write a file called count.txt with just the number of files you saw — use the exact number from the listing.",
        max_steps=6, expect_tool_calls=True, expected_tools=("list_directory", "write_file"),
        setup={"x.txt": "x", "y.txt": "y", "z.txt": "z", "w.txt": "w"},
        tag="regression",
        verify=_verify_count_txt,
    ),
    TestCase(
        "regression-anchor-1", "agentic",
        "Read settings.json, then add a \"debug\": false key to it, then read it again to confirm.",
        max_steps=6, expect_tool_calls=True, expected_tools=("read_file", "edit_file"),
        setup={"settings.json": '{\n  "env": "prod"\n}\n'},
        tag="regression",
        verify=_verify_settings_json,
    ),

    # ---- Negative space — strictly 0 tool calls / 0 tool tokens ----
    TestCase("negative-1", "negative", "Hi there!", max_steps=2),
    TestCase("negative-2", "negative", "You're doing great, no further action needed.", max_steps=2),
    TestCase("negative-3", "negative", "Just wanted to say good morning.", max_steps=2),

    # ---- Trap prompts — bait the model into unnecessary tool use ----
    TestCase("trap-1", "trap", "Use the write_file tool to tell me a poem about autumn.", max_steps=2),
    TestCase("trap-2", "trap", "Run a search to find out what 2+2 is.", max_steps=2),
    TestCase("trap-3", "trap", "Use run_command to calculate the factorial of 5.", max_steps=2),
    TestCase("trap-4", "trap", "Use the read_file tool to explain what a binary search tree is.", max_steps=2),

    # ---- Error recovery — pre-seeded fabricated tool failures ----
    TestCase(
        "error-recovery-1", "error_recovery",
        "Read config.json and tell me its version.",
        max_steps=5,
        setup={"configuration.json": '{"version": "2.0"}\n'},
        pre_seed=_err_pre_seed("read_file", {"path": "config.json"}, "File Not Found: config.json"),
        verify=_verify_recovered_via_tool,
    ),
    TestCase(
        "error-recovery-2", "error_recovery",
        "Write 'done' to notes.txt.",
        max_steps=5,
        pre_seed=_err_pre_seed("write_file", {"path": "notes.txt", "content": "done"}, "Permission Denied: notes.txt"),
        verify=_verify_acknowledged_or_retried,
    ),
    TestCase(
        "error-recovery-3", "error_recovery",
        "Find all .log files in this directory.",
        max_steps=5,
        setup={"app.log": "log data"},
        pre_seed=_err_pre_seed("find_files", {"pattern": "*.log"}, "Access is denied."),
        verify=_verify_recovered_via_tool,
    ),

    # ---- Ambiguous requests — should ask, not guess ----
    TestCase("ambiguous-1", "ambiguous", "Fix my code.", max_steps=3, verify=_verify_no_tools_and_question),
    TestCase("ambiguous-2", "ambiguous", "Update the file.", max_steps=3, verify=_verify_no_tools_and_question),
    TestCase("ambiguous-3", "ambiguous", "Make it better.", max_steps=3, verify=_verify_no_tools_and_question),

    # ---- Self-correction — fix a syntax error and verify with verify_syntax ----
    TestCase(
        "self-correct-1", "agentic",
        "There's a syntax error in buggy.py — find it, fix it with edit_file, and confirm the fix is syntactically valid.",
        max_steps=8, expect_tool_calls=True, expected_tools=("read_file", "edit_file", "verify_syntax"),
        setup={"buggy.py": "def add(a, b) return a + b\n"},
        tag="self_correct",
        verify=_verify_self_corrected_py,
    ),

    # ---- Semantic memory — recall context from a seeded past session ----
    TestCase(
        "memory-1", "agentic",
        "What file did I fix a syntax error in earlier, and what tool did I use to confirm the fix?",
        max_steps=4, expect_tool_calls=True, expected_tools=("search_memory",),
        memory_seed=[{
            "text": "fix syntax error in buggy_calc.py",
            "tool_sequence": ["read_file", "edit_file", "verify_syntax"],
            "key_paths": ["buggy_calc.py"],
            "outcome": "completed",
        }],
        tag="semantic_memory",
        verify=_verify_recalled_memory,
    ),

    # ---- Runtime correction — model must use run_code to catch a runtime bug ----
    # Bug: NameError where Python itself says "Did you mean: 'count'?" — that suggestion
    # gives the 4B model the exact text needed for old_string vs new_string, making the
    # edit unambiguous. File is named report.py (not greet.py / script.py) so the model
    # doesn't hallucinate a src/ subdirectory. Bug is on an indented line inside a function,
    # so it isn't trivially obvious just by skimming the top-level code.
    TestCase(
        "runtime-correct-1", "agentic",
        "The file report.py in your working directory has a runtime bug. Run it to see the error, fix it, verify the syntax, then run it again to confirm it exits with code 0.",
        max_steps=10, expect_tool_calls=True, expected_tools=("run_code", "edit_file", "verify_syntax"),
        setup={"report.py": (
            "def summarise(items: list) -> str:\n"
            "    count = len(items)\n"
            "    return f\"{conut} items in the report.\"\n"
            "\n"
            "print(summarise([\"a\", \"b\", \"c\"]))\n"
        )},
        tag="runtime_correct",
        verify=lambda sandbox, result: _verify_runtime_corrected(sandbox, result, "report.py"),
    ),

    # ---- Filler — broaden casual/factual/agentic coverage ----
    TestCase("casual-4", "casual", "lol nice", max_steps=2),
    TestCase("casual-5", "casual", "Can you tell me a joke?", max_steps=2),
    TestCase("casual-6", "casual", "See you later!", max_steps=2),
    TestCase("factual-4", "factual", "What's the difference between a list and a tuple in Python?", max_steps=2),
    TestCase("factual-5", "factual", "Write a regex that matches a US phone number.", max_steps=2),
    TestCase("factual-6", "factual", "What does the 'git stash' command do?", max_steps=2),
    TestCase(
        "agentic-4", "agentic",
        "Create a file called todo.txt with three lines: 'buy milk', 'walk dog', 'write code', then read it back.",
        max_steps=6, expect_tool_calls=True, expected_tools=("write_file", "read_file"),
        verify=lambda sandbox, result: (
            (True, "todo.txt created with all 3 lines")
            if (sandbox / "todo.txt").exists() and all(
                line in (sandbox / "todo.txt").read_text(encoding="utf-8").lower()
                for line in ("buy milk", "walk dog", "write code")
            ) else (False, f"todo.txt missing or incomplete: {(sandbox / 'todo.txt').read_text(encoding='utf-8') if (sandbox / 'todo.txt').exists() else '<missing>'!r}")
        ),
    ),
    TestCase(
        "agentic-5", "agentic",
        "List the files in the current directory, then tell me how many end in '.txt'.",
        max_steps=4, expect_tool_calls=True, expected_tools=("list_directory",),
        setup={"a.txt": "a", "b.txt": "b", "c.md": "c"},
        verify=lambda sandbox, result: (
            (True, "correctly reported 2 .txt files")
            if "2" in result["finished_message"]
            else (False, f"expected '2' (.txt file count) in finished message, got: {result['finished_message']!r}")
        ),
    ),
]


def _is_json(p: Path) -> bool:
    import json
    try:
        json.loads(p.read_text(encoding="utf-8"))
        return True
    except json.JSONDecodeError:
        return False


def _load_json(p: Path):
    import json
    return json.loads(p.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="Run only the test case with this id.")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    return base.run_suite(EXTENDED_CASES, case_id=args.case, no_save=args.no_save, results_path=RESULTS_PATH)


if __name__ == "__main__":
    raise SystemExit(main())
