#!/usr/bin/env python3
"""evals/test_prompt_split.py — Unit tests for the experimental prompt split.

The split (config "prompt_split", default OFF) adds two stages:
  * DIRECT — conservatively-routed knowledge queries get a small no-tools
    prompt; tool calls are refused harness-side.
  * CONTINUATION — agent-path steps >= 2 swap to a prompt without the step-1
    decision rules (4, 5, 9, 10, 12), freeing ~740 tokens of input room.

These tests pin: the flag defaults off and changes nothing when off; the
router is conservative (all measured-risk queries stay on the agent path);
the direct stage cannot execute tools; and the continuation prompt keeps the
remaining rule text byte-identical.

Usage:
    python evals/test_prompt_split.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402

_CFG: dict[str, Any] = {**sa.DEFAULT_CONFIG, "backend": "mock",
                        "memory_enabled": False, "telemetry_enabled": False}


# ---------------------------------------------------------------------------
# Router — conservative by construction
# ---------------------------------------------------------------------------

def test_router_accepts_knowledge_queries() -> None:
    for q in (
        "Hey, how's it going?",
        "Thanks, that's really helpful!",
        "What's a fun fact about space?",
        "What is the time complexity of binary search on a sorted array?",
        "Explain the difference between TCP and UDP.",
        "Tell me a joke about compilers.",
    ):
        assert sa._route_direct(q), f"should route direct: {q!r}"


def test_router_keeps_measured_risk_queries_on_agent_path() -> None:
    # Every query family the 2026-07-31 conditional-rules A/B or the
    # livestate study showed regressing must stay on the unchanged monolith.
    for q in (
        # traps: name a tool
        "Use the write_file tool to tell me a poem about autumn.",
        "Run a search to find out what 2+2 is.",
        "Use run_command to calculate the factorial of 5.",
        "Use the read_file tool to explain what a binary search tree is.",
        # livestate: question-shaped but MUST hit the machine
        "What CPU does this machine actually have?",
        "what time is it",
        "how much disk space is left?",
        # agentic
        "Create a file called notes.txt containing the line 'hello world'",
        "fix the bug in parser.py",
        'Read config.json, then add a "version": "1.0" key to it',
        # knowledge question, but mentions PowerShell machinery — stay safe
        "In PowerShell, what cmdlet returns the current date and time?",
        # ambiguous edit request (rule 12 territory)
        "fix my code",
    ):
        assert not sa._route_direct(q), f"must stay on agent path: {q!r}"


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------

def test_direct_prompt_offers_no_tools() -> None:
    p = sa.build_direct_prompt(cwd="C:/x")
    assert '"action":"finish"' in p
    for name in ("run_command", "write_file", "edit_file", "read_file",
                 "list_directory", "delegate", "TOOLS"):
        assert name not in p, f"direct prompt must not mention {name}"


def test_continuation_prompt_drops_decision_rules_keeps_rest_verbatim() -> None:
    full = sa.build_autopilot_prompt(cwd="C:/x", max_steps=15)
    lean = sa.build_autopilot_prompt(cwd="C:/x", max_steps=15,
                                     omit_rules=sa._CONTINUATION_OMIT_RULES)
    assert len(lean) < len(full) - 2000, (len(full), len(lean))
    # Dropped: the step-1 decision rules, by their distinctive wording.
    for marker in ("NEVER call a tool just because",       # rule 10
                   "AMBIGUOUS EDIT/FIX REQUESTS ONLY",      # rule 12
                   "Get-CimInstance Win32_Processor",       # rule 9
                   "Direct answers: general knowledge"):    # rule 4
        assert marker in full and marker not in lean, marker
    # Kept byte-identical: format, edit discipline, verification, tail.
    for n in sorted(set(sa._AUTOPILOT_RULES) - set(sa._CONTINUATION_OMIT_RULES)):
        body = sa._AUTOPILOT_RULES[n].format(max_steps=15)
        assert body in lean, f"rule {n} must survive verbatim"
    assert "TOOLS:" in lean and '"action":"run_command"' in lean


def test_flag_off_is_byte_identical_to_baseline() -> None:
    sa.set_active_config(dict(_CFG))
    try:
        base = sa.build_autopilot_prompt(cwd="C:/x", max_steps=15, query="hello")
        again = sa.build_autopilot_prompt(cwd="C:/x", max_steps=15, query="hello")
        assert base == again
        assert sa.DEFAULT_CONFIG["prompt_split"] is False
    finally:
        sa.set_active_config(None)


# ---------------------------------------------------------------------------
# Loop behaviour (mock backend)
# ---------------------------------------------------------------------------

def _capture_prompts(monkey_store: list[str]):
    orig = sa.call_llm

    def wrapper(config, messages, key, **kw):
        monkey_store.append(messages[0]["content"])
        return orig(config, messages, key, **kw)
    return orig, wrapper


def test_direct_stage_refuses_tool_calls() -> None:
    cfg = {**_CFG, "prompt_split": True}
    sa.set_mock_responses([
        '{"action":"write_file","args":{"path":"poem.txt","content":"x"}}',
        '{"action":"finish","message":"Here is a fun fact: honey never spoils."}',
    ])
    with tempfile.TemporaryDirectory() as tmp:
        old = Path.cwd()
        try:
            import os
            os.chdir(tmp)
            out = sa.run_autopilot(cfg, [], "What's a fun fact about space?", "powershell")
        finally:
            os.chdir(old)
        assert "honey" in out.lower()
        assert not (Path(tmp) / "poem.txt").exists(), \
            "direct stage executed a tool call"


def test_agent_path_swaps_to_continuation_prompt_at_step_two() -> None:
    cfg = {**_CFG, "prompt_split": True}
    seen: list[str] = []
    orig, wrapper = _capture_prompts(seen)
    sa.set_mock_responses([
        '{"action":"list_directory","args":{"path":"."}}',
        '{"action":"finish","message":"Two files present."}',
    ])
    sa.call_llm = wrapper
    try:
        with tempfile.TemporaryDirectory() as tmp:
            import os
            old = Path.cwd()
            try:
                os.chdir(tmp)
                sa.run_autopilot(cfg, [], "Create a file called notes.txt with a summary of this directory", "powershell")
            finally:
                os.chdir(old)
    finally:
        sa.call_llm = orig
    assert len(seen) >= 2, "expected two LLM calls"
    assert "NEVER call a tool just because" in seen[0], "step 1 must use the monolith"
    assert "NEVER call a tool just because" not in seen[1], \
        "step 2 must use the continuation prompt"
    assert '"action":"run_command"' in seen[1], "tools must survive in continuation"


def test_flag_off_never_swaps_prompts() -> None:
    cfg = {**_CFG, "prompt_split": False}
    seen: list[str] = []
    orig, wrapper = _capture_prompts(seen)
    sa.set_mock_responses([
        '{"action":"list_directory","args":{"path":"."}}',
        '{"action":"finish","message":"done"}',
    ])
    sa.call_llm = wrapper
    try:
        with tempfile.TemporaryDirectory() as tmp:
            import os
            old = Path.cwd()
            try:
                os.chdir(tmp)
                sa.run_autopilot(cfg, [], "Create a file called notes.txt with a summary of this directory", "powershell")
            finally:
                os.chdir(old)
    finally:
        sa.call_llm = orig
    assert len(seen) >= 2
    assert seen[0] == seen[1], "flag off must keep one prompt for every step"


TESTS = [
    test_router_accepts_knowledge_queries,
    test_router_keeps_measured_risk_queries_on_agent_path,
    test_direct_prompt_offers_no_tools,
    test_continuation_prompt_drops_decision_rules_keeps_rest_verbatim,
    test_flag_off_is_byte_identical_to_baseline,
    test_direct_stage_refuses_tool_calls,
    test_agent_path_swaps_to_continuation_prompt_at_step_two,
    test_flag_off_never_swaps_prompts,
]


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


def main() -> int:
    print(f"\nevals/test_prompt_split.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
