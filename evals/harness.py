#!/usr/bin/env python3
"""evals/harness.py — 9-case E2E smoke test for Hex CLI.

Hits the configured OpenAI-compatible endpoint directly using the same
autopilot system prompt and JSON-action parsing hexcli.agent uses in
production, so results reflect real tool-routing behaviour. Drives a real
(sandboxed) tool-execution loop for agentic cases — file contents are
verified on disk, not trusted from the model's own claims.

Usage:
    python evals/harness.py                 # run full matrix, save + print report
    python evals/harness.py --case casual-1 # run a single case by id
    python evals/harness.py --no-save       # skip writing results file
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

APP_DIR = Path(__file__).resolve().parent.parent  # project root
sys.path.insert(0, str(APP_DIR))
import hexcli.agent as sa  # noqa: E402

CONFIG_PATH = APP_DIR / "shellai_npurun.json"
RESULTS_PATH = Path(__file__).resolve().parent / "results" / "harness_results.json"

# Thresholds used to flag latency problems in the analysis step.
CASUAL_LATENCY_BUDGET_S = 8.0
FACTUAL_LATENCY_BUDGET_S = 10.0
AGENTIC_TOTAL_LATENCY_BUDGET_S = 60.0


# ---------------------------------------------------------------------------
# Evaluation matrix
# ---------------------------------------------------------------------------

@dataclass
class TestCase:
    id: str
    category: str  # "casual" | "factual" | "agentic" | "trap" | "negative" | "ambiguous" | "error_recovery"
    query: str
    max_steps: int = 3
    expect_tool_calls: bool = False
    expected_tools: tuple[str, ...] = ()
    setup: dict[str, str] = field(default_factory=dict)   # files to seed in the sandbox
    verify: Any = None  # callable(sandbox: Path, result: dict) -> tuple[bool, str]
    tag: str | None = None  # optional reporting label, e.g. "regression" — defaults to category
    pre_seed: list[dict[str, str]] | None = None  # fabricated turns injected before the loop starts
    memory_seed: list[dict[str, Any]] | None = None  # entries pre-loaded into the vector store


def _verify_notes_txt(sandbox: Path, _result: dict[str, Any]) -> tuple[bool, str]:
    p = sandbox / "notes.txt"
    if not p.exists():
        return False, "notes.txt was not created"
    content = p.read_text(encoding="utf-8")
    if "hello world" not in content.lower():
        return False, f"notes.txt content wrong: {content!r}"
    return True, "notes.txt created with correct content"


def _verify_summary_txt(sandbox: Path, _result: dict[str, Any]) -> tuple[bool, str]:
    p = sandbox / "summary.txt"
    if not p.exists():
        return False, "summary.txt was not created"
    expected_count = len([f for f in sandbox.iterdir() if f.name != "summary.txt"])
    content = p.read_text(encoding="utf-8")
    digits = "".join(ch for ch in content if ch.isdigit())
    if not digits or int(digits) != expected_count:
        return False, f"summary.txt should report {expected_count} files, got: {content!r}"
    return True, "summary.txt has correct file count"


def _verify_config_json(sandbox: Path, _result: dict[str, Any]) -> tuple[bool, str]:
    p = sandbox / "config.json"
    if not p.exists():
        return False, "config.json missing"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return False, f"config.json is not valid JSON: {exc}"
    if data.get("version") != "1.0":
        return False, f"config.json missing version=1.0: {data!r}"
    return True, "config.json updated with version 1.0"


def _verify_lint_report(sandbox: Path, result: dict[str, Any]) -> tuple[bool, str]:
    if "lint_code" not in result["tools_used"]:
        return False, f"model never called lint_code, tools_used={result['tools_used']!r}"
    msg = result["finished_message"]
    if not any(kw in msg.lower() for kw in ("f401", "unused", "import", "os")):
        return False, f"model called lint_code but didn't surface unused-import finding: {msg!r}"
    return True, "model used lint_code and reported the unused-import issue"


TEST_CASES: list[TestCase] = [
    # ---- Casual / short — expect a direct response, 0 tool calls ----
    TestCase("casual-1", "casual", "Hey, how's it going?", max_steps=2),
    TestCase("casual-2", "casual", "Thanks, that's really helpful!", max_steps=2),
    TestCase("casual-3", "casual", "What's a fun fact about space?", max_steps=2),

    # ---- Precise / factual — expect a direct, correct answer, 0 tool calls ----
    TestCase(
        "factual-1", "factual",
        "What's the Python list comprehension to square the even numbers from 1 to 10?",
        max_steps=2,
    ),
    TestCase(
        "factual-2", "factual",
        "In PowerShell, what cmdlet returns the current date and time?",
        max_steps=2,
    ),
    TestCase(
        "factual-3", "factual",
        "What is the time complexity of binary search on a sorted array?",
        max_steps=2,
    ),

    # ---- Long-horizon / agentic — expect sequential, correctly-schemed tool calls ----
    TestCase(
        "agentic-1", "agentic",
        "Create a file called notes.txt containing the line 'hello world', then read it back to confirm it saved correctly.",
        max_steps=6, expect_tool_calls=True,
        expected_tools=("write_file", "read_file"),
        verify=_verify_notes_txt,
    ),
    TestCase(
        "agentic-2", "agentic",
        "List the files in the current directory, then write a file called summary.txt containing just the count of files you found.",
        max_steps=6, expect_tool_calls=True,
        expected_tools=("list_directory", "write_file"),
        setup={"a.txt": "a", "b.txt": "b", "c.txt": "c"},
        verify=_verify_summary_txt,
    ),
    TestCase(
        "agentic-3", "agentic",
        "Read config.json, then add a \"version\": \"1.0\" key to it, then read it again to confirm the change.",
        max_steps=6, expect_tool_calls=True,
        expected_tools=("read_file", "edit_file"),
        setup={"config.json": '{\n  "name": "demo"\n}\n'},
        verify=_verify_config_json,
    ),
    # ---- lint_code — only included when ruff is on PATH ----
    *(
        [TestCase(
            "lint-1", "agentic",
            "Run the linter on bad_imports.py and tell me what issues it found.",
            max_steps=4, expect_tool_calls=True, expected_tools=("lint_code",),
            setup={"bad_imports.py": "import os\nimport sys\n\nx = 1\n"},
            tag="lint",
            verify=_verify_lint_report,
        )]
        if shutil.which("ruff") else []
    ),
]


# ---------------------------------------------------------------------------
# Agentic loop driver — mirrors shellai.run_autopilot but fully instrumented
# ---------------------------------------------------------------------------

def run_case(config: dict[str, Any], case: TestCase) -> dict[str, Any]:
    cwd = str(Path.cwd())
    system_prompt = sa.build_autopilot_prompt(cwd=cwd, max_steps=case.max_steps)
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Working directory: {cwd}\n\nRequest: {case.query}"},
    ]
    if case.pre_seed:
        messages.extend(case.pre_seed)

    steps: list[dict[str, Any]] = []
    tools_used: list[str] = []
    finished_message = ""
    total_latency = 0.0

    for step in range(case.max_steps):
        # Mirrors hexcli.agent.run_autopilot's up-to-2-retries-on-malformed-JSON behaviour,
        # so the harness measures the same effective tool-routing the real CLI gets.
        raw = ""
        action: dict[str, Any] = {}
        latency = 0.0
        for attempt in range(3):
            t0 = time.perf_counter()
            try:
                raw = sa.openai_chat(config, messages, "autopilot_max_output_tokens")
            except Exception as exc:  # noqa: BLE001
                steps.append({"step": step, "error": str(exc), "latency_s": time.perf_counter() - t0})
                raw = ""
                break
            latency += time.perf_counter() - t0
            action = sa.parse_agent_action(raw)
            if (
                attempt < 2
                and step < 3
                and action["action"] == "finish"
                and any(name in raw for name in sa.TOOL_NAMES)
                and not sa.parse_json_object(raw)
            ):
                messages.append({"role": "assistant", "content": raw})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. "
                        "Respond with exactly one JSON object as specified. No prose."
                    ),
                })
                continue
            break
        if not raw:
            break
        total_latency += latency

        step_record: dict[str, Any] = {
            "step": step,
            "latency_s": round(latency, 3),
            "raw_response": raw,
            "action": action,
        }

        if action["action"] == "finish":
            finished_message = action.get("message", "")
            step_record["tool"] = None
            steps.append(step_record)
            break

        if action["action"] != "tool" or not action.get("tool"):
            finished_message = action.get("message", "") or raw
            step_record["tool"] = None
            steps.append(step_record)
            break

        tool_name = action["tool"]
        tools_used.append(tool_name)
        try:
            tool_output = sa.execute_tool_call(config, action, sa.detect_shell(""))
        except Exception as exc:  # noqa: BLE001
            tool_output = f"Error: {exc}"
        step_record["tool"] = tool_name
        step_record["tool_args"] = action.get("args", {})
        step_record["tool_output"] = tool_output[:2000]
        steps.append(step_record)

        messages.append({"role": "assistant", "content": raw})
        messages.append({"role": "user", "content": f"Tool output:\n{sa.trim_text(tool_output, 12000)}"})
    else:
        finished_message = finished_message or "(hit step limit without finishing)"

    return {
        "id": case.id,
        "category": case.category,
        "query": case.query,
        "steps": steps,
        "tool_calls": len(tools_used),
        "tools_used": tools_used,
        "total_latency_s": round(total_latency, 3),
        "first_latency_s": round(steps[0]["latency_s"], 3) if steps else None,
        "finished_message": finished_message,
    }


def run_case_sandboxed(config: dict[str, Any], case: TestCase) -> dict[str, Any]:
    prev_cwd = Path.cwd()
    with tempfile.TemporaryDirectory(prefix="shellai_eval_") as tmp:
        sandbox = Path(tmp)
        for name, content in case.setup.items():
            (sandbox / name).write_text(content, encoding="utf-8")
        os.chdir(sandbox)
        try:
            if case.memory_seed:
                from hexcli import memory as mem
                store = mem.VectorStore(config)
                for entry in case.memory_seed:
                    entry = dict(entry)
                    text = entry.pop("text")
                    store.add(text, entry)
            result = run_case(config, case)
            result["tag"] = case.tag or case.category
            result["expected_tools"] = list(case.expected_tools)
            if case.verify:
                ok, detail = case.verify(sandbox, result)
                result["verified"] = ok
                result["verify_detail"] = detail
        finally:
            os.chdir(prev_cwd)
    return result


# ---------------------------------------------------------------------------
# Analysis
# ---------------------------------------------------------------------------

def analyze(results: list[dict[str, Any]]) -> list[str]:
    findings: list[str] = []
    for r in results:
        cat, cid = r["category"], r["id"]

        if cat in ("casual", "factual", "trap") and r["tool_calls"] > 0:
            findings.append(
                f"[OVER-TOOLING] {cid}: called {r['tool_calls']} tool(s) "
                f"{r['tools_used']} for a {cat} query that should answer directly."
            )
        budget = CASUAL_LATENCY_BUDGET_S if cat in ("casual", "trap", "negative") else FACTUAL_LATENCY_BUDGET_S
        if cat in ("casual", "factual", "trap", "negative") and (r["first_latency_s"] or 0) > budget:
            findings.append(
                f"[LATENCY] {cid}: first response took {r['first_latency_s']}s "
                f"(budget {budget}s) for a {cat} query."
            )

        if cat == "negative":
            tool_tokens_leaked = any(
                name in step.get("raw_response", "") for name in sa.TOOL_NAMES for step in r["steps"]
            )
            if r["tool_calls"] != 0 or tool_tokens_leaked:
                findings.append(
                    f"[NEGATIVE-SPACE] {cid}: expected EXACTLY 0 tool calls/tokens, "
                    f"got {r['tool_calls']} tool call(s) "
                    f"{'and tool-name tokens leaked into raw output' if tool_tokens_leaked else ''}."
                )

        if cat == "agentic":
            if r["tool_calls"] == 0:
                findings.append(
                    f"[UNDER-ITERATING] {cid}: made 0 tool calls for an agentic task — "
                    f"likely answered without doing the work."
                )
            missing = [t for t in r.get("expected_tools", ()) if t not in r["tools_used"]]
            if missing:
                findings.append(f"[SCHEMA] {cid}: never called expected tool(s) {missing}.")
            if r["total_latency_s"] > AGENTIC_TOTAL_LATENCY_BUDGET_S:
                findings.append(
                    f"[LATENCY] {cid}: total agentic latency {r['total_latency_s']}s "
                    f"exceeds budget {AGENTIC_TOTAL_LATENCY_BUDGET_S}s."
                )

        if cat == "error_recovery" and r["tool_calls"] == 0 and "verified" not in r:
            findings.append(
                f"[NO-RECOVERY] {cid}: model never attempted a recovery tool call after the seeded error."
            )

        if cat == "ambiguous" and r["tool_calls"] > 0 and "verified" not in r:
            findings.append(
                f"[BLIND-ACTION] {cid}: model called {r['tools_used']} instead of asking a "
                f"clarifying question for an ambiguous request."
            )

        if r.get("verified") is False:
            findings.append(f"[CORRECTNESS] {cid}: {r.get('verify_detail')}")

        for step in r["steps"]:
            if step.get("action", {}).get("action") == "finish" and not step.get("action", {}).get("message") and cat not in ("agentic",):
                findings.append(f"[EMPTY] {cid}: model returned an empty finish message.")
    return findings


def print_report(results: list[dict[str, Any]], findings: list[str]) -> None:
    print()
    print(f"{'ID':<14}{'Tag':<14}{'Category':<10}{'Tool calls':<12}{'1st lat(s)':<12}{'Total lat(s)':<14}{'Verified'}")
    print("-" * 90)
    for r in results:
        if "verified" in r:
            verified = "OK" if r.get("verified") else "FAIL"
        else:
            verified = "-"
        print(
            f"{r['id']:<14}{r.get('tag', r['category']):<14}{r['category']:<10}{r['tool_calls']:<12}"
            f"{r['first_latency_s']:<12}{r['total_latency_s']:<14}{verified}"
        )
    print()
    if findings:
        print(f"FINDINGS ({len(findings)}):")
        for f in findings:
            print(f"  - {f}")
    else:
        print("No issues found — model is balanced across all categories.")
    print()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_suite(
    all_cases: list[TestCase],
    case_id: str | None = None,
    no_save: bool = False,
    results_path: Path = RESULTS_PATH,
) -> int:
    config = sa.load_config(CONFIG_PATH)
    cases = [c for c in all_cases if not case_id or c.id == case_id]
    if not cases:
        print(f"No test case with id '{case_id}'.")
        return 1

    results: list[dict[str, Any]] = []
    for case in cases:
        print(f"Running {case.id} ({case.category})...", file=sys.stderr)
        r = run_case_sandboxed(config, case)
        results.append(r)

    findings = analyze(results)
    print_report(results, findings)

    if not no_save:
        results_path.parent.mkdir(parents=True, exist_ok=True)
        results_path.write_text(
            json.dumps({"timestamp": time.time(), "model": config.get("model"), "results": results, "findings": findings}, indent=2),
            encoding="utf-8",
        )
        print(f"Saved: {results_path}")

    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--case", help="Run only the test case with this id.")
    parser.add_argument("--no-save", action="store_true")
    args = parser.parse_args()
    return run_suite(TEST_CASES, case_id=args.case, no_save=args.no_save)


if __name__ == "__main__":
    raise SystemExit(main())
