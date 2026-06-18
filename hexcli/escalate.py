#!/usr/bin/env python3
"""hexcli.escalate — Cloud escalation for stuck agent turns.

Triggered by error-loop detection. Sends the last 6 turns and the
failing tool sequence to Anthropic's API after a redaction pass.

Transport: stdlib urllib.request — no SDK dependency.
Key source: ANTHROPIC_API_KEY env var, then config["anthropic_api_key"].
Default model: claude-haiku-4-5-20251001 (override via config["escalation_model"]).
"""
from __future__ import annotations

import copy
import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

_API_URL = "https://api.anthropic.com/v1/messages"
_ANTHROPIC_VERSION = "2023-06-01"
DEFAULT_ESCALATION_MODEL = "claude-haiku-4-5-20251001"
_MAX_FILE_CONTENT = 200  # chars — truncate long strings before sending

# ---------------------------------------------------------------------------
# Sensitive path prefixes — content following these in payload text is redacted.
# ---------------------------------------------------------------------------

_SENSITIVE_PATH_PREFIXES: list[str] = [
    str(Path.home() / ".ssh"),
    str(Path.home() / ".aws"),
    str(Path.home() / ".gnupg"),
    str(Path.home() / ".gpg"),
    # Generic tilde forms so tests can use them directly.
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.gpg",
]

# ---------------------------------------------------------------------------
# Redaction patterns: (compiled_pattern, replacement) applied in order.
# ---------------------------------------------------------------------------

_REDACT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # Anthropic / OpenAI API keys
    (re.compile(r"sk-[A-Za-z0-9\-_]{10,}"), "sk-***"),
    # Generic Bearer tokens
    (re.compile(r"Bearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***"),
    # password= in query strings / config
    (re.compile(r"password=[^\s&'\"\r\n<>]{1,200}", re.IGNORECASE), "password=***"),
    # api_key= and api-key=
    (re.compile(r"api[_\-]key=[^\s&'\"\r\n<>]{1,200}", re.IGNORECASE), "api_key=***"),
    # token= (word-boundary to avoid "multipart" style false positives)
    (re.compile(r"\btoken=[^\s&'\"\r\n<>]{1,200}", re.IGNORECASE), "token=***"),
    # Connection strings
    (
        re.compile(
            r"(postgresql|postgres|mongodb|mysql|redis|amqp|rabbitmq)://[^\s'\"\r\n<>{}[\]]{1,300}",
            re.IGNORECASE,
        ),
        r"\1://***",
    ),
]


def redact_text(text: str) -> str:
    """Apply all redaction rules to a string, in-place (returns new string)."""
    # Sensitive path content: if a sensitive path prefix appears, redact the
    # following _MAX_FILE_CONTENT chars (which is likely file content).
    for prefix in _SENSITIVE_PATH_PREFIXES:
        idx = text.lower().find(prefix.lower())
        while idx != -1:
            end = min(idx + len(prefix) + _MAX_FILE_CONTENT, len(text))
            text = text[:idx] + "[REDACTED:sensitive-path]" + text[end:]
            idx = text.lower().find(prefix.lower())

    for pattern, replacement in _REDACT_PATTERNS:
        text = pattern.sub(replacement, text)
    return text


def _redact_value(v: Any) -> Any:
    """Recursively redact strings in a JSON-like structure."""
    if isinstance(v, str):
        if len(v) > _MAX_FILE_CONTENT * 2:
            v = v[: _MAX_FILE_CONTENT] + "...[truncated]"
        return redact_text(v)
    if isinstance(v, dict):
        return {k: _redact_value(val) for k, val in v.items()}
    if isinstance(v, list):
        return [_redact_value(item) for item in v]
    return v


def redact_payload(turns: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deep-copy and redact all strings in a list of message dicts."""
    return [_redact_value(copy.deepcopy(t)) for t in turns]


# ---------------------------------------------------------------------------
# API key resolution
# ---------------------------------------------------------------------------

def get_api_key(config: dict[str, Any]) -> str | None:
    """Return the Anthropic API key from env or config; None if absent."""
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        key = str(config.get("anthropic_api_key", "") or "").strip()
    return key or None


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------

def _call_api(api_key: str, model: str, messages: list[dict[str, Any]]) -> str:
    payload = json.dumps({
        "model": model,
        "max_tokens": 1024,
        "messages": messages,
    }).encode()
    req = urllib.request.Request(
        _API_URL,
        data=payload,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": _ANTHROPIC_VERSION,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode())
    content = data.get("content", [])
    if content and isinstance(content[0], dict):
        return str(content[0].get("text", "")).strip()
    return str(data)


# ---------------------------------------------------------------------------
# Main escalation entry point
# ---------------------------------------------------------------------------

def escalate(
    config: dict[str, Any],
    turns: list[dict[str, Any]],
    tool_seq: list[str],
) -> str:
    """Send redacted context to Claude cloud and return the suggestion.

    Returns a user-visible message — either the LLM's suggestion or an
    explanation of why escalation is unavailable.
    """
    api_key = get_api_key(config)
    if not api_key:
        return "(set ANTHROPIC_API_KEY to enable cloud escalation)"

    model = str(config.get("escalation_model", DEFAULT_ESCALATION_MODEL)).strip()

    # Build the escalation prompt.
    parts: list[str] = [
        "The local agent is stuck in a repeated error loop.\n",
    ]
    if tool_seq:
        parts.append(f"Failing tool sequence: {', '.join(tool_seq)}\n")
    if turns:
        parts.append("\nLast session turns:\n")
        for t in turns[-6:]:
            role = t.get("role", "?")
            content = str(t.get("content", ""))[:500]
            parts.append(f"[{role}]: {content}\n")
    parts.append(
        "\nPlease provide a concise suggestion on how to resolve this loop "
        "and unblock the agent."
    )

    prompt = redact_text("".join(parts))
    messages = [{"role": "user", "content": prompt}]

    try:
        return _call_api(api_key, model, messages)
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = exc.read().decode()[:200]
        except Exception:
            pass
        return f"Cloud escalation failed (HTTP {exc.code}): {body}"
    except Exception as exc:
        return f"Cloud escalation failed: {exc}"
