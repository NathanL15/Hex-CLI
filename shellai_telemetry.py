#!/usr/bin/env python3
"""shellai_telemetry — silent structured session logging for shellai.

One-way dependency, mirroring shellai_ui.py: shellai.py imports this module,
never the reverse. Every public method swallows its own exceptions — a
telemetry failure must never surface in the terminal UI or interrupt the
agent loop.
"""
from __future__ import annotations

import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_LOG_DIR_NAME = ".shellai/logs"
_REDACT_KEYS = {"content", "old_string", "new_string"}


def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in args.items():
        if key in _REDACT_KEYS and isinstance(value, str):
            out[key] = f"<{len(value)} chars>"
        else:
            out[key] = value
    return out


class TurnRecorder:
    """Accumulates tool calls and LLM latency for a single user turn."""

    def __init__(self, turn_index: int, mode: str, prompt: str) -> None:
        self.turn_index = turn_index
        self.mode = mode
        self.prompt = prompt
        self.timestamp = datetime.now(timezone.utc).isoformat()
        self.execution_path = "direct"
        self.tool_calls: list[dict[str, Any]] = []
        self.steps_used = 0
        self.thinking_latency_s = 0.0
        self.tokens_generated = 0
        self._start = time.monotonic()

    def record_llm(self, latency_s: float, tokens: int = 0) -> None:
        self.thinking_latency_s += latency_s
        self.tokens_generated += tokens
        self.steps_used += 1

    def record_tool(self, tool: str, args: dict[str, Any], latency_s: float, status: str) -> None:
        self.execution_path = "agentic"
        self.tool_calls.append({
            "tool": tool,
            "args_summary": _redact_args(args),
            "latency_s": round(latency_s, 3),
            "status": status,
        })

    def finish(self, status: str = "completed") -> dict[str, Any]:
        return {
            "turn_index": self.turn_index,
            "timestamp": self.timestamp,
            "mode": self.mode,
            "prompt": self.prompt,
            "execution_path": self.execution_path,
            "tool_calls": self.tool_calls,
            "steps_used": self.steps_used,
            "thinking_latency_s": round(self.thinking_latency_s, 3),
            "total_latency_s": round(time.monotonic() - self._start, 3),
            "tokens_generated": self.tokens_generated,
            "completion_status": status,
        }


class SessionTelemetry:
    """Writes one JSON file per process session to .shellai/logs/.

    Disabled (no-op) if config["telemetry_enabled"] is falsy, or if the log
    directory can't be created/written — in either case every method
    becomes a silent no-op rather than raising.
    """

    def __init__(self, config: dict[str, Any], cwd: str | None = None) -> None:
        self.enabled = bool(config.get("telemetry_enabled", True))
        self.session_id = str(uuid.uuid4())
        self.started_at = datetime.now(timezone.utc).isoformat()
        self.model = str(config.get("model", "unknown"))
        self.backend = str(config.get("backend", "unknown"))
        self.cwd = cwd or str(Path.cwd())
        self.turns: list[dict[str, Any]] = []
        self._path: Path | None = None
        if self.enabled:
            try:
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                short_id = self.session_id[:8]
                log_dir = Path.cwd() / _LOG_DIR_NAME
                log_dir.mkdir(parents=True, exist_ok=True)
                self._path = log_dir / f"session_{stamp}_{short_id}.json"
            except Exception:
                self.enabled = False
                self._path = None

    def start_turn(self, mode: str, prompt: str) -> TurnRecorder:
        return TurnRecorder(len(self.turns), mode, prompt)

    def record_turn(self, recorder: TurnRecorder, status: str = "completed") -> None:
        if not self.enabled:
            return
        try:
            self.turns.append(recorder.finish(status))
            self._write()
        except Exception:
            self.enabled = False

    def _write(self) -> None:
        if not self._path:
            return
        payload = {
            "session_id": self.session_id,
            "started_at": self.started_at,
            "model": self.model,
            "backend": self.backend,
            "cwd": self.cwd,
            "turns": self.turns,
        }
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self._path)
