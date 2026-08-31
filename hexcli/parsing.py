#!/usr/bin/env python3
"""hexcli.parsing — the wire protocol's text side, lifted out of agent.py.

Everything here is pure text/JSON interpretation with no agent state: trim
helpers, the query normalizers, <think>-stripping, and the JSON-action parser
that turns a model response into a dispatchable action. TOOL_NAMES lives here
because it is the action vocabulary this parser interprets; agent.py re-binds
it (and everything else) by name, so `sa.parse_agent_action` and friends keep
working for every existing caller and eval.

Split stage 1 (docs/V2X_ROADMAP.md, "The Split"). Function bodies are moved
verbatim — behavior changes do not belong in split commits.
"""
from __future__ import annotations

import json
import re
import shutil
from typing import Any

# ruff is an optional hard dependency: if present, lint_code is registered as a
# live tool and injected into the system prompt. If absent, the tool simply does
# not appear — no fallback needed since verify_syntax covers the critical path.
_RUFF: str | None = shutil.which("ruff")

TOOL_NAMES = frozenset({
    "run_command", "read_file", "edit_file", "write_file",
    "append_file", "list_directory", "search_files", "find_files",
    "verify_syntax", "search_memory", "run_code",
    "fetch_url", "batch", "delegate",
    *(["lint_code"] if _RUFF else []),
})


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def trim_text(text: str, limit: int) -> str:
    """Head-only truncation. Use trim_tool_output for command/tool results."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated to {limit} chars]"


def trim_tool_output(text: str, limit: int) -> str:
    """Head+tail truncation for tool results.

    Command output carries its verdict at the END — exit summaries, stack
    traces, assertion messages. v1.7's head-only trim hid exactly the part
    the model needed to recover from a failure, so keep both ends.
    """
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = max(0, limit - head)
    omitted = len(text) - head - tail
    return f"{text[:head]}\n...[{omitted} chars omitted]...\n{text[-tail:]}" if tail else trim_text(text, limit)


def normalize_text(value: str) -> str:
    return re.sub(r"\s+", " ", value.strip().lower())


def is_help_request(query: str) -> bool:
    normalized = re.sub(r"[?!.,]+", "", normalize_text(query))
    return normalized in {"help", "/help", "what can you do", "what is this", "how do i use this"}


def is_small_talk(query: str) -> bool:
    normalized = re.sub(r"[?!.,]+", "", normalize_text(query))
    return normalized in {"hi", "hello", "hey", "whats up", "what is up", "yo"}


def local_meta_response(query: str, config: dict[str, Any]) -> str | None:
    normalized = re.sub(r"[?!.,]+", "", normalize_text(query))
    model = str(config.get("model", "unknown")).strip() or "unknown"
    backend = str(config.get("backend", "ollama")).strip()
    label = f"{model} via {'Ollama' if backend == 'ollama' else 'local OpenAI-compatible endpoint'}"
    if any(p in normalized for p in ("what model are you", "which model are you", "what llm")):
        return f"Using {label}."
    if normalized in {"who are you", "what are you"}:
        return f"Local coding and system agent powered by {label}."
    return None


def strip_thinking(text: str) -> str:
    """Remove <think>...</think> blocks emitted by reasoning models like deepseek-r1."""
    return re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_json_object(raw_text: str) -> dict[str, Any] | None:
    text = strip_thinking(raw_text).strip()
    if not text:
        return None
    # Direct parse
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Strip markdown fences
    stripped = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text, flags=re.MULTILINE).strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass
    # Extract the FIRST complete {...} object by brace balancing.
    #
    # This used to be a greedy re.search(r"\{.*\}"), which spans from the first
    # brace to the LAST one. When the model emits several actions in a row —
    # {"action":"edit_file",…},{"action":"verify_syntax",…},{"action":"run_code",…}
    # — that match is not valid JSON, so the whole response was discarded, the
    # identical retry was issued up to 3×, and the turn ended with no tool call
    # at all. Measured 2026-07-30: this is what actually killed uc1-t4/t5/t6
    # (0/3 each), NOT a context-length cliff. Batching is a natural response to
    # rules that prescribe an edit→verify→run sequence, so take the first
    # action and let the loop drive the rest.
    for candidate in _iter_json_objects(text):
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            continue
    return None


def _iter_json_objects(text: str):
    """Yield complete brace-balanced {...} substrings, in order.

    String-literal aware, so braces inside JSON strings never affect depth.
    """
    depth = 0
    start = -1
    in_string = False
    escaped = False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start >= 0:
                    yield text[start:i + 1]
                    start = -1


def parse_agent_action(raw_text: str) -> dict[str, Any]:
    parsed = parse_json_object(raw_text)
    if isinstance(parsed, dict):
        action = str(parsed.get("action", "")).strip().lower()
        args = parsed.get("args")
        args = args if isinstance(args, dict) else {}

        if action in TOOL_NAMES:
            return {"action": "tool", "tool": action, "args": args}
        if action == "tool":
            tool = str(parsed.get("tool", "")).strip()
            return {"action": "tool", "tool": tool, "args": args}
        if action == "finish":
            return {"action": "finish", "message": str(parsed.get("message") or "").strip()}

        tool = str(parsed.get("tool", "")).strip()
        if tool in TOOL_NAMES:
            return {"action": "tool", "tool": tool, "args": args}

        message = str(parsed.get("message") or "").strip()
        if message:
            # Valid JSON, but the action name (if any) matched nothing. Tag it
            # so the loop can distinguish "model typo'd a tool name" (worth a
            # retry naming the bad action) from an intended finish.
            return {"action": "finish", "message": message,
                    "fallback": "unknown-action", "bad_action": action}

    # No parseable JSON anywhere: the message is just the raw prose. This is
    # the deliberate direct-answer path for knowledge questions — but the loop
    # retries it when the text shows signs of an ATTEMPTED action (see
    # _looks_like_botched_action), because "prose instead of action" was the
    # uc1-t5/t6 failure mode.
    return {"action": "finish", "message": strip_thinking(raw_text).strip(),
            "fallback": "prose"}


def _looks_like_botched_action(raw_text: str) -> bool:
    """Does an unparseable response look like it TRIED to be an action?

    Braces or code fences mean attempted JSON; a tool name means attempted
    tool use. Pure prose with none of those is accepted as an implicit finish
    — that path is load-bearing for direct answers, so this must stay
    conservative about flagging it.
    """
    text = strip_thinking(raw_text)
    if "{" in text or "```" in text:
        return True
    return any(name in text for name in TOOL_NAMES)
