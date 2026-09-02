#!/usr/bin/env python3
"""hexcli.config — defaults, load/merge, and the /config value tables,
lifted out of agent.py.

DEFAULT_CONFIG is re-bound in agent.py as the same dict object, so every
existing sa.DEFAULT_CONFIG reader (and {**sa.DEFAULT_CONFIG, ...} test
fixture) sees the canonical table.

Split stage 5 (docs/V2X_ROADMAP.md, "The Split"). Bodies moved verbatim.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from hexcli.tools import DEFAULT_TIMEOUT_SECONDS

DEFAULT_CONFIG: dict[str, Any] = {
    "backend": "ollama",
    "model": "qwen2.5-coder:7b",
    "temperature": 0.1,
    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    "max_output_tokens": 512,
    "autopilot_max_output_tokens": 2048,
    "compact_max_output_tokens": 512,
    "max_agent_steps": 15,
    "tool_output_limit": 12000,
    # Compiled Genie window. Per-step tool output is budgeted against it so a
    # single tool result can never overflow the window (an overflow returns
    # an EMPTY generation — measured 2026-09-01). Raise only with a bigger
    # bundle.
    "context_window_tokens": 3000,
    "history_retention_days": 30,
    "shell_exe": "",
    "use_streaming": True,
    # Render streamed answers live (text as it arrives, tool intent announced
    # early). Off = the old token-counter behaviour.
    "live_streaming": True,
    # Print a diff after every successful file mutation.
    "show_diffs": True,
    # Network policy for fetch_url, the agent's only outbound channel:
    # "ask" (default) confirms each fetch and denies when non-interactive;
    # "allow" fetches silently; "deny" disables the tool and drops its schema.
    "network_access": "ask",
    # Omit the procedural rules (13/14) when the query cannot trigger them.
    # OFF by default on measured evidence: it saves ~330 prompt tokens and 16%
    # of first-token latency, but extended trap-4 went 5/8 -> 3/18 across three
    # independent A/B runs (Fisher p~=0.017). See docs/V2_PLAN.md §14.15.
    # Opt in only with a bigger-context bundle or a different model.
    "conditional_rules": False,
    "prompt_split": True,
    # Byte-stable system prompt (date/cwd move to the first user message).
    # Precondition for KV prefix reuse; default flips after the A/B.
    "prompt_stable_prefix": False,
    # Rich input line: persistent history, Tab completion, multi-line paste.
    # Falls back to bare input() automatically when stdin/stdout is not a tty.
    "rich_input": True,
    "input_history_file": "",
    "input_history_limit": 500,
    # After an unverified file mutation, deflect the first "done" once and ask
    # the agent to check its work. (Was read from config but declared nowhere,
    # so `/config require_verification false` reported an unknown key.)
    "require_verification": True,
    # Confine file MUTATIONS to the working directory (reads stay free).
    "workspace_write_scope": True,
    # Extra roots the agent may write to (absolute paths, ~ expanded).
    "workspace_write_allow": [],
    "telemetry_enabled": True,
    "memory_enabled": True,
    # The dreaming consolidation daemon is OFF by default: measured 2026-08-16
    # writing the same five fabricated machine "facts" (wrong CPU, wrong RAM,
    # an invented temperature) into memory_rules.md every idle cycle, which
    # workspace_snapshot then injected as "Prior knowledge" — locking the
    # model's hardware confabulations in permanently. V2X_ROADMAP already
    # ruled it ships only with a quality eval; the eval now exists and it
    # failed it. Re-enable only with new evidence.
    "memory_dreaming": False,
    "autopilot_confirm_destructive": True,
    # Sensitive-data command gate (ssh keys, credential stores, security
    # files, obfuscated execution). Separate from the destructive flag so
    # injection defense holds even when destructive confirms are disabled.
    "autopilot_confirm_sensitive": True,
    # Agent protocol: "v1" (JSON action loop) or "v2" (native tool-call format,
    # payload-block edits, persistent shell — see docs/V2_PLAN.md §5).
    "protocol": "v1",
    # Auto-compact is deterministic (no LLM call) by default: summarising via
    # the same model that is already at its context cliff produced unverified
    # summaries and cost a full extra re-prefill. Set true to restore the
    # LLM summariser for auto-compact; explicit /compact always uses it.
    "auto_compact_uses_llm": False,
    # Override the derived history budget (tokens). Empty = derive from the
    # measured system-prompt size.
    "context_warn_tokens": 0,
    # Local escalation ladder (docs/V2_PLAN.md §4): name of a bigger local
    # npurun model to consult at hard moments (loop trips, ignored
    # verification, prose-instead-of-edit). Empty = disabled. The server is
    # spawned lazily on the bind address below and reused for the session.
    "escalation_local_model": "",
    "escalation_local_bind": "127.0.0.1:11436",
    "escalation_max_output_tokens": 900,
    "escalation_timeout_seconds": 240,
    "ollama": {"host": "http://127.0.0.1:11434"},
    "openai_compatible": {
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "local",
    },
    "anthropic_api_key": "",
    "escalation_model": "claude-haiku-4-5-20251001",
}


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged: dict[str, Any] = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def ensure_default_config(path: Path) -> None:
    if not path.exists():
        payload = json.dumps(DEFAULT_CONFIG, indent=2) + "\n"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(payload, encoding="utf-8")
        tmp.replace(path)


def load_config(path: Path) -> dict[str, Any]:
    ensure_default_config(path)
    with path.open("r", encoding="utf-8") as fh:
        data = json.load(fh)
    config = deep_merge(DEFAULT_CONFIG, data)
    # Per-project override: .shellai/config.json in cwd deep-merges on top.
    project_cfg = Path.cwd() / ".shellai" / "config.json"
    if project_cfg != path and project_cfg.exists():
        try:
            with project_cfg.open("r", encoding="utf-8") as fh:
                project_data = json.load(fh)
            config = deep_merge(config, project_data)
        except Exception:
            pass
    return config


_CONFIG_SETTABLE: dict[str, str] = {
    "model":                          "str",
    "temperature":                    "float",
    "timeout_seconds":                "int",
    "max_output_tokens":              "int",
    "autopilot_max_output_tokens":    "int",
    "compact_max_output_tokens":      "int",
    "max_agent_steps":                "int",
    "tool_output_limit":              "int",
    "context_window_tokens":          "int",
    "history_retention_days":         "int",
    "use_streaming":                  "bool",
    "live_streaming":                 "bool",
    "workspace_write_scope":          "bool",
    "workspace_write_allow":          "list",
    "require_verification":           "bool",
    "show_diffs":                     "bool",
    "conditional_rules":              "bool",
    "prompt_split":                   "bool",
    "prompt_stable_prefix":           "bool",
    "network_access":                 "str",
    "rich_input":                     "bool",
    "input_history_file":             "str",
    "input_history_limit":            "int",
    "telemetry_enabled":              "bool",
    "memory_enabled":                 "bool",
    "memory_dreaming":                "bool",
    "autopilot_confirm_destructive":  "bool",
    "autopilot_confirm_sensitive":    "bool",
    "protocol":                       "str",
    "auto_compact_uses_llm":          "bool",
    "context_warn_tokens":            "int",
    "escalation_local_model":         "str",
    "escalation_local_bind":          "str",
    "escalation_max_output_tokens":   "int",
    "escalation_timeout_seconds":     "int",
    "anthropic_api_key":              "str",
    "escalation_model":               "str",
}


def _coerce_config_value(value: str, kind: str) -> Any:
    if kind == "bool":
        return value.lower() in ("1", "true", "yes", "on")
    if kind == "int":
        return int(value)
    if kind == "float":
        return float(value)
    if kind == "list":
        # Comma- or semicolon-separated; "" clears. Without this, the one
        # setting the write-scope error message tells users to change
        # (workspace_write_allow) could not be changed from inside the tool.
        return [p.strip() for p in re.split(r"[;,]", value) if p.strip()]
    return value
