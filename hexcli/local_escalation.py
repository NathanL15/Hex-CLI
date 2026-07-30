#!/usr/bin/env python3
"""hexcli.local_escalation — consult a bigger LOCAL model at hard moments.

docs/V2_PLAN.md §4 escalation ladder, de-risked: instead of the (unavailable)
Qwen3-4B-Thinking-2507 self-compile, the precompiled qualcomm Qwen3-8B bundle
serves as the "senior engineer" — a hybrid-thinking model whose ~8-9 tok/s
decode is fine for rare consultations even though it would be too slow as the
main loop. Fully offline; the cloud path (hexcli.escalate) remains a separate,
opt-in, last resort.

The measured failure modes this targets (2026-07-30 instrument data):
  * loop-detector trips (model repeats a failing call and cannot adapt)
  * verification-gate nudges being ignored (finishes without checking work)
  * "prose instead of action" — the model narrates or gives up on an edit
    request without ever mutating a file (uc1-t5/t6, 0/3)

Design: one consult per turn max; every failure path degrades to the previous
behaviour (never crash the loop); the escalation server is spawned lazily on
first use and reused for the session.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

DETACHED_PROCESS = 0x00000008

_ESCALATION_SYSTEM = (
    "You are a senior software engineer advising a junior terminal agent that "
    "has gotten stuck. Think through the situation carefully, then give ONE "
    "concrete, specific next action: the exact command to run, the exact "
    "old/new text for a file edit, or the exact question to ask. Be brief and "
    "actionable — the junior agent will execute your advice literally."
)

# Verbs that signal the user asked for a file mutation; used by the
# prose-instead-of-action trigger.
_EDIT_INTENT_RE = re.compile(
    r"\b(add|fix|edit|update|change|modify|refactor|rename|insert|remove|"
    r"delete|guard|implement|patch|rewrite|append)\b", re.IGNORECASE)
_MUTATING_TOOLS = frozenset({"edit_file", "write_file", "append_file", "edit", "write"})


def looks_like_edit_request(query: str) -> bool:
    return bool(_EDIT_INTENT_RE.search(query or ""))


def turn_mutated(tools_used: list[str]) -> bool:
    return any(t in _MUTATING_TOOLS for t in tools_used)


class LocalEscalator:
    """Lazily-started second npurun server hosting the escalation model."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.model = str(config.get("escalation_local_model", "") or "")
        self.bind = str(config.get("escalation_local_bind", "127.0.0.1:11436"))
        self.max_tokens = int(config.get("escalation_max_output_tokens", 900))
        self.timeout_s = int(config.get("escalation_timeout_seconds", 240))
        self._proc: subprocess.Popen[bytes] | None = None
        self._failed = False

    @property
    def enabled(self) -> bool:
        return bool(self.model) and not self._failed

    # -- server lifecycle ---------------------------------------------------

    def _npurun_exe(self) -> Path:
        return Path.home() / ".cargo" / "bin" / "npurun.exe"

    def _env(self) -> dict[str, str]:
        env = os.environ.copy()
        sdk = Path(env.get("QNN_SDK_ROOT", r"C:\Qualcomm\AIStack\QAIRT_2.47.0"))
        env["QNN_SDK_ROOT"] = str(sdk)
        env["ADSP_LIBRARY_PATH"] = str(sdk / "lib" / "hexagon-v73" / "unsigned")
        env["PATH"] = (
            f"{sdk / 'bin' / 'aarch64-windows-msvc'};"
            f"{sdk / 'lib' / 'aarch64-windows-msvc'};"
            f"{self._npurun_exe().parent};{env.get('PATH', '')}"
        )
        return env

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(f"http://{self.bind}/healthz", timeout=3) as r:
                return r.status == 200
        except Exception:
            return False

    def ensure_server(self, wait_s: int = 90) -> bool:
        """Start the escalation server if it isn't already up. Slow on first
        use (bundle load ~10s + spawn); a no-op afterwards."""
        if self._healthy():
            return True
        exe = self._npurun_exe()
        if not exe.exists():
            self._failed = True
            return False
        try:
            self._proc = subprocess.Popen(
                [str(exe), "serve", "--model", self.model, "--bind", self.bind],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                env=self._env(), creationflags=DETACHED_PROCESS,
            )
        except Exception:
            self._failed = True
            return False
        deadline = time.time() + wait_s
        while time.time() < deadline:
            if self._healthy():
                return True
            time.sleep(2)
        self._failed = True  # don't retry every turn against a broken spawn
        return False

    def stop(self) -> None:
        if self._proc is not None:
            try:
                self._proc.kill()
            except Exception:
                pass
            self._proc = None

    # -- consultation -------------------------------------------------------

    def consult(self, situation: str) -> str | None:
        """Ask the escalation model for advice. Returns None on ANY failure —
        callers must degrade gracefully."""
        if not self.enabled or not self.ensure_server():
            return None
        body = json.dumps({
            "model": self.model,
            "messages": [
                {"role": "system", "content": _ESCALATION_SYSTEM},
                {"role": "user", "content": situation},
            ],
            "max_tokens": self.max_tokens,
            "temperature": 0.6,
            "stream": False,
            "stop": ["<|im_end|>", "<|im_start|>"],
        }).encode()
        req = urllib.request.Request(
            f"http://{self.bind}/v1/chat/completions",
            data=body, headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read())
            text = data["choices"][0]["message"]["content"] or ""
        except Exception:
            return None
        # The hybrid 8B thinks in <think> blocks; only the conclusion goes
        # back to the 4B loop.
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return text or None


def build_situation(
    query: str,
    recent_events: list[str],
    problem: str,
    max_chars: int = 4000,
) -> str:
    """Compact consultation prompt: the task, what happened, what went wrong."""
    events = "\n".join(f"- {e[:400]}" for e in recent_events[-8:])
    text = (
        f"TASK the agent was given:\n{query}\n\n"
        f"RECENT ACTIONS AND RESULTS:\n{events}\n\n"
        f"PROBLEM:\n{problem}\n\n"
        "What exactly should the agent do next?"
    )
    return text[:max_chars]
