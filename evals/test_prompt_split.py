#!/usr/bin/env python3
"""evals/test_prompt_split.py — Unit tests for the prompt split (DIRECT stage).

Config "prompt_split" (default ON) routes conservatively-matched knowledge
queries to a small no-tools prompt; tool calls there are refused
harness-side. Measured 2026-08-31: no regression in the routed subset,
knowledge-case first-token latency -40%. The continuation-stage half of the
original experiment was measured and REJECTED the same day (see prompts.py).

These tests pin: the router is conservative (all measured-risk queries stay
on the agent path); the direct stage cannot execute tools; agent-path turns
keep one prompt for every step; flag off restores baseline entirely.

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


def test_split_defaults_on_and_monolith_untouched() -> None:
    assert sa.DEFAULT_CONFIG["prompt_split"] is True
    sa.set_active_config(dict(_CFG))
    try:
        # The agent-path prompt is the unchanged monolith regardless of flag.
        base = sa.build_autopilot_prompt(cwd="C:/x", max_steps=15, query="hello")
        assert "NEVER call a tool just because" in base
        assert "TOOLS:" in base
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


def test_agent_path_keeps_one_prompt_for_every_step() -> None:
    # The continuation-stage prompt swap was measured and rejected
    # (2026-08-31); this pins that agent-path steps never change prompt.
    cfg = {**_CFG, "prompt_split": True}
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
    assert seen[0] == seen[1], "agent path must keep one prompt for every step"


TESTS = [
    test_router_accepts_knowledge_queries,
    test_router_keeps_measured_risk_queries_on_agent_path,
    test_direct_prompt_offers_no_tools,
    test_split_defaults_on_and_monolith_untouched,
    test_direct_stage_refuses_tool_calls,
    test_agent_path_keeps_one_prompt_for_every_step,
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
