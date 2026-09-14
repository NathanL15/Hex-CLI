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

_STRAY_QUOTE_MSGS = ("Expecting ',' delimiter", "Expecting ':' delimiter",
                     "Expecting property name")
_MAX_QUOTE_REPAIRS = 64
_DECODER = json.JSONDecoder(strict=False)   # raw newlines inside a written file are fine


def _escaped_at(text: str, i: int) -> bool:
    """Is the character at i preceded by an odd run of backslashes?"""
    n = 0
    while i - 1 - n >= 0 and text[i - 1 - n] == "\\":
        n += 1
    return n % 2 == 1


def _repair_stray_quote(text: str, err: json.JSONDecodeError) -> str | None:
    """Escape the quote that ended a string early. A 4B writing 1.6K of HTML
    inside a JSON string forgets the backslash on a closing attribute quote
    (`onclick=\\"input('7')">`, sixteen times in one reply, 2026-09-13): the
    string closes there and the decoder trips on the next token. The last
    unescaped quote before the error is that closer; escape it and let the
    caller decode again. A truncated string (\"Unterminated string\") is not
    repairable and is left alone."""
    if not any(err.msg.startswith(m) for m in _STRAY_QUOTE_MSGS):
        return None
    q = text.rfind('"', 0, err.pos)
    while q > 0 and _escaped_at(text, q):
        q = text.rfind('"', 0, q)
    if q <= 0:
        return None
    return text[:q] + "\\" + text[q:]


def _loads_object(text: str) -> dict[str, Any] | None:
    """The first JSON object in `text`, decoded from its first brace and
    ignoring whatever follows it (a second batched action, prose). Stray
    quotes inside string values are repaired a bounded number of times."""
    start = text.find("{")
    if start < 0:
        return None
    for _ in range(_MAX_QUOTE_REPAIRS + 1):
        try:
            parsed, _end = _DECODER.raw_decode(text, start)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError as err:
            fixed = _repair_stray_quote(text, err)
            if fixed is None:
                return None
            text = fixed
    return None


def describe_json_error(raw_text: str) -> str:
    """What is wrong with the first JSON object in a reply, for the retry
    feedback: the decoder's message, the character offset and the text
    around it. Empty when the object decodes."""
    text = strip_thinking(raw_text).strip()
    start = max(0, text.find("{"))
    try:
        _DECODER.raw_decode(text, start)
        return ""
    except json.JSONDecodeError as err:
        lo, hi = max(0, err.pos - 40), min(len(text), err.pos + 20)
        return f"{err.msg} at character {err.pos - start}, near: {text[lo:hi]!r}"


def parse_json_object(raw_text: str) -> dict[str, Any] | None:
    text = strip_thinking(raw_text).strip()
    if not text:
        return None
    # Direct parse
    parsed = _loads_object(text)
    if parsed is not None:
        return parsed
    # Strip markdown fences
    stripped = re.sub(r"^```[a-zA-Z]*\s*|```\s*$", "", text, flags=re.MULTILINE).strip()
    parsed = _loads_object(stripped)
    if parsed is not None:
        return parsed
    # Nothing decodable from the first brace. Batched actions ({edit}{verify}
    # {run}) are handled above: raw_decode takes the FIRST object and ignores
    # the rest, and the loop drives the next step (measured 2026-07-30: the
    # old greedy match discarded such replies and killed uc1-t4/t5/t6). A
    # broken first object is a malformed reply, never skipped for a later
    # one: on 2026-09-13 the finish behind an unparseable write_file was
    # accepted and the turn claimed a file it had not written. The loop
    # retries with the decoder's complaint (describe_json_error).
    return None


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
