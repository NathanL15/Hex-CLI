#!/usr/bin/env python3
"""evals/cases_extended.py — Eval v2 extended suite (adversarial + regression).

Port of the v1 extended.py 35-case matrix onto the v2 instrument, with the
grading defects from the v1.7.0 audit fixed:

  * error_recovery fixtures are REAL — the sandbox actually enforces the
    failure (missing file, read-only ACL), instead of fabricated error strings
    the sandbox contradicted. Recovery is graded on OUTCOME (correct answer /
    correct state), not on "any tool call counts".
  * trap cases assert the CORRECT direct answer (120, 4, ...), not just the
    absence of tools.
  * ambiguous cases require an actual clarifying question, not merely
    "didn't say done".
  * negative cases keep the strict raw-token assertion.
  * memory-1 seeds are verified on disk; a failed seed marks the run INVALID
    rather than failing the model.
  * count checks match whole integer tokens — no digit concatenation.

Includes the smoke suite (superset relationship preserved from v1).

Usage:
    python evals/cases_extended.py                 # 1 run/case
    python evals/cases_extended.py --runs 5        # pass^5 mode
    python evals/cases_extended.py --case trap-3
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import checks as ck  # noqa: E402
from evals.cases_smoke import SMOKE_CASES, _count_files_except  # noqa: E402
from evals.runner import (  # noqa: E402
    Case,
    InvalidRun,
    load_live_config,
    run_suite_cli,
    seed_memory,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _readonly_notes(sandbox: Path) -> None:
    p = sandbox / "notes.txt"
    p.write_text("original content", encoding="utf-8")
    os.chmod(p, stat.S_IREAD)


def _seed_buggy_calc_memory(sandbox: Path) -> None:
    ok = seed_memory(sandbox, [{
        "text": "fix syntax error in buggy_calc.py",
        "tool_sequence": ["read_file", "edit_file", "verify_syntax"],
        "key_paths": ["buggy_calc.py"],
        "outcome": "completed",
    }], load_live_config())
    if not ok:
        raise InvalidRun("memory seed did not land on disk (embedder unavailable?)")


def _err_history(tool: str, args: dict[str, str], error: str) -> list[dict[str, str]]:
    """A prior failed attempt, injected as conversation history. Unlike v1,
    the referenced failure is REAL in the sandbox — a blind identical retry
    genuinely fails again."""
    import json
    return [
        {"role": "assistant", "content": json.dumps({"action": "tool", "tool": tool, "args": args})},
        {"role": "user", "content": f"Tool output:\n[Error] {error}"},
    ]


# ---------------------------------------------------------------------------
# Case matrix
# ---------------------------------------------------------------------------

EXTENSION_CASES: list[Case] = [
    # ---- Regression anchors — fresh phrasings of the 3 originally-fixed bugs ----
    Case("regression-knowledge-1", "factual",
         "What PowerShell cmdlet lists running processes?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains("get-process")),
         max_steps=2, tag="regression"),
    Case("regression-count-1", "agentic",
         "List the files here, then write a file called count.txt with just the "
         "number of files you saw — use the exact number from the listing.",
         setup={"x.txt": "x", "y.txt": "y", "z.txt": "z", "w.txt": "w"},
         verify=ck.all_of(
             ck.used_capability("list"),
             ck.file_has_int("count.txt", _count_files_except("count.txt")),
         ),
         tag="regression", expected_tools=("list_directory", "write_file")),
    Case("regression-anchor-1", "agentic",
         'Read settings.json, then add a "debug": false key to it, then read it '
         "again to confirm.",
         setup={"settings.json": '{\n  "env": "prod"\n}\n'},
         verify=ck.all_of(
             ck.used_capability("read", "edit"),
             ck.json_file_expect("settings.json", {"debug": False}, preserved={"env": "prod"}),
         ),
         tag="regression", expected_tools=("read_file", "edit_file")),

    # ---- Negative space — strictly 0 tool calls AND 0 tool tokens in raw output ----
    Case("negative-1", "negative", "Hi there!",
         verify=ck.all_of(ck.no_tool_calls(), ck.no_tool_tokens_in_raw(), ck.message_nonempty(min_len=2)),
         max_steps=2),
    Case("negative-2", "negative", "You're doing great, no further action needed.",
         verify=ck.all_of(ck.no_tool_calls(), ck.no_tool_tokens_in_raw(), ck.message_nonempty(min_len=2)),
         max_steps=2),
    Case("negative-3", "negative", "Just wanted to say good morning.",
         verify=ck.all_of(ck.no_tool_calls(), ck.no_tool_tokens_in_raw(), ck.message_nonempty(min_len=2)),
         max_steps=2),

    # ---- Traps — named-tool bait; must refuse the bait AND answer correctly ----
    Case("trap-1", "trap", "Use the write_file tool to tell me a poem about autumn.",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=40), ck.file_absent("poem.txt")),
         max_steps=2),
    Case("trap-2", "trap", "Run a search to find out what 2+2 is.",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_has_int(4)), max_steps=2),
    Case("trap-3", "trap", "Use run_command to calculate the factorial of 5.",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_has_int(120)), max_steps=2),
    Case("trap-4", "trap", "Use the read_file tool to explain what a binary search tree is.",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains_any("node", "left", "sorted", "ordered")),
         max_steps=2),

    # ---- Live machine state — rule 9's other direction ----
    # Found live 2026-08-14: "what cpu do i have" was answered 3/3 with a
    # confabulated "Intel Core i7-13700K" (this machine is a Snapdragon
    # X Elite) and zero tool calls. The trap cases measure resisting a named
    # tool; nothing measured the opposite obligation — a hardware question
    # MUST hit the machine. Graded on behaviour (a command actually ran)
    # and content (names the real silicon, not the confabulation).
    Case("livestate-1", "livestate", "What CPU does this machine actually have?",
         verify=ck.all_of(
             ck.tools_called("run_command"),
             ck.regex_answer_matches(
                 [r"snapdragon|oryon|qualcomm|arm|x1e"],
                 [r"intel|ryzen|core i[3579]"],
             ),
         ),
         expected_tools=("run_command",)),

    # ---- Multi-step arithmetic — the gap between rule 4 and a 4B ----
    # Same session: 104k at 37.5 h/week came back as "$50/hr" 3/3 (the model
    # pattern-matches the standard 2,080-hour year) and "$520" in the wild.
    # Correct: 104000 / (52 * 37.5) = 53.33. Graded on the answer alone —
    # a direct correct answer and a run_code-computed one both pass.
    # CORRECTION (2026-08-16): the original 0/5 baseline was measured against
    # a 27-hour-old server — the degrading-backend trap biting the diagnosis
    # itself. Fresh-server baseline is ~2/3; this case is FLAKY, not dead.
    # Full study: evals/cases_everyday.py and docs/V2X_ROADMAP notes.
    Case("numeric-1", "numeric",
         "What is a 104k annual salary in hourly, working 37.5 hours per week?",
         verify=ck.message_contains_any("53.3", "$53", "53.33")),

    # ---- Error recovery — REAL failures; graded on outcome ----
    Case("error-recovery-1", "error_recovery",
         "Read config.json and tell me its version.",
         setup={"configuration.json": '{"version": "2.0"}\n'},
         history=_err_history("read_file", {"path": "config.json"}, "File Not Found: config.json"),
         # config.json genuinely does not exist; the correct outcome is finding
         # configuration.json and reporting its version.
         verify=ck.message_contains("2.0"),
         max_steps=5),
    Case("error-recovery-2", "error_recovery",
         "Write 'done' to notes.txt.",
         setup_fn=_readonly_notes,
         history=_err_history("write_file", {"path": "notes.txt", "content": "done"},
                              "Permission Denied: notes.txt"),
         # Two legitimate outcomes: genuinely recover (clear the read-only bit
         # and write), or truthfully report the failure. Hallucinated success fails.
         verify=ck.any_of(
             ck.file_contains("notes.txt", "done"),
             ck.acknowledges_failure(),
         ),
         max_steps=5),
    Case("error-recovery-3", "error_recovery",
         "Find all .log files in this directory.",
         setup={"app.log": "log data"},
         history=_err_history("find_files", {"pattern": "*.log"}, "Access is denied."),
         verify=ck.message_contains("app.log"),
         max_steps=5),

    # ---- Ambiguous — must actually ask a clarifying question ----
    Case("ambiguous-1", "ambiguous", "Fix my code.", verify=ck.asks_clarification(), max_steps=3),
    Case("ambiguous-2", "ambiguous", "Update the file.", verify=ck.asks_clarification(), max_steps=3),
    Case("ambiguous-3", "ambiguous", "Make it better.", verify=ck.asks_clarification(), max_steps=3),

    # ---- Self-correction ----
    Case("self-correct-1", "agentic",
         "There's a syntax error in buggy.py — find it, fix it with edit_file, "
         "and confirm the fix is syntactically valid.",
         setup={"buggy.py": "def add(a, b) return a + b\n"},
         verify=ck.all_of(
             ck.verified_after_mutation(),
             ck.python_file_valid("buggy.py"),
         ),
         max_steps=8, tag="self_correct",
         expected_tools=("read_file", "edit_file", "verify_syntax")),

    # ---- Semantic memory (seed verified on disk; invalid if embedder absent) ----
    Case("memory-1", "agentic",
         "What file did I fix a syntax error in earlier, and what tool did I use "
         "to confirm the fix?",
         setup_fn=_seed_buggy_calc_memory,
         requires_memory_seed=True,
         verify=ck.all_of(
             ck.used_capability("memory"),
             ck.message_contains("buggy_calc.py"),
         ),
         max_steps=4, tag="semantic_memory", expected_tools=("search_memory",)),

    # ---- Runtime correction ----
    Case("runtime-correct-1", "agentic",
         "The file report.py in your working directory has a runtime bug. Run it "
         "to see the error, fix it, verify the syntax, then run it again to "
         "confirm it exits with code 0.",
         setup={"report.py": (
             "def summarise(items: list) -> str:\n"
             "    count = len(items)\n"
             "    return f\"{conut} items in the report.\"\n"
             "\n"
             "print(summarise([\"a\", \"b\", \"c\"]))\n"
         )},
         verify=ck.all_of(
             ck.ran_file("report.py"),
             ck.verified_after_mutation(),
             ck.python_file_runs("report.py"),
         ),
         max_steps=10, tag="runtime_correct",
         expected_tools=("run_code", "edit_file", "verify_syntax")),

    # ---- Filler breadth ----
    Case("casual-4", "casual", "lol nice",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=2)), max_steps=2),
    Case("casual-5", "casual", "Can you tell me a joke?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=20)), max_steps=2),
    Case("casual-6", "casual", "See you later!",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_nonempty(min_len=2)), max_steps=2),
    Case("factual-4", "factual", "What's the difference between a list and a tuple in Python?",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains_any("immutable", "mutable")),
         max_steps=2),
    Case("factual-5", "factual", "Write a regex that matches a US phone number.",
         verify=ck.all_of(
             ck.no_tool_calls(),
             ck.regex_answer_matches(
                 positives=["555-123-4567", "(555) 123-4567", "5551234567", "555.123.4567"],
                 negatives=["banana"],
             ),
         ),
         max_steps=2),
    Case("factual-6", "factual", "What does the 'git stash' command do?",
         verify=ck.all_of(
             ck.no_tool_calls(),
             ck.message_contains_any("temporar", "shelve", "set aside", "without committing", "saves"),
         ),
         max_steps=2),
    Case("agentic-4", "agentic",
         "Create a file called todo.txt with three lines: 'buy milk', 'walk dog', "
         "'write code', then read it back.",
         verify=ck.all_of(
             ck.file_contains("todo.txt", "buy milk", "walk dog", "write code"),
             ck.used_capability("write", "read"),
         ),
         expected_tools=("write_file", "read_file")),
    Case("agentic-5", "agentic",
         "List the files in the current directory, then tell me how many end in '.txt'.",
         setup={"a.txt": "a", "b.txt": "b", "c.md": "c"},
         verify=ck.all_of(ck.used_capability("list"), ck.message_has_int(2)),
         max_steps=4, expected_tools=("list_directory",)),
]

EXTENDED_CASES: list[Case] = [*SMOKE_CASES, *EXTENSION_CASES]


def main() -> int:
    return run_suite_cli("extended_v2", cases=EXTENDED_CASES, default_runs=1)


if __name__ == "__main__":
    raise SystemExit(main())
