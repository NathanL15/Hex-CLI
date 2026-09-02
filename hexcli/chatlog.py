#!/usr/bin/env python3
"""hexcli.chatlog — full-detail transcript log, one JSONL file per session.

Telemetry (.shellai/logs) is a redacted summary: prompts cut at 500 chars,
edit contents replaced by their lengths. This log keeps everything, so a
session can be replayed and a failure understood after the fact:

  session_start   version, model, backend, server budget, npurun/QAIRT
                  versions, the config in force (secrets redacted)
  command         every slash command typed
  turn_start      the request as typed, history size, context gauge
  system_prompt   the system prompt text, once per distinct prompt (by hash)
  request         what the model was sent, per call: the messages added since
                  the previous call (the system prompt by reference)
  reply           the raw model reply, per call, with latency and retry index
  tool            each tool call: name, args, full output, latency, status
  turn_end        how the turn ended, the final message, duration
  compaction      history sizes before/after an auto-compact
  error           backend or loop failures the REPL caught

Local only, under ~/.shellai/chatlog/ (config chat_log_dir), off with
chat_log_enabled=false. One-way dependency like telemetry: hexcli.agent may
import this module, never the reverse. Every method swallows its own
exceptions — a logging failure must never reach the terminal or the loop.
Read it back with tools/chatlog_report.py.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_DEFAULT_DIR = Path.home() / ".shellai" / "chatlog"
_SECRET_MARKERS = ("key", "token", "secret", "password")


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            if any(m in str(k).lower() for m in _SECRET_MARKERS) and isinstance(v, str) and v:
                out[k] = "<redacted>"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8", "replace")).hexdigest()[:12]


def _npurun_version() -> str:
    exe = shutil.which("npurun") or str(Path.home() / ".cargo" / "bin" / "npurun.exe")
    try:
        out = subprocess.run([exe, "--version"], capture_output=True, text=True, timeout=5)
        return (out.stdout or out.stderr).strip()
    except Exception:
        return ""


def _server_info(config: dict[str, Any]) -> dict[str, Any]:
    """The budget and window the server advertises, when it is npurun."""
    if config.get("backend") != "openai":
        return {}
    try:
        from hexcli import http_client
        base = str(config["openai_compatible"]["base_url"]).rstrip("/")
        data = http_client.http_json_get(f"{base}/models", timeout_s=3)
        first = (data.get("data") or [{}])[0]
        return {k: first.get(k) for k in ("id", "context_size", "input_token_budget") if first.get(k) is not None}
    except Exception:
        return {}


class ChatLog:
    """Append-only JSONL writer for one process session."""

    def __init__(self, config: dict[str, Any], version: str = "", cwd: str | None = None,
                 kind: str = "repl") -> None:
        self.enabled = bool(config.get("chat_log_enabled", True))
        self.session_id = str(uuid.uuid4())
        self.path: Path | None = None
        self._system_prompts: set[str] = set()
        self._turn_started: float = 0.0
        if not self.enabled:
            return
        try:
            log_dir = Path(str(config.get("chat_log_dir") or "")).expanduser() if config.get("chat_log_dir") else _DEFAULT_DIR
            log_dir.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now().strftime("%Y-%m-%d_%H%M%S")
            self.path = log_dir / f"{stamp}_{self.session_id[:8]}.jsonl"
            self.event(
                "session_start",
                mode=kind,
                version=version,
                model=str(config.get("model", "")),
                backend=str(config.get("backend", "")),
                server=_server_info(config),
                npurun=_npurun_version(),
                qairt=os.environ.get("QNN_SDK_ROOT", ""),
                rewind_mode=os.environ.get("NPURUN_REWIND", ""),
                cwd=cwd or str(Path.cwd()),
                python=platform.python_version(),
                os=f"{platform.system()} {platform.release()}",
                config=_redact({k: v for k, v in config.items() if not str(k).startswith("_")}),
            )
        except Exception:
            self.enabled = False
            self.path = None

    # ------------------------------------------------------------------ core
    def event(self, kind: str, **fields: Any) -> None:
        if not self.enabled or self.path is None:
            return
        try:
            record = {"ts": _now(), "session": self.session_id, "kind": kind, **fields}
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception:
            self.enabled = False

    # --------------------------------------------------------------- events
    def command(self, text: str) -> None:
        self.event("command", text=text)

    def turn_start(self, index: int, query: str, history: list[dict[str, str]],
                   context_percent: int | None = None) -> TurnProbe:
        self._turn_started = time.monotonic()
        self.event(
            "turn_start", turn=index, query=query,
            history_messages=len(history),
            history_chars=sum(len(m.get("content", "")) for m in history),
            context_percent=context_percent,
        )
        return TurnProbe(self, index)

    def turn_end(self, index: int, status: str, message: str = "", kind: str = "") -> None:
        self.event(
            "turn_end", turn=index, status=status, end_kind=kind, message=message,
            duration_s=round(time.monotonic() - self._turn_started, 3) if self._turn_started else None,
        )

    def compaction(self, before: int, after: int, chars_before: int, chars_after: int) -> None:
        self.event("compaction", messages_before=before, messages_after=after,
                   chars_before=chars_before, chars_after=chars_after)

    def error(self, where: str, message: str) -> None:
        self.event("error", where=where, message=message)

    def _system_prompt(self, text: str) -> str:
        h = _hash(text)
        if h not in self._system_prompts:
            self._system_prompts.add(h)
            self.event("system_prompt", hash=h, chars=len(text), text=text)
        return h


class TurnProbe:
    """AutopilotProbe-shaped observer for one turn (duck-typed: hexcli.agent
    calls these through _probe(), which tolerates missing methods)."""

    def __init__(self, log: ChatLog, turn: int) -> None:
        self.log = log
        self.turn = turn
        self._seen = 0
        self._system_hash = ""

    def _encode(self, messages: list[dict[str, str]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for m in messages:
            if m.get("role") == "system":
                out.append({"role": "system", "ref": self.log._system_prompt(m.get("content", ""))})
            else:
                out.append({"role": m.get("role", ""), "content": m.get("content", "")})
        return out

    def on_start(self, system_prompt: str, messages: list[dict[str, str]]) -> None:
        self._system_hash = self.log._system_prompt(system_prompt)
        self._seen = 0

    def on_request(self, step: int, attempt: int, messages: list[dict[str, str]]) -> None:
        new = messages[self._seen:]
        self.log.event(
            "request", turn=self.turn, step=step, attempt=attempt,
            total_messages=len(messages),
            total_chars=sum(len(m.get("content", "")) for m in messages),
            new_messages=self._encode(new),
        )
        self._seen = len(messages)

    def on_llm(self, step: int, attempt: int, raw: str, latency_s: float) -> None:
        self.log.event("reply", turn=self.turn, step=step, attempt=attempt,
                       latency_s=round(latency_s, 3), empty=not raw.strip(), raw=raw)

    def on_tool(self, step: int, tool: str, args: dict[str, Any], output: str,
                latency_s: float, status: str) -> None:
        self.log.event("tool", turn=self.turn, step=step, tool=tool, args=args,
                       status=status, latency_s=round(latency_s, 3),
                       output_chars=len(output), output=output)

    def on_end(self, kind: str, message: str) -> None:
        self.log.event("turn_result", turn=self.turn, end_kind=kind, message=message)


def latest_log_path(config: dict[str, Any] | None = None) -> Path | None:
    """The most recent log file, for /stats and the report tool."""
    log_dir = Path(str((config or {}).get("chat_log_dir") or "")).expanduser() if (config or {}).get("chat_log_dir") else _DEFAULT_DIR
    try:
        files = sorted(log_dir.glob("*.jsonl"))
        return files[-1] if files else None
    except Exception:
        return None


if __name__ == "__main__":  # pragma: no cover
    print(latest_log_path() or "no chat log yet", file=sys.stderr)
