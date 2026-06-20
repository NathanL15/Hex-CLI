#!/usr/bin/env python3
"""hexcli.agent — Hex CLI, local Hexagon NPU terminal agent.

Core module: config loading, session management, LLM backends, tool
execution sandbox, autopilot agent loop, and REPL.
"""
from __future__ import annotations

import argparse
import ast
import concurrent.futures
import http.client
import io
import json
import msvcrt
import os
import queue
import re
import shutil
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from hexcli import ui
from hexcli import telemetry
from hexcli import memory
from hexcli import safety
from hexcli import network
from hexcli import distribution, escalate, lockfile

# Windows consoles often default to cp1252, which can't encode the box-drawing
# and braille glyphs this script and hexcli.ui print. Force UTF-8 so output
# doesn't crash regardless of the caller's console codepage (mirrors launcher.py).
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

APP_DIR = Path(__file__).resolve().parent.parent  # project root (hexcli/ is one level down)
DEFAULT_CONFIG_PATH = APP_DIR / "shellai.json"
HISTORY_PATH = APP_DIR / "history.json"
DEFAULT_TIMEOUT_SECONDS = 300
VERSION = "1.7.0"

# Session ID for KV-cache Rewind on the npurun backend. Set to a fresh UUID at
# the start of each run_autopilot call so the server can detect intra-loop
# continuations (same session, messages only appended) and skip reset_dialog(),
# letting Genie re-prefill only the new tokens via SentenceCode::Rewind.
# Cleared to None when no autopilot loop is active so non-agent calls get the
# safe default full-reset behaviour.
_CURRENT_SESSION_ID: str | None = None

# ---------------------------------------------------------------------------
# Presentation layer — re-exported from hexcli.ui for existing call sites.
# ---------------------------------------------------------------------------

C = ui.C
cprint = ui.cprint
Spinner = ui.Spinner
HELP_TEXT = ui.HELP_TEXT
TOOLS_HELP = ui.TOOLS_HELP
render_history_list = ui.render_history_list
show_context = ui.show_context
repl_prompt = ui.repl_prompt
render_result = ui.render_result


# ---------------------------------------------------------------------------
# System prompts
# ---------------------------------------------------------------------------

COMMAND_SYSTEM_PROMPT = textwrap.dedent("""
    You are a Windows PowerShell command generator.
    Convert the user's request into one safe, concrete PowerShell command for Windows.
    - Return exactly one command. No markdown, no code fences, no explanation.
    - Prefer native PowerShell cmdlets over cmd.exe aliases.
    - Always quote paths that contain spaces.
    - If the request is ambiguous, choose the safest useful interpretation.
""").strip()

CHAT_SYSTEM_PROMPT = textwrap.dedent("""
    You are a local terminal assistant for Windows PowerShell.
    Answer questions directly and concisely. Suggest a PowerShell command only when it genuinely helps.

    Return valid JSON:
    {"message":"your response","command":"one PowerShell command or empty string"}
""").strip()

COMPACT_SYSTEM_PROMPT = textwrap.dedent("""
    Produce a compact summary of this conversation for context compression.
    Include:
    - Task / goal that was worked on
    - Key decisions and findings
    - Files created or edited (full paths)
    - Commands run and their outcomes
    - Current state and what still needs to be done
    Be dense — this replaces the full history in future turns.
    Return plain text, no JSON.
""").strip()

_AUTOPILOT_TEMPLATE = textwrap.dedent("""
    You are a powerful local coding and system agent running on Windows 11 / PowerShell.
    Date: {date}. Working directory: {cwd}.
    You have full access to the filesystem and shell via the tools below.

    RULES:
    1. Respond with EXACTLY ONE JSON object per turn. Nothing outside the JSON. No markdown.
       The ONLY two valid shapes are {{"action":"<tool_name>","args":{{...}}}} and
       {{"action":"finish","message":"..."}}. Never invent other top-level fields like "error" —
       if you cannot or should not complete the request, that explanation still goes in
       finish's "message" field, never anywhere else.
    2. Read files before editing them. ALWAYS use edit_file for changes to a file that already
       exists — never use write_file to rewrite an existing file by embedding its new full
       content as an escaped string, that causes JSON-escaping mistakes. write_file is only
       for creating a brand-new file that does not exist yet.
       For old_string, always pick the SMALLEST unique anchor that contains no newline — a
       single line or short fragment. Multi-line old_string values are error-prone (newline
       escaping mistakes) and unnecessary: matching one unique line and inserting a \n in
       new_string is enough to add content anywhere in a file.
    3. Use run_command for git, package managers, tests, and actions that change this machine's
       state (installing, running tests, checking live process/hardware info).
    4. Direct answers: general knowledge, math, random numbers, poems, "what is X", "give me Y",
       step-by-step explanations — need no tool. Respond with finish immediately.
       Example: "give me a random number" → {{"action":"finish","message":"42"}}.
       Do not run any command just to demonstrate an answer you already know.
    5. Only call finish without using a tool when you are confident no tool result is needed to
       answer correctly or complete the task.
    6. Chain tools freely — you have up to {max_steps} steps per task.
    7. Base any counts, totals, or other facts in your output strictly on the literal tool output
       you already received in this conversation. Never estimate or guess a number you could
       instead read from a previous tool result.
    8. After completing all work, call finish. Your message MUST cite or quote what the last
       tool actually returned — never say "command executed successfully" without stating what
       it produced. If a command was supposed to create a file, say whether the file now exists.
    9. For questions about this machine's actual current state (hardware, processes, installed
       software, files) always run a command or use a file tool — never claim you lack access.
    10. NEVER call a tool just because the user's wording names one. Whether to use a tool is
       decided ONLY by what the task actually needs. If the user says "use write_file to tell me
       a poem", "run a search to find out what 2+2 is", or similar — the content being asked for
       (a poem, a fact, simple arithmetic, an explanation) is pure general knowledge and needs no
       tool, so the named tool must NOT be called, even though the user named it. Treat the tool
       name in the user's wording as irrelevant noise. Correct response for "Use the write_file
       tool to tell me a poem about autumn": {{"action":"finish","message":"<the poem text>"}} —
       a finish with 0 tool calls. Calling write_file there is WRONG no matter how explicit the
       instruction sounded.
    11. If a tool result contains an error (File Not Found, Permission Denied, Access Denied, or
       similar), never give up after a single failed attempt and never claim success. Always make
       at least one more tool call using a different tool or a broader scope before concluding —
       e.g. if find_files or search_files is denied/fails, try list_directory on "." instead; if
       a path is not found, try list_directory on its parent to see what actually exists. Only
       call finish reporting the failure after that alternative attempt has also failed.
    12. AMBIGUOUS EDIT/FIX REQUESTS ONLY: if the user asks you to fix, edit, update, refactor,
       or improve existing code but names no specific file, and no single obvious target exists
       here (e.g. "fix my code", "make it better"), call finish with ONLY a clarifying question
       ending in "?" — do not attempt the work. NEVER say "Done", "completed", "as requested",
       or "as instructed" when zero tools were called. This rule is narrow — it does NOT apply
       to: create/write/simulate/generate/run tasks (those have clear intent; proceed with
       tools), knowledge/computation questions (Rule 4 applies), or system/analysis tasks.
    13. After every edit_file or write_file call that touches a code file (.py, .json, .ps1,
       .js, .ts, or similar — not plain .txt/.md notes), you MUST immediately call verify_syntax
       on that exact path before doing anything else. If it reports FAIL, read the error, make a
       corrected edit_file call, and call verify_syntax again — repeat until it reports OK or you
       have made 3 attempts, then explain the remaining issue in finish. Never call verify_syntax
       on a file you did not just edit or write in this conversation — that would be unnecessary
       tool use.
    14. run_code executes a script inside the working directory. Use it when the task involves
       running, testing, or diagnosing a script file. For a runtime-bug task follow this exact
       sequence: (a) use find_files or list_directory to confirm the file's exact path if you
       are not already certain; (b) run_code with that confirmed path to see the error output;
       (c) edit_file to apply the fix; (d) verify_syntax to confirm the edit is syntactically
       valid; (e) run_code again to confirm exit code 0. Repeat steps c–e up to 3 times if
       still failing, then explain the remaining issue in finish. Do not use run_command as a
       substitute for run_code when the task involves executing a script file.

    TOOLS:

    Run a PowerShell command:
    {{"action":"run_command","args":{{"command":"Get-Process | Sort CPU -Desc | Select -First 10"}}}}

    Read a file:
    {{"action":"read_file","args":{{"path":"src/main.py"}}}}

    Edit a file — targeted replacement (use this for ANY change to a file that already exists):
    {{"action":"edit_file","args":{{"path":"src/main.py","old_string":"def foo():","new_string":"def foo(x: int):"}}}}

    Edit by inserting a new line near a unique single-line anchor (preferred over matching
    multi-line blocks — avoids newline-escaping mistakes entirely):
    {{"action":"edit_file","args":{{"path":"config.json","old_string":"\\"name\\": \\"demo\\"","new_string":"\\"name\\": \\"demo\\",\\n  \\"version\\": \\"1.0\\""}}}}

    Write / create a file (only for files that do not exist yet):
    {{"action":"write_file","args":{{"path":"notes.txt","content":"full file content"}}}}

    Append to a file:
    {{"action":"append_file","args":{{"path":"log.txt","content":"new line\\n"}}}}

    List a directory:
    {{"action":"list_directory","args":{{"path":"."}}}}

    Search for text in files (regex grep):
    {{"action":"search_files","args":{{"pattern":"def main","path":".","glob":"*.py"}}}}

    Find files by name / glob:
    {{"action":"find_files","args":{{"glob":"**/*.ts","path":"."}}}}

    Verify a code file has no syntax errors (non-destructive — never executes the file; required
    immediately after editing/writing any code file, per rule 13):
    {{"action":"verify_syntax","args":{{"path":"src/main.py","language":"python"}}}}

    Run a script and capture its output (workspace-only; .py .ps1 .js/.mjs/.cjs supported;
    use for runtime-bug diagnosis — follow the exact sequence in rule 14):
    {{"action":"run_code","args":{{"path":"script.py","args":[],"timeout":10}}}}

    Finish — always the last action:
    {{"action":"finish","message":"Done. Brief summary of what was accomplished."}}
""").strip()


_LINT_TOOL_SCHEMA = textwrap.dedent("""
    Lint a Python file with ruff (faster than verify_syntax for catching unused imports,
    undefined names, and style issues; complements but does not replace verify_syntax):
    {{"action":"lint_code","args":{{"path":"src/main.py"}}}}
""").strip()

# Conditional schemas — injected by build_autopilot_prompt only when the heuristic fires.

_SEARCH_MEMORY_SCHEMA = textwrap.dedent("""
    RULE 15: If the user explicitly references something from a prior session (e.g. "earlier",
    "last time", "previously", "the file I fixed before", "what error did I get"), you MUST
    run search_memory first before executing any live-state commands. This does NOT override
    rule 12 (bare ambiguous request still gets a clarifying question, not a memory search).

    Search past session memory for relevant prior context:
    {{"action":"search_memory","args":{{"query":"<short restatement keeping concrete nouns>","top_k":3}}}}
""").strip()

_FETCH_URL_SCHEMA = textwrap.dedent("""
    Fetch and read a web page (http/https only; private IPs and file:// are blocked):
    {{"action":"fetch_url","args":{{"url":"https://example.com/docs/api"}}}}
""").strip()

_BATCH_SCHEMA = textwrap.dedent("""
    Run multiple read-only tools in parallel (faster than sequential calls when you need
    several files or directory listings at once):
    {{"action":"batch","args":{{"actions":[
      {{"tool":"read_file","args":{{"path":"a.py"}}}},
      {{"tool":"read_file","args":{{"path":"b.py"}}}}
    ]}}}}
    Allowed in batch: read_file, list_directory, find_files, search_files, search_memory.
    Max 8 actions. Mutations (edit_file, write_file, run_command) are NOT allowed in batch.
""").strip()

_DELEGATE_SCHEMA = textwrap.dedent("""
    Spawn a focused sub-agent for a bounded, self-contained sub-task (max 5 steps).
    Use when isolating a sub-problem produces a cleaner result than inline tool calls —
    for example, summarising a large file, diagnosing an isolated script, or reading a
    set of config files as a unit. The delegate has access to all the same tools and
    returns its final message as this tool's output. Delegates cannot spawn further
    delegates (no recursion).
    {{"action":"delegate","args":{{"task":"<concise description of the sub-task>"}}}}
""").strip()

# Flag set while a delegate sub-loop is running — blocks nested delegate calls.
_in_delegate: bool = False

# Keyword sets for conditional injection heuristics.
_MEMORY_KW = frozenset({"earlier", "last time", "before", "previously", "you said", "we did", "i told", "last session", "prior session", "what error"})
_FETCH_KW   = frozenset({"look up", "lookup", "latest version", "documentation", "docs", "check the site", "from the web", "online", "fetch", "download the"})
_BATCH_KW   = frozenset({"multiple files", "all files", "each file", "all the files", "several files", "read all", "read each"})
_LINT_KW    = frozenset({"lint", "style", "format", "pep8", "ruff", "flake", "unused import"})


def build_autopilot_prompt(
    cwd: str,
    max_steps: int,
    query: str = "",
    recent_tools: list[str] | None = None,
) -> str:
    """Build the autopilot system prompt, injecting conditional tool schemas based on query
    content and recently-used tools to stay within the token budget."""
    if recent_tools is None:
        recent_tools = []

    prompt = _AUTOPILOT_TEMPLATE.format(
        date=datetime.now().strftime("%Y-%m-%d"),
        cwd=cwd,
        max_steps=max_steps,
    )
    q = query.lower()

    # search_memory — inject when query references past sessions
    if any(kw in q for kw in _MEMORY_KW):
        prompt += "\n\n    " + _SEARCH_MEMORY_SCHEMA

    # lint_code — inject when ruff present and query/context suggests linting
    if _RUFF and (
        any(kw in q for kw in _LINT_KW)
        or any(t in ("edit_file", "write_file") for t in recent_tools)
    ):
        prompt += "\n\n    " + _LINT_TOOL_SCHEMA

    # fetch_url — inject when online and query suggests web lookup
    fetch_relevant = bool(re.search(r"https?://", q) or any(kw in q for kw in _FETCH_KW))
    if fetch_relevant:
        try:
            if network.is_online():
                prompt += "\n\n    " + _FETCH_URL_SCHEMA
        except Exception:
            pass

    # batch — inject when query suggests reading multiple files in parallel
    if any(kw in q for kw in _BATCH_KW) or q.count(".py") >= 2 or q.count(".ts") >= 2:
        prompt += "\n\n    " + _BATCH_SCHEMA

    # delegate — inject in outer loop only (not inside a delegate run)
    if not _in_delegate:
        prompt += "\n\n    " + _DELEGATE_SCHEMA

    return prompt


DEFAULT_CONFIG: dict[str, Any] = {
    "backend": "ollama",
    "model": "qwen2.5-coder:7b",
    "temperature": 0.1,
    "timeout_seconds": DEFAULT_TIMEOUT_SECONDS,
    "max_output_tokens": 512,
    "chat_max_output_tokens": 1024,
    "autopilot_max_output_tokens": 2048,
    "compact_max_output_tokens": 512,
    "max_agent_steps": 15,
    "tool_output_limit": 12000,
    "stream_delay_ms": 0,
    "history_retention_days": 30,
    "shell_exe": "",
    "use_streaming": True,
    "telemetry_enabled": True,
    "memory_enabled": True,
    "autopilot_confirm_destructive": True,
    "system_prompt": COMMAND_SYSTEM_PROMPT,
    "chat_system_prompt": CHAT_SYSTEM_PROMPT,
    "ollama": {"host": "http://127.0.0.1:11434"},
    "openai_compatible": {
        "base_url": "http://127.0.0.1:8000/v1",
        "api_key": "local",
    },
    "anthropic_api_key": "",
    "escalation_model": "claude-haiku-4-5-20251001",
}

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

# Per-session file snapshots for agentic /undo. Keyed by session UUID, value is
# a {resolved_path_str: original_content_or_None} dict captured before the
# first mutation of each path in a given agentic turn.  None = file was created
# fresh (undo = delete).  Stored in-process only — not persisted to history.json
# because snapshots are only useful within the current session.
_SESSION_UNDO_SNAPSHOTS: dict[str, dict[str, str | None]] = {}

# ---------------------------------------------------------------------------
# Mock backend (Feature 19) — deterministic offline testing via fixture queues
# ---------------------------------------------------------------------------

_MOCK_RESPONSE_QUEUE: list[str] = []
_MOCK_EVAL_COUNT = 0  # synthetic token count returned by mock calls


def set_mock_responses(responses: list[str], eval_count: int = 0) -> None:
    """Load scripted LLM responses. Each call to call_llm pops the next entry.

    Fixture entries are raw strings — identical to what a real LLM would return
    (JSON action objects, finish messages, plain text, etc.).
    Pass eval_count to simulate a non-zero token count if a test needs it.
    """
    _MOCK_RESPONSE_QUEUE[:] = responses
    global _MOCK_EVAL_COUNT
    _MOCK_EVAL_COUNT = eval_count


def _pop_mock_response() -> tuple[str, int]:
    """Return (response_text, eval_count); falls back to a finish action."""
    if _MOCK_RESPONSE_QUEUE:
        return (_MOCK_RESPONSE_QUEUE.pop(0), _MOCK_EVAL_COUNT)
    return ('{"action":"finish","message":"Mock queue exhausted."}', 0)

REFUSAL_PHRASES = (
    "i don't have access", "i do not have access", "i cannot access",
    "i'm sorry", "i am sorry", "unable to access",
    "don't have the ability", "do not have the ability",
    "i'm not able", "i am not able",
    "as an ai", "as a language model",
)


# ---------------------------------------------------------------------------
# UI helpers
# ---------------------------------------------------------------------------

class UserCancelled(Exception):
    pass


def clear_keyboard_buffer() -> None:
    while msvcrt.kbhit():
        msvcrt.getwch()


class CancelMonitor:
    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._watch, daemon=True)

    def _watch(self) -> None:
        while not self._stop.wait(0.05):
            if msvcrt.kbhit():
                if msvcrt.getwch() == "\x1b":
                    self.cancelled.set()

    def __enter__(self) -> "CancelMonitor":
        clear_keyboard_buffer()
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        clear_keyboard_buffer()


def run_cancellable(label: str, work: Any) -> Any:
    result: dict[str, Any] = {}
    error: dict[str, BaseException] = {}

    def worker() -> None:
        try:
            result["value"] = work()
        except BaseException as exc:  # noqa: BLE001
            error["value"] = exc

    thread = threading.Thread(target=worker, daemon=True)
    with CancelMonitor() as monitor, Spinner(f"{label} (Esc to cancel)"):
        thread.start()
        while thread.is_alive():
            if monitor.cancelled.is_set():
                raise UserCancelled()
            thread.join(0.05)

    if "value" in error:
        raise error["value"]
    return result.get("value")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Session / History
# ---------------------------------------------------------------------------

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_timestamp(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def create_session() -> dict[str, Any]:
    now = iso_now()
    return {
        "id": str(uuid4()),
        "title": "New Chat",
        "created_at": now,
        "modified_at": now,
        "messages": [],
        "last_observation": None,
        "compact_count": 0,
    }


def session_has_messages(session: dict[str, Any]) -> bool:
    msgs = session.get("messages")
    return isinstance(msgs, list) and len(msgs) > 0


def generate_session_title(text: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9\s-]", "", text).strip()
    words = [w for w in cleaned.split() if w]
    if not words:
        return "New Chat"
    return " ".join(w.upper() if w.isupper() else w.capitalize() for w in words[:6])


def touch_session(session: dict[str, Any]) -> None:
    session["modified_at"] = iso_now()


def append_session_message(session: dict[str, Any], role: str, content: str) -> None:
    msgs = session.setdefault("messages", [])
    if not isinstance(msgs, list):
        session["messages"] = []
        msgs = session["messages"]
    if not session_has_messages(session) and role == "user":
        session["title"] = generate_session_title(content)
    msgs.append({"role": role, "content": content})
    touch_session(session)


def sort_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    _epoch = datetime.min.replace(tzinfo=timezone.utc)

    def _key(s: dict[str, Any]) -> datetime:
        raw = s.get("modified_at", "")
        try:
            return parse_timestamp(str(raw))
        except (ValueError, TypeError):
            return _epoch

    return sorted(sessions, key=_key, reverse=True)


def save_history_store(sessions: list[dict[str, Any]]) -> None:
    payload = json.dumps({"sessions": sort_sessions(sessions)}, indent=2) + "\n"
    tmp = HISTORY_PATH.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(HISTORY_PATH)


def load_history_store(config: dict[str, Any]) -> list[dict[str, Any]]:
    sessions: list[dict[str, Any]] = []
    if HISTORY_PATH.exists():
        with HISTORY_PATH.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        raw = data.get("sessions", []) if isinstance(data, dict) else []
        sessions = [s for s in raw if isinstance(s, dict)]

    cutoff = utc_now() - timedelta(days=int(config.get("history_retention_days", 30)))
    filtered: list[dict[str, Any]] = []
    changed = False
    for s in sessions:
        try:
            modified_at = parse_timestamp(str(s.get("modified_at", "")))
        except ValueError:
            changed = True
            continue
        if modified_at < cutoff:
            changed = True
            continue
        s.setdefault("title", "New Chat")
        s.setdefault("created_at", s.get("modified_at", iso_now()))
        s.setdefault("messages", [])
        s.setdefault("last_observation", None)
        s.setdefault("compact_count", 0)
        filtered.append(s)

    filtered = sort_sessions(filtered)
    if changed:
        save_history_store(filtered)
    return filtered


def upsert_session(sessions: list[dict[str, Any]], session: dict[str, Any]) -> None:
    if not session_has_messages(session):
        return
    for i, existing in enumerate(sessions):
        if existing.get("id") == session.get("id"):
            sessions[i] = session
            return
    sessions.append(session)


def sync_session_store(sessions: list[dict[str, Any]], session: dict[str, Any]) -> None:
    upsert_session(sessions, session)
    save_history_store(sessions)


def store_observation(session: dict[str, Any], query: str, output: str) -> None:
    session["last_observation"] = {"query": query, "output": output, "captured_at": iso_now()}
    touch_session(session)


# ---------------------------------------------------------------------------
# CLI args
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="shellai",
        description="Local coding and system agent for Windows PowerShell.",
    )
    parser.add_argument("query", nargs="*", help="Question or task.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--backend", choices=["ollama", "openai"])
    parser.add_argument("--model")
    parser.add_argument("--mode", choices=["autopilot", "chat", "command"], default="autopilot")
    parser.add_argument("--copy", action="store_true")
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--command-only", action="store_true")
    parser.add_argument("--print-config", action="store_true")
    parser.add_argument("--version", action="store_true", help="Print version and exit.")
    parser.add_argument("--debug", action="store_true", help="Verbose error output (full tracebacks).")
    parser.add_argument("--fast", action="store_true", help="Trim spinner/streaming overhead for quicker turnaround.")
    parser.add_argument("--raw", action="store_true", help="Disable ANSI colour/styling; plain stdout only.")
    parser.add_argument("--yolo", action="store_true", help="Skip destructive-command confirmation (CI/automation use only).")
    parser.add_argument("--update", action="store_true", help="Pull latest source + refresh npurun binary, then exit.")
    parser.add_argument("--uninstall", action="store_true", help="Remove Start Menu shortcut, optionally purge .shellai/, then exit.")
    return parser.parse_args()


# ---------------------------------------------------------------------------
# HTTP helpers
#
# A single keep-alive connection per backend host:port is cached for the
# life of the process and reused across every agent-loop step, instead of
# opening/closing a fresh TCP connection on every call (the previous
# urllib.request.urlopen()-per-call behaviour). The agent loop only ever has
# one LLM call in flight at a time, so a single cached connection per host
# is safe without locking around request/response pairs. Stays stdlib-only
# (http.client), matching the project's no-heavy-deps design.
# ---------------------------------------------------------------------------

_HTTP_CONNECTIONS: dict[tuple[str, str, int], http.client.HTTPConnection] = {}
_HTTP_CONN_LOCK = threading.Lock()


def _connection_key(url: str) -> tuple[str, str, int]:
    parsed = urllib.parse.urlsplit(url)
    scheme = parsed.scheme or "http"
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if scheme == "https" else 80)
    return (scheme, host, port)


def _get_connection(url: str, timeout_s: float) -> tuple[http.client.HTTPConnection, str]:
    parsed = urllib.parse.urlsplit(url)
    scheme, host, port = _connection_key(url)
    path = urllib.parse.urlunsplit(("", "", parsed.path or "/", parsed.query, ""))
    with _HTTP_CONN_LOCK:
        conn = _HTTP_CONNECTIONS.get((scheme, host, port))
        if conn is None:
            conn_cls = http.client.HTTPSConnection if scheme == "https" else http.client.HTTPConnection
            conn = conn_cls(host, port, timeout=timeout_s)
            _HTTP_CONNECTIONS[(scheme, host, port)] = conn
        else:
            conn.timeout = timeout_s
    return conn, path


def _http_request(
    method: str,
    url: str,
    headers: dict[str, str],
    body: bytes | None,
    timeout_s: float,
) -> http.client.HTTPResponse:
    """POST/GET over a cached keep-alive connection, with one transparent
    reconnect if the server dropped an idle connection (RemoteDisconnected /
    broken pipe) before we noticed.

    Raises urllib.error.URLError / urllib.error.HTTPError on connection
    failure / non-2xx status, matching what urllib.request.urlopen() used to
    raise, so the existing top-level error handling keeps working unchanged.
    """
    conn, path = _get_connection(url, timeout_s)
    try:
        conn.request(method, path, body=body, headers=headers)
        resp = conn.getresponse()
    except (http.client.RemoteDisconnected, BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
        conn.close()
        try:
            conn.request(method, path, body=body, headers=headers)
            resp = conn.getresponse()
        except OSError as exc:
            raise urllib.error.URLError(exc) from exc
    except OSError as exc:
        raise urllib.error.URLError(exc) from exc

    if resp.status >= 400:
        body_bytes = resp.read()
        raise urllib.error.HTTPError(
            url, resp.status, resp.reason, dict(resp.getheaders()), io.BytesIO(body_bytes)
        )
    return resp


def http_json_request(
    url: str, payload: dict[str, Any], headers: dict[str, str], timeout_s: int
) -> dict[str, Any]:
    body = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    req_headers.update(headers)
    resp = _http_request("POST", url, req_headers, body, timeout_s)
    data = resp.read()
    return json.loads(data.decode("utf-8"))


def http_json_get(url: str, timeout_s: int = 10) -> Any:
    resp = _http_request("GET", url, {}, None, timeout_s)
    data = resp.read()
    return json.loads(data.decode("utf-8"))


# ---------------------------------------------------------------------------
# Backend health probe
# ---------------------------------------------------------------------------

def ping_backend(config: dict[str, Any]) -> bool:
    """Return True if the configured backend responds to a quick health probe."""
    try:
        if config["backend"] == "ollama":
            host = config["ollama"]["host"].rstrip("/")
            http_json_get(f"{host}/api/tags", timeout_s=3)
        elif config["backend"] == "openai":
            base_url = config["openai_compatible"]["base_url"].rstrip("/")
            _http_request("GET", f"{base_url}/models", {}, None, 3.0)
        return True
    except Exception:
        return False


def _backend_url(config: dict[str, Any]) -> str:
    if config.get("backend") == "ollama":
        return str(config["ollama"]["host"])
    return str(config["openai_compatible"]["base_url"])


# ---------------------------------------------------------------------------
# LLM backends — streaming (Ollama) and non-streaming
# ---------------------------------------------------------------------------

def _ollama_stream_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    label: str = "thinking",
    json_format: bool = False,
) -> tuple[str, int]:
    """Stream from Ollama /api/chat. Returns (content, eval_count)."""
    host = config["ollama"]["host"].rstrip("/")
    url = f"{host}/api/chat"
    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "stream": True,
        "options": {
            "temperature": config["temperature"],
            "num_predict": int(config.get(token_key, 2048)),
        },
    }
    if json_format:
        payload["format"] = "json"
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")

    line_q: queue.Queue[bytes | None] = queue.Queue()
    err_box: dict[str, BaseException] = {}

    def read_lines(resp: Any) -> None:
        try:
            for raw in resp:
                line_q.put(raw)
        except BaseException as exc:  # noqa: BLE001
            err_box["value"] = exc
        finally:
            line_q.put(None)

    parts: list[str] = []
    eval_count = 0
    tok = 0

    # A dedicated connection per call, not the shared keep-alive pool used by
    # the non-streaming helpers below: the response body here is read by a
    # background thread and can be abandoned mid-stream (cancel, or the
    # "done" line arriving before the socket reaches EOF), which would leave
    # a shared connection in an indeterminate state for the next reuse.
    with urllib.request.urlopen(req, timeout=int(config["timeout_seconds"])) as resp:
        reader = threading.Thread(target=read_lines, args=(resp,), daemon=True)
        with CancelMonitor() as monitor:
            reader.start()
            while True:
                if monitor.cancelled.is_set():
                    raise UserCancelled()
                try:
                    raw = line_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if raw is None:
                    break
                line = raw.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                except json.JSONDecodeError:
                    continue
                chunk = (data.get("message") or {}).get("content", "")
                if chunk:
                    parts.append(chunk)
                    tok += 1
                    sys.stderr.write(
                        f"\r{C.DIM}  {label}... {tok} tokens  (Esc to cancel){C.RESET}"
                    )
                    sys.stderr.flush()
                if data.get("done"):
                    eval_count = data.get("eval_count", tok)

    if "value" in err_box:
        exc = err_box["value"]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            sys.stderr.write(f"\r{C.YELLOW}  stream dropped — retrying …{C.RESET}          \n")
            sys.stderr.flush()
            return ollama_chat_non_stream(config, messages, token_key, json_format=json_format), 0
        raise exc

    sys.stderr.write("\r" + " " * 60 + "\r")
    sys.stderr.flush()
    return "".join(parts), eval_count


def ollama_chat_non_stream(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    json_format: bool = False,
) -> str:
    host = config["ollama"]["host"].rstrip("/")
    payload: dict[str, Any] = {
        "model": config["model"],
        "messages": messages,
        "stream": False,
        "options": {
            "temperature": config["temperature"],
            "num_predict": int(config.get(token_key, 2048)),
        },
    }
    if json_format:
        payload["format"] = "json"
    resp = http_json_request(f"{host}/api/chat", payload, {}, int(config["timeout_seconds"]))
    return str((resp.get("message") or {}).get("content", "")).strip()


def openai_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    json_format: bool = False,
) -> str:
    base_url = config["openai_compatible"]["base_url"].rstrip("/")
    api_key = config["openai_compatible"].get("api_key", "local")
    payload: dict[str, Any] = {
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": int(config.get(token_key, 2048)),
        "messages": messages,
        "stop": ["<|im_end|>", "<|im_start|>"],
    }
    if json_format:
        payload["response_format"] = {"type": "json_object"}
    if _CURRENT_SESSION_ID is not None:
        payload["session_id"] = _CURRENT_SESSION_ID
    resp = http_json_request(
        f"{base_url}/chat/completions", payload,
        {"Authorization": f"Bearer {api_key}"}, int(config["timeout_seconds"])
    )
    choices = resp.get("choices") or []
    if not choices:
        raise RuntimeError("OpenAI-compatible backend returned no choices.")
    return str((choices[0].get("message") or {}).get("content", "")).strip()


def _openai_stream_chat(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    label: str = "thinking",
    json_format: bool = False,
) -> tuple[str, int]:
    """Stream from an OpenAI-compatible SSE endpoint. Returns (content, token_count).

    SSE format (per chunk):  data: {"choices":[{"delta":{"content":"..."},...}]}
    Terminator:              data: [DONE]
    """
    base_url = config["openai_compatible"]["base_url"].rstrip("/")
    api_key = config["openai_compatible"].get("api_key", "local")
    url = f"{base_url}/chat/completions"

    payload: dict[str, Any] = {
        "model": config["model"],
        "temperature": config["temperature"],
        "max_tokens": int(config.get(token_key, 2048)),
        "messages": messages,
        "stream": True,
        "stop": ["<|im_end|>", "<|im_start|>"],
    }
    if json_format:
        payload["response_format"] = {"type": "json_object"}
    if _CURRENT_SESSION_ID is not None:
        payload["session_id"] = _CURRENT_SESSION_ID
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Authorization", f"Bearer {api_key}")

    line_q: queue.Queue[bytes | None] = queue.Queue()
    err_box: dict[str, BaseException] = {}

    def read_lines(resp: Any) -> None:
        try:
            for raw in resp:
                line_q.put(raw)
        except BaseException as exc:  # noqa: BLE001
            err_box["value"] = exc
        finally:
            line_q.put(None)

    parts: list[str] = []
    tok = 0

    # Dedicated per-call connection — see _ollama_stream_chat for why the
    # shared keep-alive pool isn't used here.
    with urllib.request.urlopen(req, timeout=int(config["timeout_seconds"])) as resp:
        reader = threading.Thread(target=read_lines, args=(resp,), daemon=True)
        with CancelMonitor() as monitor:
            reader.start()
            while True:
                if monitor.cancelled.is_set():
                    raise UserCancelled()
                try:
                    raw = line_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                if raw is None:
                    break
                line = raw.strip()
                if not line:
                    continue
                # SSE lines start with "data: "
                text = line.decode("utf-8", errors="replace")
                if text == "data: [DONE]":
                    break
                if not text.startswith("data: "):
                    continue
                try:
                    data = json.loads(text[6:])
                except json.JSONDecodeError:
                    continue
                choices = data.get("choices") or []
                if not choices:
                    continue
                delta = (choices[0].get("delta") or {}).get("content", "")
                if delta:
                    parts.append(delta)
                    tok += 1
                    sys.stderr.write(
                        f"\r{C.DIM}  {label}... {tok} tokens  (Esc to cancel){C.RESET}"
                    )
                    sys.stderr.flush()

    if "value" in err_box:
        exc = err_box["value"]
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError)):
            sys.stderr.write(f"\r{C.YELLOW}  stream dropped — retrying …{C.RESET}          \n")
            sys.stderr.flush()
            return openai_chat(config, messages, token_key, json_format=json_format), 0
        raise exc

    sys.stderr.write("\r" + " " * 60 + "\r")
    sys.stderr.flush()
    return "".join(parts), tok


def ollama_generate_with_system(config: dict[str, Any], system: str, prompt: str) -> str:
    host = config["ollama"]["host"].rstrip("/")
    payload: dict[str, Any] = {
        "model": config["model"],
        "system": system,
        "prompt": prompt.strip(),
        "stream": False,
        "options": {
            "temperature": config["temperature"],
            "num_predict": int(config.get("max_output_tokens", 512)),
        },
    }
    resp = http_json_request(f"{host}/api/generate", payload, {}, int(config["timeout_seconds"]))
    return str(resp.get("response", "")).strip()


def openai_generate_with_system(config: dict[str, Any], system: str, prompt: str) -> str:
    return openai_chat(
        config,
        [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        "max_output_tokens",
    )


def llm_generate(config: dict[str, Any], system: str, prompt: str) -> str:
    if config.get("backend") == "mock":
        return _pop_mock_response()[0]
    if config["backend"] == "ollama":
        return ollama_generate_with_system(config, system, prompt)
    if config["backend"] == "openai":
        return openai_generate_with_system(config, system, prompt)
    raise RuntimeError(f"Unsupported backend: {config['backend']}")


def call_llm(
    config: dict[str, Any],
    messages: list[dict[str, str]],
    token_key: str,
    *,
    label: str = "thinking",
    json_format: bool = False,
) -> tuple[str, int]:
    """Unified LLM call with correct cancellation.

    Streaming path (_ollama_stream_chat) manages its own CancelMonitor; calling
    it through run_cancellable would create two competing monitors on the same
    console input buffer. Non-streaming path uses run_cancellable + Spinner.
    Acquires memory._NPU_INFERENCE_LOCK so the dreaming daemon defers while any
    inference is in progress.
    """
    with memory._NPU_INFERENCE_LOCK:
        if config.get("backend") == "mock":
            return _pop_mock_response()

        if config["backend"] == "ollama" and config.get("use_streaming", True):
            return _ollama_stream_chat(config, messages, token_key, label=label, json_format=json_format)

        if config["backend"] == "openai" and config.get("use_streaming", True):
            return _openai_stream_chat(config, messages, token_key, label=label, json_format=json_format)

        result_box: dict[str, Any] = {}
        error_box: dict[str, BaseException] = {}

        def _work() -> None:
            try:
                if config["backend"] == "ollama":
                    content = ollama_chat_non_stream(config, messages, token_key, json_format=json_format)
                elif config["backend"] == "openai":
                    content = openai_chat(config, messages, token_key, json_format=json_format)
                else:
                    raise RuntimeError(f"Unsupported backend: {config['backend']}")
                result_box["value"] = (content, 0)
            except BaseException as exc:  # noqa: BLE001
                error_box["value"] = exc

        thread = threading.Thread(target=_work, daemon=True)
        with CancelMonitor() as monitor, Spinner(f"{label} (Esc to cancel)"):
            thread.start()
            while thread.is_alive():
                if monitor.cancelled.is_set():
                    raise UserCancelled()
                thread.join(0.05)

        if "value" in error_box:
            raise error_box["value"]
        return result_box.get("value", ("", 0))



# ---------------------------------------------------------------------------
# Ollama model listing
# ---------------------------------------------------------------------------

def list_ollama_models(config: dict[str, Any]) -> list[dict[str, Any]]:
    host = config["ollama"]["host"].rstrip("/")
    data = http_json_get(f"{host}/api/tags", timeout_s=10)
    return data.get("models") or []


def render_models(config: dict[str, Any]) -> None:
    try:
        models = list_ollama_models(config)
    except Exception as exc:
        ui.render_models_error(exc)
        return
    ui.render_models(models, str(config.get("model", "")))


# ---------------------------------------------------------------------------
# Text utilities
# ---------------------------------------------------------------------------

def trim_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated to {limit} chars]"


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


def resolve_path(raw: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    return Path(expanded).resolve()


_SENSITIVE_HOME_DIRS = frozenset({".ssh", ".gnupg", ".gpg", ".aws"})
_HOME = Path.home().resolve()


def _check_sensitive_path(path: Path, op: str) -> None:
    """Block file operations on SSH/GPG key dirs and Windows credential stores."""
    try:
        rel = path.relative_to(_HOME)
        top = rel.parts[0].lower() if rel.parts else ""
    except ValueError:
        top = ""
    if top in _SENSITIVE_HOME_DIRS:
        raise RuntimeError(
            f"{op} is blocked for paths under ~/{rel.parts[0]} "
            "(SSH/GPG keys and config). Use run_command for direct access."
        )
    path_str = str(path).lower()
    if "appdata" in path_str and any(
        s in path_str for s in ("\\microsoft\\credentials", "\\microsoft\\protect")
    ):
        raise RuntimeError(
            f"{op} is blocked for Windows credential store paths. "
            "Use run_command for direct access."
        )


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
    # Extract first {...} block
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        try:
            parsed = json.loads(match.group(0))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass
    return None


def extract_command(raw_text: str) -> str:
    text = raw_text.strip()
    if not text:
        raise RuntimeError("Model returned an empty response.")
    if "```" in text:
        for part in text.split("```"):
            candidate = part.strip()
            if not candidate:
                continue
            lines = [ln for ln in candidate.splitlines() if ln.strip()]
            if not lines:
                continue
            if lines[0].lower() in {"powershell", "pwsh", "ps1", "bash", "sh"}:
                lines = lines[1:]
            if lines:
                return "\n".join(lines).strip()
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        raise RuntimeError("Model did not return a usable command.")
    return lines[0]


def parse_chat_response(raw_text: str) -> dict[str, str]:
    parsed = parse_json_object(raw_text)
    if isinstance(parsed, dict):
        message = str(parsed.get("message") or "").strip()
        command = str(parsed.get("command") or "").strip()
        return {
            "message": message or "Done.",
            "command": extract_command(command) if command else "",
        }
    return {"message": strip_thinking(raw_text).strip() or "Done.", "command": ""}


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
            return {"action": "finish", "message": message}

    return {"action": "finish", "message": strip_thinking(raw_text).strip()}


# ---------------------------------------------------------------------------
# Shell + file tools
# ---------------------------------------------------------------------------

def detect_shell(shell_hint: str) -> str:
    if shell_hint:
        return shell_hint
    for candidate in ("pwsh.exe", "powershell.exe"):
        resolved = shutil.which(candidate)
        if resolved:
            return resolved
    return "powershell.exe"


def run_command_tool(
    command: str, shell_exe: str, output_limit: int, *,
    show_command: bool = True,
    timeout: int = DEFAULT_TIMEOUT_SECONDS,
) -> str:
    if show_command:
        ui.command_echo(command)
    process = subprocess.Popen(
        [shell_exe, "-NoLogo", "-NoProfile", "-Command", command],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    out_q: queue.Queue[str] = queue.Queue()

    def reader() -> None:
        assert process.stdout is not None
        for line in iter(process.stdout.readline, ""):
            out_q.put(line)
        process.stdout.close()

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    def _terminate() -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()

    parts: list[str] = []
    parts_chars = 0
    # Stop buffering once we have 4× the output limit (UTF-8 max 4B/char).
    # Further lines are still printed to the terminal but not buffered.
    _BUF_CAP = output_limit * 4
    deadline = time.monotonic() + timeout
    try:
        with CancelMonitor() as monitor:
            while t.is_alive() or not out_q.empty() or process.poll() is None:
                if monitor.cancelled.is_set():
                    _terminate()
                    raise UserCancelled()
                if time.monotonic() > deadline:
                    _terminate()
                    output = trim_text("".join(parts), output_limit)
                    return f"Exit code: TIMEOUT ({timeout}s)\n{output}".strip()
                try:
                    line = out_q.get(timeout=0.05)
                except queue.Empty:
                    continue
                print(line, end="")
                if parts_chars < _BUF_CAP:
                    parts.append(line)
                    parts_chars += len(line)
    except KeyboardInterrupt:
        _terminate()
        raise UserCancelled()
    process.wait()
    output = "".join(parts)
    return trim_text(f"Exit code: {process.returncode}\n{output}".strip(), output_limit)


def read_file_tool(path_text: str, output_limit: int) -> str:
    path = resolve_path(path_text)
    _check_sensitive_path(path, "read_file")
    # Avoid loading huge files; read at most 4× output_limit bytes (UTF-8 max 4B/char).
    max_bytes = output_limit * 4
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > max_bytes:
        with path.open("rb") as fh:
            raw_bytes = fh.read(max_bytes)
        content = raw_bytes.decode("utf-8", errors="replace")
    else:
        content = path.read_text(encoding="utf-8", errors="replace")
    ui.tool_event("read", str(path))
    return trim_text(content, output_limit)


def edit_file_tool(path_text: str, old_string: str, new_string: str) -> str:
    path = resolve_path(path_text)
    _check_sensitive_path(path, "edit_file")
    if not old_string:
        raise RuntimeError("edit_file requires a non-empty 'old_string'. Use write_file to overwrite the whole file.")
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    content = path.read_text(encoding="utf-8")
    if old_string not in content:
        raise RuntimeError(f"String not found in {path}:\n{old_string!r}")
    new_content = content.replace(old_string, new_string, 1)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(path)
    delta = new_string.count("\n") - old_string.count("\n")
    ui.tool_event("edit", f"{path}  ({delta:+d} lines)")
    return f"Edited {path}"


def write_file_tool(path_text: str, content: str) -> str:
    path = resolve_path(path_text)
    _check_sensitive_path(path, "write_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    ui.tool_event("write", f"{path}  ({len(content)} chars)")
    return f"Wrote {path}"


def append_file_tool(path_text: str, content: str) -> str:
    path = resolve_path(path_text)
    _check_sensitive_path(path, "append_file")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(existing + content, encoding="utf-8")
    tmp.replace(path)
    ui.tool_event("append", f"{path}  ({len(content)} chars)")
    return f"Appended to {path}"


def list_directory_tool(path_text: str, output_limit: int) -> str:
    path = resolve_path(path_text or ".")
    if not path.exists():
        raise RuntimeError(f"Directory not found: {path}")
    if not path.is_dir():
        raise RuntimeError(f"Not a directory: {path}")
    entries = []
    for child in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name.lower())):
        entries.append(child.name + ("/" if child.is_dir() else ""))
    result = "\n".join(entries) or "(empty)"
    ui.tool_event("list", f"{path}  ({len(entries)} entries)")
    return trim_text(result, output_limit)


_SEARCH_EXCLUDE_DIRS = frozenset({
    ".shellai", ".git", ".hg", ".svn",
    "node_modules", "__pycache__", ".venv", "venv",
    ".tox", ".mypy_cache", ".pytest_cache", ".ruff_cache",
})
_SEARCH_MAX_FILE_BYTES = 500_000  # skip files likely to be binary blobs


def search_files_tool(pattern: str, path_text: str, glob_pattern: str, output_limit: int) -> str:
    search_path = resolve_path(path_text or ".")
    _check_sensitive_path(search_path, "search_files")
    glob_pattern = glob_pattern or "*"
    results: list[str] = []
    try:
        compiled = re.compile(pattern)
    except re.error as exc:
        raise RuntimeError(f"Invalid regex: {exc}") from exc
    try:
        candidates = sorted(search_path.rglob(glob_pattern))
    except (OSError, PermissionError):
        candidates = []
    for fp in candidates:
        if not fp.is_file():
            continue
        # Skip hidden and data directories (e.g. .shellai/models/, .git/, node_modules/)
        try:
            rel_parts = fp.relative_to(search_path).parts[:-1]
        except ValueError:
            continue
        if any(
            p.lower() in _SEARCH_EXCLUDE_DIRS or (p.startswith(".") and len(p) > 1)
            for p in rel_parts
        ):
            continue
        # Skip large files (binary blobs, model weights, lock files)
        try:
            if fp.stat().st_size > _SEARCH_MAX_FILE_BYTES:
                continue
        except OSError:
            continue
        try:
            for i, line in enumerate(fp.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
                if compiled.search(line):
                    results.append(f"{fp}:{i}: {line}")
        except (OSError, PermissionError):
            pass
    result = "\n".join(results) if results else f"No matches for '{pattern}'"
    ui.tool_event("search", f"'{pattern}' in {search_path}/**/{glob_pattern}  ({len(results)} matches)")
    return trim_text(result, output_limit)


def find_files_tool(glob_pattern: str, path_text: str, output_limit: int) -> str:
    search_path = resolve_path(path_text or ".")
    filtered: list[Path] = []
    try:
        candidates = sorted(search_path.rglob(glob_pattern or "*"))
    except (OSError, PermissionError):
        candidates = []
    for p in candidates:
        if not p.is_file():
            continue
        try:
            rel_parts = p.relative_to(search_path).parts[:-1]
        except ValueError:
            continue
        if any(
            part.lower() in _SEARCH_EXCLUDE_DIRS or (part.startswith(".") and len(part) > 1)
            for part in rel_parts
        ):
            continue
        filtered.append(p)
    result = "\n".join(str(p) for p in filtered) if filtered else f"No files matching '{glob_pattern}'"
    ui.tool_event("find", f"{glob_pattern} in {search_path}  ({len(filtered)} files)")
    return trim_text(result, output_limit)


_LANGUAGE_BY_EXT = {
    ".py": "python", ".pyw": "python",
    ".json": "json",
    ".ps1": "powershell", ".psm1": "powershell", ".psd1": "powershell",
    ".js": "node", ".mjs": "node", ".cjs": "node",
    ".ts": "node", ".tsx": "node", ".jsx": "node",
}
_VERIFY_MAX_BYTES = 500_000  # skip files too large for in-process parse


def _verify_python_syntax(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > _VERIFY_MAX_BYTES:
        return True, f"OK: skipped (file too large: {size} bytes)"
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        ast.parse(source, filename=str(path))
        return True, "OK: no syntax errors"
    except SyntaxError as exc:
        return False, f"FAIL: line {exc.lineno}, col {exc.offset}: {exc.msg}"


def _verify_json_syntax(path: Path) -> tuple[bool, str]:
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    if size > _VERIFY_MAX_BYTES:
        return True, f"OK: skipped (file too large: {size} bytes)"
    source = path.read_text(encoding="utf-8", errors="replace")
    try:
        json.loads(source)
        return True, "OK: valid JSON"
    except json.JSONDecodeError as exc:
        return False, f"FAIL: line {exc.lineno}, col {exc.colno}: {exc.msg}"


def _verify_powershell_syntax(path: Path, shell_exe: str) -> tuple[bool, str]:
    # [Parser]::ParseFile only tokenizes/parses an AST — it never invokes the script,
    # so this is as non-destructive as the Python ast.parse() check above.
    escaped = str(path).replace("'", "''")
    script = (
        f"$perr = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{escaped}', [ref]$null, [ref]$perr); "
        f"if ($perr) {{ $perr | ForEach-Object {{ Write-Output $_.Message }}; exit 1 }} "
        f"else {{ Write-Output 'OK' }}"
    )
    try:
        result = subprocess.run(
            [shell_exe, "-NoLogo", "-NoProfile", "-Command", script],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except Exception as exc:
        return True, f"OK: skipped (could not run PowerShell parser: {exc})"
    if result.returncode == 0:
        return True, "OK: no syntax errors"
    return False, f"FAIL: {result.stdout.strip() or result.stderr.strip()}"


def _verify_node_syntax(path: Path) -> tuple[bool, str]:
    node = shutil.which("node")
    if not node:
        return True, f"OK: skipped (no checker available for {path.suffix} — node not found on PATH)"
    try:
        result = subprocess.run(
            [node, "--check", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
        )
    except Exception as exc:
        return True, f"OK: skipped (could not run node --check: {exc})"
    if result.returncode == 0:
        return True, "OK: no syntax errors"
    return False, f"FAIL: {result.stderr.strip() or result.stdout.strip()}"


def verify_syntax_tool(path_text: str, language: str, shell_exe: str) -> str:
    path = resolve_path(path_text)
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    lang = (language or "").strip().lower() or _LANGUAGE_BY_EXT.get(path.suffix.lower(), "")
    if lang == "python":
        ok, detail = _verify_python_syntax(path)
    elif lang == "json":
        ok, detail = _verify_json_syntax(path)
    elif lang == "powershell":
        ok, detail = _verify_powershell_syntax(path, shell_exe)
    elif lang == "node" or path.suffix.lower() in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}:
        ok, detail = _verify_node_syntax(path)
    else:
        ok, detail = True, f"OK: skipped (no syntax checker for '{path.suffix or language or 'unknown'}')"
    ui.tool_event("verify", f"{path}  ({'pass' if ok else 'FAIL'})")
    return detail


def lint_code_tool(path_text: str) -> str:
    if not _RUFF:
        raise RuntimeError("ruff is not on PATH — lint_code is unavailable.")
    path = resolve_path(path_text)
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    try:
        result = subprocess.run(
            [_RUFF, "check", "--output-format=text", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception as exc:
        raise RuntimeError(f"ruff failed: {exc}") from exc
    output = (result.stdout + result.stderr).strip()
    status = "clean" if result.returncode == 0 else f"{result.returncode} issue(s)"
    ui.tool_event("lint", f"{path}  ({status})")
    return output if output else f"OK: no issues in {path}"


_RUN_CODE_INTERPRETERS: dict[str, list[str]] = {
    ".py":  [sys.executable],
    ".ps1": [],           # filled in at call time with shell_exe
    ".js":  ["node"],
    ".mjs": ["node"],
    ".cjs": ["node"],
}


def run_code_tool(
    path_text: str,
    run_args: list[str],
    timeout: int,
    shell_exe: str,
    output_limit: int,
) -> str:
    cwd = Path.cwd().resolve()
    path = resolve_path(path_text)
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    if not path.is_relative_to(cwd):
        raise RuntimeError(
            f"run_code is restricted to files under the working directory ({cwd}). "
            f"Resolved path was: {path}"
        )
    ext = path.suffix.lower()
    if ext in {".js", ".mjs", ".cjs"} and not shutil.which("node"):
        raise RuntimeError("node not found on PATH — cannot run .js/.mjs/.cjs files")
    if ext == ".ps1":
        cmd_prefix = [shell_exe, "-NoLogo", "-NoProfile", "-File"]
    else:
        cmd_prefix = _RUN_CODE_INTERPRETERS.get(ext)
        if cmd_prefix is None:
            raise RuntimeError(
                f"Unsupported extension {ext!r} for run_code. "
                "Allowed: .py .ps1 .js .mjs .cjs"
            )
    cmd = [*cmd_prefix, str(path), *[str(a) for a in run_args]]
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace",
        )
    except (PermissionError, FileNotFoundError, OSError) as exc:
        raise RuntimeError(f"Failed to launch interpreter for {path.name}: {exc}") from exc
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        out = trim_text(stdout, output_limit)
        err = trim_text(stderr, output_limit)
        ui.tool_event("run", f"{path}  (TIMEOUT after {timeout}s)")
        return (
            f"Exit code: TIMEOUT ({timeout}s exceeded)\n\n"
            f"[stdout]\n{out or '(empty)'}\n\n"
            f"[stderr]\n{err or '(empty)'}"
        )
    out = trim_text(stdout, output_limit)
    err = trim_text(stderr, output_limit)
    ui.tool_event("run", f"{path}  (exit {proc.returncode})")
    return (
        f"Exit code: {proc.returncode}\n\n"
        f"[stdout]\n{out or '(empty)'}\n\n"
        f"[stderr]\n{err or '(empty)'}"
    )


def workspace_snapshot(cwd: str) -> str:
    """Return a compact ≤150-token workspace context line prepended to each agent turn."""
    p = Path(cwd)
    parts: list[str] = []

    # Project type detection via marker files
    proj = "dir"
    if (p / "pyproject.toml").exists() or (p / "setup.py").exists() or (p / "requirements.txt").exists():
        proj = "python"
    elif (p / "package.json").exists():
        proj = "node"
    elif (p / "Cargo.toml").exists():
        proj = "rust"
    elif (p / "go.mod").exists():
        proj = "go"
    elif list(p.glob("*.sln")) or list(p.glob("*.csproj")):
        proj = "csharp"
    parts.append(f"workspace:{proj}")

    # Git branch + dirty flag (0.5 s timeout — fast enough, safe on slow NTFS)
    try:
        br = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=0.5,
        )
        if br.returncode == 0:
            branch = br.stdout.strip()
            # --porcelain detects staged, unstaged, and untracked changes in one call;
            # git diff --quiet only detects unstaged changes (misses staged commits).
            status = subprocess.run(
                ["git", "status", "--porcelain"],
                cwd=cwd, capture_output=True, text=True, timeout=0.5,
            )
            dirty = bool(status.stdout.strip())
            parts.append(f"git:{branch}{'*' if dirty else ''}")
    except Exception:
        pass

    # Primary entry point
    for name in ("shellai.py", "main.py", "app.py", "index.js", "main.rs", "main.go", "main.cs"):
        if (p / name).exists():
            parts.append(f"entry:{name}")
            break

    # Test directory
    for tdir in ("tests", "test", "evals", "spec"):
        if (p / tdir).is_dir():
            parts.append(f"tests:{tdir}/")
            break

    tag_line = "[" + " | ".join(parts) + "]"
    rules = memory.read_memory_rules(5)
    if rules:
        return tag_line + "\nPrior knowledge:\n" + "\n".join(f"  {r}" for r in rules)
    return tag_line


def _extract_tools_from_history(history: list[dict[str, str]], last_n: int = 4) -> list[str]:
    """Return tool names used in the last N assistant history messages."""
    tools: list[str] = []
    for msg in history[-last_n:]:
        if msg.get("role") != "assistant":
            continue
        for match in re.finditer(r'"action"\s*:\s*"(\w+)"', msg.get("content", "")):
            tool = match.group(1)
            if tool in TOOL_NAMES:
                tools.append(tool)
    return tools


# ---------------------------------------------------------------------------
# Batch tool implementation
# ---------------------------------------------------------------------------

_BATCH_ALLOWED = frozenset({"read_file", "list_directory", "find_files", "search_files", "search_memory"})
_BATCH_MAX = 8


def _run_batch(config: dict[str, Any], actions: list[Any], shell_exe: str) -> str:
    """Execute read-only tools in parallel; return indexed results (partial failure OK)."""
    if len(actions) > _BATCH_MAX:
        raise RuntimeError(f"batch: max {_BATCH_MAX} actions, got {len(actions)}.")

    results: list[dict[str, Any]] = [{}] * len(actions)

    def run_one(idx: int, act: Any) -> tuple[int, dict[str, Any]]:
        if not isinstance(act, dict):
            return idx, {"error": "action must be a JSON object"}
        tool = str(act.get("tool", "")).strip()
        if tool not in _BATCH_ALLOWED:
            return idx, {"error": f"tool {tool!r} not allowed in batch (read-only tools only)"}
        sub = {"action": "tool", "tool": tool, "args": act.get("args") or {}}
        try:
            output = execute_tool_call(config, sub, shell_exe)
            return idx, {"tool": tool, "result": output}
        except Exception as exc:
            return idx, {"tool": tool, "error": str(exc)}

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        futs = {pool.submit(run_one, i, act): i for i, act in enumerate(actions)}
        for fut in concurrent.futures.as_completed(futs):
            idx, result = fut.result()
            results[idx] = result

    parts: list[str] = []
    for i, r in enumerate(results):
        if not r:
            parts.append(f"[{i}] (no result)")
        elif "error" in r:
            parts.append(f"[{i}] ERROR ({r.get('tool', '?')}): {r['error']}")
        else:
            parts.append(f"[{i}] {r.get('tool', '?')}:\n{r.get('result', '')}")
    return "\n\n---\n\n".join(parts)


# ---------------------------------------------------------------------------
# Delegate sub-agent
# ---------------------------------------------------------------------------

def _run_delegate(config: dict[str, Any], task: str, shell_exe: str) -> str:
    """Run a focused sub-agent with a fresh context. Blocked inside a delegate (no recursion)."""
    global _CURRENT_SESSION_ID, _in_delegate
    if _in_delegate:
        raise RuntimeError("delegate cannot be called from within a delegate (no recursion).")

    parent_sid = _CURRENT_SESSION_ID
    _in_delegate = True
    _CURRENT_SESSION_ID = str(uuid4())

    cprint(f"\n  ⟶ delegate: {task[:100]}", C.BCYAN)
    delegate_config = dict(config)
    delegate_config["max_agent_steps"] = min(int(config.get("max_agent_steps", 15)), 5)
    try:
        result = run_autopilot(
            delegate_config,
            [],
            task,
            shell_exe,
            session=None,
        )
    finally:
        _CURRENT_SESSION_ID = parent_sid
        _in_delegate = False

    cap = 1500
    if len(result) > cap:
        result = result[:cap] + f"\n...[delegate output truncated to {cap} chars]"
    cprint(f"  ⟶ delegate done", C.DIM)
    return result


# ---------------------------------------------------------------------------
# Checkpoints (Feature 17 — /save, /load, /checkpoints)
# ---------------------------------------------------------------------------

def _checkpoint_dir() -> Path:
    return Path.cwd() / ".shellai" / "checkpoints"


def _safe_checkpoint_name(name: str) -> str:
    return re.sub(r"[^\w\-.]", "_", name)[:60]


def _save_checkpoint(name: str, session: dict[str, Any], cwd: str) -> Path:
    cp_dir = _checkpoint_dir()
    cp_dir.mkdir(parents=True, exist_ok=True)
    cp_path = cp_dir / f"{_safe_checkpoint_name(name)}.json"
    cp_data = {
        "name": name,
        "created_at": iso_now(),
        "cwd": cwd,
        "message_count": len(session.get("messages", [])),
        "messages": session.get("messages", []),
        "workspace_metadata": workspace_snapshot(cwd),
    }
    payload = json.dumps(cp_data, indent=2) + "\n"
    tmp = cp_path.with_suffix(".tmp")
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(cp_path)
    return cp_path


def _load_checkpoint(name: str) -> dict[str, Any] | None:
    cp_path = _checkpoint_dir() / f"{_safe_checkpoint_name(name)}.json"
    if not cp_path.exists():
        return None
    try:
        return json.loads(cp_path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _list_checkpoints() -> list[dict[str, Any]]:
    cp_dir = _checkpoint_dir()
    if not cp_dir.exists():
        return []
    checkpoints: list[dict[str, Any]] = []
    for cp_file in sorted(cp_dir.glob("*.json")):
        try:
            data = json.loads(cp_file.read_text(encoding="utf-8"))
            checkpoints.append({
                "name": data.get("name", cp_file.stem),
                "created_at": data.get("created_at", "?"),
                "message_count": data.get("message_count", 0),
                "cwd": data.get("cwd", "?"),
            })
        except Exception:
            pass
    return checkpoints


# ---------------------------------------------------------------------------
# /config, /memory, /profile REPL helpers
# ---------------------------------------------------------------------------

_CONFIG_SETTABLE: dict[str, str] = {
    "model":                          "str",
    "temperature":                    "float",
    "timeout_seconds":                "int",
    "max_output_tokens":              "int",
    "chat_max_output_tokens":         "int",
    "autopilot_max_output_tokens":    "int",
    "compact_max_output_tokens":      "int",
    "max_agent_steps":                "int",
    "tool_output_limit":              "int",
    "stream_delay_ms":                "int",
    "history_retention_days":         "int",
    "use_streaming":                  "bool",
    "telemetry_enabled":              "bool",
    "memory_enabled":                 "bool",
    "autopilot_confirm_destructive":  "bool",
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
    return value


def _handle_config_cmd(query: str, config: dict[str, Any]) -> None:
    parts = query.split(None, 2)
    if len(parts) == 1:
        print()
        for key, kind in sorted(_CONFIG_SETTABLE.items()):
            val = config.get(key, "(unset)")
            print(f"  {key:<42}  {str(val):<18}  [{kind}]")
        print()
        return
    key = parts[1]
    if key not in _CONFIG_SETTABLE:
        cprint(f"  Unknown config key: {key!r}. Run /config to see all settable keys.", C.YELLOW)
        return
    if len(parts) == 2:
        cprint(f"  {key} = {config.get(key, '(unset)')!r}  [{_CONFIG_SETTABLE[key]}]", C.DIM)
        return
    value_str = parts[2]
    try:
        new_val = _coerce_config_value(value_str, _CONFIG_SETTABLE[key])
    except (ValueError, TypeError) as exc:
        cprint(f"  Cannot set {key!r}: {exc}", C.RED)
        return
    config[key] = new_val
    cprint(f"  {key} = {new_val!r}", C.BCYAN)


def _handle_memory_cmd(query: str, config: dict[str, Any]) -> None:
    parts = query.split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else "status"

    if sub == "status":
        enabled = bool(config.get("memory_enabled", True))
        if not enabled:
            cprint("  Memory disabled  (memory_enabled = false).", C.YELLOW)
            return
        meta_path = Path.cwd() / ".shellai" / "vector_store" / "metadata.json"
        if not meta_path.exists():
            cprint("  Memory store: empty (no entries indexed yet).", C.DIM)
            return
        try:
            entries = json.loads(meta_path.read_text(encoding="utf-8"))
            size_kb = meta_path.stat().st_size // 1024
            cprint(f"  Memory store: {len(entries)} entries, ~{size_kb} KB", C.BCYAN)
            if entries:
                oldest = entries[0].get("created_at", "?")[:16]
                newest = entries[-1].get("created_at", "?")[:16]
                cprint(f"  Oldest: {oldest}  →  Newest: {newest}", C.DIM)
        except Exception as exc:
            cprint(f"  Memory store: error reading metadata ({exc})", C.YELLOW)

    elif sub == "list":
        n = 10
        if len(parts) > 2:
            try:
                n = int(parts[2])
            except ValueError:
                pass
        meta_path = Path.cwd() / ".shellai" / "vector_store" / "metadata.json"
        if not meta_path.exists():
            print("  No memory entries.")
            return
        try:
            entries = json.loads(meta_path.read_text(encoding="utf-8"))
            shown = entries[-n:]
            offset = max(0, len(entries) - n)
            print()
            for i, e in enumerate(shown, start=offset + 1):
                ts = e.get("created_at", "?")[:16]
                text = e.get("text", "")[:80]
                tools = ", ".join(e.get("tool_sequence", []) or [])
                print(f"  #{i:>3}  [{ts}]  {text}")
                if tools:
                    print(f"         tools: {tools}")
            print()
        except Exception as exc:
            cprint(f"  Error reading memory: {exc}", C.YELLOW)

    elif sub == "search":
        if len(parts) < 3:
            print("  Usage: /memory search <query>")
            return
        result = memory.search_memory_tool(config, parts[2], top_k=5)
        print(f"\n{result}\n")

    elif sub == "clear":
        confirm = input("  Delete all memory entries? [y/N] ").strip().lower()
        if confirm not in ("y", "yes"):
            print("  Aborted.")
            return
        store_dir = Path.cwd() / ".shellai" / "vector_store"
        deleted: list[str] = []
        for fname in ("vectors.npz", "metadata.json"):
            f = store_dir / fname
            if f.exists():
                try:
                    f.unlink()
                    deleted.append(fname)
                except Exception as exc:
                    cprint(f"  Could not delete {fname}: {exc}", C.YELLOW)
        if deleted:
            cprint(f"  Cleared: {', '.join(deleted)}", C.BCYAN)
        else:
            print("  Nothing to clear.")

    elif sub == "prune":
        removed = memory.prune_memory_rules()
        if removed:
            cprint(f"  Pruned {removed} old rule(s) from memory_rules.md.", C.BCYAN)
        else:
            cprint("  Rules file is within the cap — nothing pruned.", C.DIM)

    else:
        print("  Usage: /memory [status|list [n]|search <query>|clear|prune]")


def _show_profile(config: dict[str, Any], mode: str, session: dict[str, Any]) -> None:
    print()
    cprint("  Hex CLI Profile", C.BOLD)
    print(f"  Version         {VERSION}")
    print(f"  Mode            {mode}")
    backend = config.get("backend", "?")
    print(f"  Backend         {backend}  →  {_backend_url(config)}")
    print(f"  Model           {config.get('model', '?')}")
    print(f"  Temperature     {config.get('temperature', '?')}")
    print(f"  Max steps       {config.get('max_agent_steps', '?')}")
    mem_state = "enabled" if config.get("memory_enabled", True) else "disabled"
    tel_state = "enabled" if config.get("telemetry_enabled", True) else "disabled"
    print(f"  Memory          {mem_state}")
    print(f"  Telemetry       {tel_state}")
    try:
        alive = ping_backend(config)
        status_str = "online" if alive else "offline"
        status_col = C.BGREEN if alive else C.YELLOW
    except Exception:
        status_str, status_col = "unknown", C.DIM
    cprint(f"  Backend status  {status_str}", status_col)
    msgs = session.get("messages", [])
    title = session.get("title", "New Chat")
    est = sum(len(m.get("content", "")) for m in msgs) // 4
    print(f"  Session         {title!r}  ({len(msgs)} messages, ~{est:,} tokens)")
    print()


def execute_tool_call(config: dict[str, Any], action: dict[str, Any], shell_exe: str) -> str:
    tool = str(action.get("tool", "")).strip()
    args = action.get("args")
    if not isinstance(args, dict):
        raise RuntimeError("Tool args must be a JSON object.")
    limit = int(config.get("tool_output_limit", 12000))

    if tool == "run_command":
        cmd = str(args.get("command") or "").strip()
        if not cmd:
            raise RuntimeError("run_command requires 'command'.")
        classification = safety.classify_command(cmd)
        confirm = config.get("autopilot_confirm_destructive", True)
        if classification == "destructive" and confirm:
            if not ui.confirm_destructive_command(cmd):
                safety.append_audit_log(_CURRENT_SESSION_ID, classification, cmd, "blocked")
                return "Blocked by user."
        result = run_command_tool(cmd, shell_exe, limit, timeout=int(config.get("timeout_seconds", DEFAULT_TIMEOUT_SECONDS)))
        # Parse exit code from the first line of run_command_tool output for the audit log.
        exit_code: int | str | None = None
        try:
            first = result.strip().splitlines()[0]
            if first.startswith("Exit code:"):
                exit_code = int(first.split(":", 1)[1].strip())
        except Exception:
            pass
        safety.append_audit_log(_CURRENT_SESSION_ID, classification, cmd, exit_code)
        return result
    if tool == "read_file":
        path = str(args.get("path") or "").strip()
        if not path:
            raise RuntimeError("read_file requires 'path'.")
        return read_file_tool(path, limit)
    if tool == "edit_file":
        path = str(args.get("path") or "").strip()
        old = str(args.get("old_string") or "")
        new = str(args.get("new_string") or "")
        if not path:
            raise RuntimeError("edit_file requires 'path'.")
        if not old:
            raise RuntimeError("edit_file requires 'old_string'.")
        return edit_file_tool(path, old, new)
    if tool == "write_file":
        path = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
        if not path:
            raise RuntimeError("write_file requires 'path'.")
        return write_file_tool(path, content)
    if tool == "append_file":
        path = str(args.get("path") or "").strip()
        content = str(args.get("content") or "")
        if not path:
            raise RuntimeError("append_file requires 'path'.")
        return append_file_tool(path, content)
    if tool == "list_directory":
        path = str(args.get("path") or ".").strip() or "."
        return list_directory_tool(path, limit)
    if tool == "search_files":
        pattern = str(args.get("pattern") or "").strip()
        path = str(args.get("path") or ".").strip() or "."
        glob_pat = str(args.get("glob") or "*").strip() or "*"
        if not pattern:
            raise RuntimeError("search_files requires 'pattern'.")
        return search_files_tool(pattern, path, glob_pat, limit)
    if tool == "find_files":
        glob_pat = str(args.get("glob") or "").strip()
        path = str(args.get("path") or ".").strip() or "."
        if not glob_pat:
            raise RuntimeError("find_files requires 'glob'.")
        return find_files_tool(glob_pat, path, limit)
    if tool == "verify_syntax":
        path = str(args.get("path") or "").strip()
        if not path:
            raise RuntimeError("verify_syntax requires 'path'.")
        language = str(args.get("language") or "").strip()
        return verify_syntax_tool(path, language, shell_exe)
    if tool == "search_memory":
        query_text = str(args.get("query") or "").strip()
        if not query_text:
            raise RuntimeError("search_memory requires 'query'.")
        top_k = max(1, min(int(args.get("top_k") or 3), 10))
        return memory.search_memory_tool(config, query_text, top_k)
    if tool == "run_code":
        path = str(args.get("path") or "").strip()
        if not path:
            raise RuntimeError("run_code requires 'path'.")
        run_args = args.get("args") or []
        if not isinstance(run_args, list):
            run_args = [str(run_args)]
        timeout = max(1, min(int(args.get("timeout") or 10), 60))
        return run_code_tool(path, run_args, timeout, shell_exe, limit)

    if tool == "lint_code":
        path = str(args.get("path") or "").strip()
        if not path:
            raise RuntimeError("lint_code requires 'path'.")
        return lint_code_tool(path)

    if tool == "fetch_url":
        url = str(args.get("url") or "").strip()
        if not url:
            raise RuntimeError("fetch_url requires 'url'.")
        return network.fetch_url(url)

    if tool == "batch":
        actions = args.get("actions")
        if not isinstance(actions, list):
            raise RuntimeError("batch requires 'actions' list.")
        return _run_batch(config, actions, shell_exe)

    if tool == "delegate":
        task = str(args.get("task") or "").strip()
        if not task:
            raise RuntimeError("delegate requires 'task'.")
        return _run_delegate(config, task, shell_exe)

    raise RuntimeError(f"Unknown tool: {tool!r}")


# ---------------------------------------------------------------------------
# /compact
# ---------------------------------------------------------------------------

def compact_history(
    config: dict[str, Any],
    session: dict[str, Any],
    *,
    quiet: bool = False,
) -> list[dict[str, str]]:
    """Summarise the current message history and replace it with a compact version.

    quiet=True suppresses the printed summary (used by auto-compact).
    """
    messages: list[dict[str, str]] = list(session.get("messages", []))
    _COMPACT_KEEP_RECENT = 4
    # Need at least keep_recent+3 messages so that 3+ messages are summarised
    # and removed — otherwise the 2 summary messages + 4 tail can exceed the
    # original count (e.g. 5 msgs → 6 msgs after compact).
    if len(messages) < _COMPACT_KEEP_RECENT + 3:
        print(f"Nothing to compact yet (need at least {_COMPACT_KEEP_RECENT + 3} messages).")
        return messages

    # /no_think disables Qwen3's chain-of-thought block so the token budget
    # goes to the actual summary rather than being consumed by <think> tags.
    summary_messages: list[dict[str, str]] = [
        {"role": "system", "content": COMPACT_SYSTEM_PROMPT},
        *messages,
        {"role": "user", "content": "Produce the compact summary now. /no_think"},
    ]
    compact_tokens = max(512, int(config.get("compact_max_output_tokens", 512)))
    config_with_compact = {**config, "_compact_tokens": compact_tokens}
    summary, _ = call_llm(config_with_compact, summary_messages, "_compact_tokens", label="compacting")
    summary = strip_thinking(summary).strip()

    # Keep the last few messages verbatim so in-progress task state survives compaction.
    tail = messages[-_COMPACT_KEEP_RECENT:] if len(messages) > _COMPACT_KEEP_RECENT else []

    new_messages: list[dict[str, str]] = [
        {
            "role": "user",
            "content": (
                "[Conversation compacted. Summary of prior context:]\n\n"
                + summary
                + "\n\n[Continue from here]"
            ),
        },
        {
            "role": "assistant",
            "content": "Understood. I have the context summary and will continue from where we left off.",
        },
        *tail,
    ]
    session["messages"] = new_messages
    session["compact_count"] = session.get("compact_count", 0) + 1
    touch_session(session)

    n_removed = len(messages) - len(new_messages)
    if not quiet:
        cprint(f"\nCompacted: {len(messages)} → {len(new_messages)} messages (removed ~{n_removed}).", C.BCYAN)
        cprint("Summary:", C.BOLD)
        print(summary)
        print()
    return new_messages


# ---------------------------------------------------------------------------
# Context estimate
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Command generation (command mode)
# ---------------------------------------------------------------------------

def generate_command(config: dict[str, Any], query: str) -> str:
    return extract_command(
        run_cancellable(
            "planning",
            lambda: llm_generate(config, config["system_prompt"], f"User request: {query.strip()}"),
        )
    )


# ---------------------------------------------------------------------------
# Chat mode
# ---------------------------------------------------------------------------

def chat_turn(
    config: dict[str, Any], history: list[dict[str, str]], query: str
) -> dict[str, str]:
    if is_help_request(query):
        return {"message": HELP_TEXT, "command": ""}
    meta = local_meta_response(query, config)
    if meta:
        return {"message": meta, "command": ""}

    msgs: list[dict[str, str]] = [
        {"role": "system", "content": config.get("chat_system_prompt", CHAT_SYSTEM_PROMPT)},
        *history,
        {"role": "user", "content": query.strip()},
    ]
    raw, _ = call_llm(config, msgs, "chat_max_output_tokens", label="thinking")
    return parse_chat_response(raw)


# ---------------------------------------------------------------------------
# Autopilot: multi-step agentic loop
# ---------------------------------------------------------------------------

def run_autopilot(
    config: dict[str, Any],
    history: list[dict[str, str]],
    query: str,
    shell_exe: str,
    session: dict[str, Any] | None = None,
    turn: telemetry.TurnRecorder | None = None,
) -> str:
    global _CURRENT_SESSION_ID
    _CURRENT_SESSION_ID = None  # clear before early-return paths
    if is_help_request(query):
        return HELP_TEXT
    meta = local_meta_response(query, config)
    if meta:
        return meta
    if is_small_talk(query):
        return "Hi — what would you like me to do?"

    # Fresh UUID for this agent loop: lets the npurun server detect
    # continuation turns (messages only appended) and skip reset_dialog(),
    # so Genie re-prefills only the new tokens via SentenceCode::Rewind.
    _CURRENT_SESSION_ID = str(uuid4())

    cwd = str(Path.cwd())
    max_steps = int(config.get("max_agent_steps", 15))
    recent_tools = _extract_tools_from_history(history)
    system_prompt = build_autopilot_prompt(cwd=cwd, max_steps=max_steps, query=query, recent_tools=recent_tools)
    config_system = config.get("autopilot_system_prompt", "").strip()
    if config_system:
        system_prompt = config_system

    ws = workspace_snapshot(cwd)
    user_content = f"{ws}\nWorking directory: {cwd}\n\nRequest: {query.strip()}"
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": user_content},
    ]

    output_limit = int(config.get("tool_output_limit", 12000))
    last_tool_output = ""
    total_eval = 0
    tools_used: list[str] = []
    touched_paths: list[str] = []
    # Snapshot original file content before first mutation per path so /undo
    # can restore the exact pre-turn state. None means file was created fresh.
    _turn_snapshots: dict[str, str | None] = {}
    # Rolling window for error-loop detection: (tool_name, output) tuples.
    _loop_tracker: list[tuple[str, str]] = []

    for step in range(max_steps):
        step_label = "thinking" if step == 0 else f"step {step + 1}/{max_steps}"
        cprint(f"\n  {step_label}...", C.DIM, file=sys.stderr)

        # Up to 2 retries on bad JSON
        raw = ""
        action: dict[str, Any] = {}
        for attempt in range(3):
            llm_start = time.monotonic()
            raw, eval_count = call_llm(
                config, messages, "autopilot_max_output_tokens", label=step_label, json_format=True
            )
            if turn:
                turn.record_llm(time.monotonic() - llm_start, eval_count)
            total_eval += eval_count
            action = parse_agent_action(raw)

            # Retry only if we got a malformed finish on early steps
            if (
                attempt < 2
                and step < 3
                and action["action"] == "finish"
                and any(name in raw for name in TOOL_NAMES)
                and not parse_json_object(raw)
            ):
                messages.append({"role": "assistant", "content": strip_thinking(raw)})
                messages.append({
                    "role": "user",
                    "content": (
                        "Your response was not valid JSON. "
                        "Respond with exactly one JSON object as specified. No prose."
                    ),
                })
                continue
            break

        if action["action"] == "finish":
            msg = action.get("message", "")
            # Nudge once if the model refused to use tools
            if step == 0 and any(phrase in msg.lower() for phrase in REFUSAL_PHRASES):
                messages.append({"role": "assistant", "content": strip_thinking(raw)})
                messages.append({
                    "role": "user",
                    "content": "You have run_command and other tools available. Use them. Output JSON only.",
                })
                continue
            result = msg or last_tool_output or "Done."
            if session and last_tool_output:
                store_observation(session, query, last_tool_output)
            memory.maybe_index_turn(config, query, tools_used, touched_paths, outcome="completed")
            if total_eval:
                cprint(f"\n  (~{total_eval} tokens generated)", C.DIM)
            if session:
                _SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = _turn_snapshots
            return result

        if action["action"] != "tool" or not action.get("tool"):
            if session:
                _SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = _turn_snapshots
            return action.get("message", "") or last_tool_output or "Done."

        tool_name = action["tool"]
        tools_used.append(tool_name)
        tool_path = action.get("args", {}).get("path") if isinstance(action.get("args"), dict) else None
        if tool_path:
            touched_paths.append(str(tool_path))

        # Capture file state before first mutation so /undo can restore it.
        if tool_name in {"edit_file", "write_file", "append_file"} and tool_path:
            try:
                snap_key = str(resolve_path(tool_path))
                if snap_key not in _turn_snapshots:
                    p = Path(snap_key)
                    _turn_snapshots[snap_key] = p.read_text(encoding="utf-8") if p.exists() else None
            except Exception:
                pass

        ui.tool_header(tool_name)
        tool_start = time.monotonic()
        try:
            tool_output = execute_tool_call(config, action, shell_exe)
            if turn:
                turn.record_tool(tool_name, action.get("args", {}), time.monotonic() - tool_start, "ok")
        except (UserCancelled, KeyboardInterrupt):
            if session:
                _SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = _turn_snapshots
            raise
        except Exception as exc:
            tool_output = f"Error: {exc}"
            ui.error_box(str(exc))
            if turn:
                turn.record_tool(tool_name, action.get("args", {}), time.monotonic() - tool_start, "error")

        last_tool_output = tool_output
        # Error-loop detection: if the last 3 (tool, output) pairs are identical,
        # the agent is cycling — stop early rather than burning the full step budget.
        _loop_tracker.append((tool_name, tool_output))
        if len(_loop_tracker) > 3:
            _loop_tracker.pop(0)
        if len(_loop_tracker) == 3 and len(set(_loop_tracker)) == 1:
            cprint("\n  ⚠ Agent appears stuck in a repeat loop (3 identical results). Stopping.", C.BYELLOW)
            if escalate.get_api_key(config):
                try:
                    answer = input(
                        "\n  The agent is stuck. Escalate to Claude cloud? [y/N] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    answer = "n"
                if answer in ("y", "yes"):
                    tool_seq = [t for t, _ in _loop_tracker]
                    suggestion = escalate.escalate(config, messages, tool_seq)
                    cprint("\n── Cloud suggestion ──────────────────────────────────────────────", C.BCYAN)
                    print(suggestion)
                    print()
            else:
                cprint("  (set ANTHROPIC_API_KEY to enable cloud escalation)", C.DIM)
            if session:
                _SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = _turn_snapshots
            return last_tool_output or "Done."
        messages.append({"role": "assistant", "content": strip_thinking(raw)})
        messages.append({"role": "user", "content": f"Tool output:\n{trim_text(tool_output, output_limit)}"})

    if session and last_tool_output:
        store_observation(session, query, last_tool_output)
    if total_eval:
        cprint(f"\n  (~{total_eval} tokens generated, hit step limit)", C.DIM)
    if session:
        _SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = _turn_snapshots
    return last_tool_output or "Done."


# ---------------------------------------------------------------------------
# Command execution UI
# ---------------------------------------------------------------------------

def copy_to_clipboard(command: str) -> None:
    subprocess.run(["clip.exe"], input=command, text=True, check=True)


def execute_command(command: str, shell_exe: str) -> int:
    return subprocess.run([shell_exe, "-NoLogo", "-NoProfile", "-Command", command]).returncode


def prompt_for_action() -> str:
    while True:
        choice = input("[E]xecute, [C]opy, [A]bort? ").strip().lower()
        if choice in {"e", "execute", "c", "copy", "a", "abort"}:
            return choice[:1]
        print("Please choose E, C, or A.")


def act_on_command(
    command: str, shell_exe: str, force_copy: bool, force_execute: bool
) -> int:
    print()
    cprint("Suggested command:", C.BOLD)
    cprint(f"\n  {command}\n", C.BGREEN)
    if force_copy:
        copy_to_clipboard(command)
        print("Copied.")
        return 0
    if force_execute:
        return execute_command(command, shell_exe)
    choice = prompt_for_action()
    if choice == "c":
        copy_to_clipboard(command)
        print("Copied.")
        return 0
    if choice == "e":
        return execute_command(command, shell_exe)
    print("Aborted.")
    return 0


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# One-shot entry points
# ---------------------------------------------------------------------------

def one_shot_autopilot(config: dict[str, Any], query: str, shell_exe: str) -> int:
    sessions = load_history_store(config)
    session = create_session()
    append_session_message(session, "user", query)
    tel = telemetry.SessionTelemetry(config)
    turn = tel.start_turn("autopilot", query)
    message = run_autopilot(config, [], query, shell_exe, session=session, turn=turn)
    tel.record_turn(turn)
    append_session_message(session, "assistant", message)
    sync_session_store(sessions, session)
    render_result("Result", message)
    return 0


def one_shot_command_mode(
    config: dict[str, Any], query: str, shell_exe: str, args: argparse.Namespace
) -> int:
    sessions = load_history_store(config)
    session = create_session()
    append_session_message(session, "user", query)
    tel = telemetry.SessionTelemetry(config)
    turn = tel.start_turn("command", query)
    llm_start = time.monotonic()
    command = generate_command(config, query)
    turn.record_llm(time.monotonic() - llm_start)
    tel.record_turn(turn)
    append_session_message(session, "assistant", f"Suggested command: {command}")
    sync_session_store(sessions, session)
    return act_on_command(command, shell_exe, args.copy, args.execute)


# ---------------------------------------------------------------------------
# Context warning
# ---------------------------------------------------------------------------

# Eval showed Rule 15 degradation onset at ~2,600 total input tokens, of which
# the system prompt accounts for ~1,200 tokens. Auto-compact when session history
# alone approaches that margin so the user never manually hits the cliff.
# Recalibrated for v1.3: base prompt ~1,000 tokens (search_memory/fetch_url/batch are
# conditional). Warn at 1,300 history tokens → total ~2,450 (under 2,600 cliff).
_CONTEXT_WARN_TOKENS = 1_300
_CONTEXT_CRIT_TOKENS = 1_600


def _maybe_auto_compact(
    config: dict[str, Any],
    session: dict[str, Any],
    sessions: list[dict[str, Any]],
) -> None:
    """Silently compact history when it approaches the 4B instruction-following cliff.

    Fires after each autopilot turn. At ≥1,300 history tokens the compact runs
    automatically so TTFT never crosses the degradation threshold. The full
    summary is suppressed (quiet=True); only a one-line notice is printed.
    """
    msgs = session.get("messages", [])
    est = sum(len(m.get("content", "")) for m in msgs) // 4
    if est < _CONTEXT_WARN_TOKENS:
        return
    label = f"~{est:,} tokens"
    if est >= _CONTEXT_CRIT_TOKENS:
        label += " — past degradation threshold"
    cprint(f"  Context {label} — auto-compacting…", C.BCYAN)
    try:
        compact_history(config, session, quiet=True)
        sync_session_store(sessions, session)
        n_after = len(session.get("messages", []))
        cprint(f"  Auto-compacted. {n_after} active messages; past context indexed to memory.", C.DIM)
    except UserCancelled:
        cprint("  Auto-compact cancelled. Run /compact manually when ready.", C.YELLOW)
    except Exception as exc:  # noqa: BLE001
        cprint(f"  Auto-compact failed ({exc}). Run /compact manually.", C.YELLOW)


# ---------------------------------------------------------------------------
# REPL
# ---------------------------------------------------------------------------

def run_repl(config: dict[str, Any], initial_mode: str = "autopilot") -> int:
    shell_exe = detect_shell(str(config.get("shell_exe", "") or ""))
    sessions = load_history_store(config)
    current_session = create_session()
    mode = initial_mode
    tel = telemetry.SessionTelemetry(config)

    ui.print_banner(str(config.get("model", "?")), str(config.get("backend", "ollama")), mode)
    memory.start_dreaming(lambda: config, llm_generate)

    while True:
        prompt = repl_prompt(config, mode)
        try:
            query = input(prompt).strip()
        except EOFError:
            print()
            sync_session_store(sessions, current_session)
            return 0
        except KeyboardInterrupt:
            print()
            continue

        memory.touch_last_turn()

        if not query:
            continue

        norm = normalize_text(query)

        # ── exit ──────────────────────────────────────────────────────────
        if norm in {"/exit", "/quit"}:
            sync_session_store(sessions, current_session)
            return 0

        # ── help / tools ──────────────────────────────────────────────────
        if norm == "/help":
            print(f"\n{HELP_TEXT}\n")
            continue
        if norm == "/tools":
            print(f"\n{TOOLS_HELP}\n")
            continue

        # ── history ───────────────────────────────────────────────────────
        if norm == "/history":
            sync_session_store(sessions, current_session)
            sessions = load_history_store(config)
            render_history_list(sessions, str(current_session.get("id", "")))
            continue

        # ── new / clear ───────────────────────────────────────────────────
        if norm in {"/new", "/clear"}:
            sync_session_store(sessions, current_session)
            current_session = create_session()
            cprint("New session started.", C.DIM)
            continue

        # ── resume ────────────────────────────────────────────────────────
        if norm in {"/resume", "/open"} or norm.startswith("/resume ") or norm.startswith("/open "):
            sync_session_store(sessions, current_session)
            sessions = load_history_store(config)
            parts = norm.split()
            if len(parts) != 2 or not parts[1].isdigit():
                print("Usage: /resume <number>")
                continue
            idx = int(parts[1]) - 1
            if idx < 0 or idx >= len(sessions):
                cprint("No session with that number.", C.YELLOW)
                continue
            current_session = sessions[idx]
            cprint(f"Resumed: {current_session['title']}", C.BCYAN)
            continue

        # ── compact ───────────────────────────────────────────────────────
        if norm == "/compact":
            try:
                new_msgs = compact_history(config, current_session)
                sync_session_store(sessions, current_session)
            except UserCancelled:
                print("\nCancelled.\n")
            except Exception as exc:  # noqa: BLE001
                ui.error_box(str(exc))
                if DEBUG:
                    raise
            continue

        # ── undo ──────────────────────────────────────────────────────────
        if norm == "/undo":
            msgs: list[dict[str, str]] = current_session.get("messages", [])
            if len(msgs) >= 2:
                current_session["messages"] = msgs[:-2]
                touch_session(current_session)
                # Restore any files mutated during the last agentic turn.
                snapshots = _SESSION_UNDO_SNAPSHOTS.pop(current_session.get("id", ""), {})
                if snapshots:
                    restored: list[str] = []
                    failed: list[str] = []
                    for path_str, original in snapshots.items():
                        try:
                            p = Path(path_str)
                            if original is None:
                                if p.exists():
                                    p.unlink()
                                restored.append(f"deleted {p.name}")
                            else:
                                tmp_p = p.parent / (p.name + ".tmp")
                                tmp_p.write_text(original, encoding="utf-8")
                                tmp_p.replace(p)
                                restored.append(p.name)
                        except Exception as exc:
                            failed.append(f"{Path(path_str).name}: {exc}")
                    if restored:
                        cprint(f"  Files restored: {', '.join(restored)}", C.BCYAN)
                    if failed:
                        cprint(f"  Could not restore: {', '.join(failed)}", C.YELLOW)
                sync_session_store(sessions, current_session)
                cprint("Removed last exchange.", C.DIM)
            elif len(msgs) == 1:
                current_session["messages"] = []
                touch_session(current_session)
                _SESSION_UNDO_SNAPSHOTS.pop(current_session.get("id", ""), None)
                cprint("Removed last message.", C.DIM)
            else:
                print("Nothing to undo.")
            continue

        # ── context ───────────────────────────────────────────────────────
        if norm == "/context":
            show_context(current_session, config)
            continue

        # ── models ────────────────────────────────────────────────────────
        if norm == "/models":
            render_models(config)
            continue

        # ── mode ──────────────────────────────────────────────────────────
        if norm in {"/mode autopilot", "/mode agent"}:
            mode = "autopilot"
            cprint("Mode: autopilot", C.DIM)
            continue
        if norm == "/mode chat":
            mode = "chat"
            cprint("Mode: chat", C.DIM)
            continue
        if norm == "/mode command":
            mode = "command"
            cprint("Mode: command", C.DIM)
            continue
        if norm == "/mode" or norm.startswith("/mode "):
            cprint("Usage: /mode autopilot|chat|command", C.YELLOW)
            continue

        # ── model ─────────────────────────────────────────────────────────
        if norm.startswith("/model "):
            new_model = query.strip()[len("/model "):].strip()
            if new_model:
                config["model"] = new_model
                cprint(f"Model: {new_model}", C.BCYAN)
            else:
                cprint(f"Current model: {config.get('model', 'unknown')}", C.DIM)
            continue

        # ── cwd ───────────────────────────────────────────────────────────
        if norm == "/cwd" or norm.startswith("/cwd "):
            parts_cwd = query.strip().split(None, 1)
            if len(parts_cwd) == 2:
                new_path = parts_cwd[1].strip()
                try:
                    os.chdir(resolve_path(new_path))
                    cprint(f"cwd: {Path.cwd()}", C.BCYAN)
                except Exception as exc:
                    cprint(f"Cannot change to '{new_path}': {exc}", C.RED)
            else:
                cprint(f"cwd: {Path.cwd()}", C.DIM)
            continue

        # ── config ────────────────────────────────────────────────────────
        if norm == "/config" or norm.startswith("/config "):
            _handle_config_cmd(query.strip(), config)
            continue

        # ── memory ────────────────────────────────────────────────────────
        if norm == "/memory" or norm.startswith("/memory "):
            _handle_memory_cmd(query.strip(), config)
            continue

        # ── profile ───────────────────────────────────────────────────────
        if norm == "/profile":
            _show_profile(config, mode, current_session)
            continue

        # ── checkpoints ───────────────────────────────────────────────────
        if norm == "/checkpoints":
            cps = _list_checkpoints()
            if not cps:
                print("  No checkpoints saved.")
            else:
                print()
                for cp in cps:
                    ts = cp["created_at"][:16] if len(cp.get("created_at", "")) >= 16 else cp.get("created_at", "?")
                    cprint(
                        f"  {cp['name']:<24}  {ts}  ({cp['message_count']} messages)  {cp['cwd']}",
                        C.DIM,
                    )
                print()
            continue

        if norm.startswith("/save "):
            cp_name = query.strip()[len("/save "):].strip()
            if not cp_name:
                print("  Usage: /save <name>")
            else:
                try:
                    cp_path = _save_checkpoint(cp_name, current_session, str(Path.cwd()))
                    cprint(f"  Checkpoint saved: {cp_path.name}", C.BCYAN)
                except Exception as exc:
                    cprint(f"  Could not save checkpoint: {exc}", C.YELLOW)
            continue

        if norm.startswith("/load "):
            cp_name = query.strip()[len("/load "):].strip()
            if not cp_name:
                print("  Usage: /load <name>")
                continue
            cp_data = _load_checkpoint(cp_name)
            if cp_data is None:
                cprint(f"  No checkpoint named '{cp_name}' found.", C.YELLOW)
                continue
            msgs = current_session.get("messages", [])
            if msgs:
                try:
                    confirm = input(
                        f"  Load '{cp_name}'? This will replace {len(msgs)} current message(s). [y/N] "
                    ).strip().lower()
                except (EOFError, KeyboardInterrupt):
                    confirm = "n"
                if confirm not in ("y", "yes"):
                    print("  Aborted.")
                    continue
            current_session["messages"] = cp_data.get("messages", [])
            touch_session(current_session)
            sync_session_store(sessions, current_session)
            n = len(current_session["messages"])
            cprint(f"  Loaded checkpoint '{cp_name}' ({n} messages).", C.BCYAN)
            continue

        # ── dispatch to mode ──────────────────────────────────────────────
        history: list[dict[str, str]] = current_session.get("messages", [])

        if mode == "command":
            turn = tel.start_turn("command", query)
            try:
                llm_start = time.monotonic()
                command = generate_command(config, query)
                turn.record_llm(time.monotonic() - llm_start)
                act_on_command(command, shell_exe, False, False)
                append_session_message(current_session, "user", query)
                append_session_message(current_session, "assistant", f"Command: {command}")
                sync_session_store(sessions, current_session)
                tel.record_turn(turn)
            except (UserCancelled, KeyboardInterrupt):
                print("\nCancelled.\n")
                tel.record_turn(turn, status="cancelled")
            except Exception as exc:  # noqa: BLE001
                ui.error_box(str(exc))
                tel.record_turn(turn, status="error")
                if DEBUG:
                    raise
            continue

        if mode == "chat":
            turn = tel.start_turn("chat", query)
            try:
                llm_start = time.monotonic()
                response = chat_turn(config, history, query)
                turn.record_llm(time.monotonic() - llm_start)
                render_result("Answer", response["message"])
                append_session_message(current_session, "user", query)
                assistant_content = response["message"]
                if response["command"]:
                    act_on_command(response["command"], shell_exe, False, False)
                    assistant_content += f"\nCommand: {response['command']}"
                append_session_message(current_session, "assistant", assistant_content)
                sync_session_store(sessions, current_session)
                tel.record_turn(turn)
            except (UserCancelled, KeyboardInterrupt):
                print("\nCancelled.\n")
                tel.record_turn(turn, status="cancelled")
            except Exception as exc:  # noqa: BLE001
                ui.error_box(str(exc))
                tel.record_turn(turn, status="error")
                if DEBUG:
                    raise
            continue

        # autopilot
        turn = tel.start_turn("autopilot", query)
        try:
            message = run_autopilot(config, history, query, shell_exe, session=current_session, turn=turn)
            render_result("Result", message)
            append_session_message(current_session, "user", query)
            append_session_message(current_session, "assistant", message)
            sync_session_store(sessions, current_session)
            tel.record_turn(turn)
            _maybe_auto_compact(config, current_session, sessions)
        except (UserCancelled, KeyboardInterrupt):
            print("\nCancelled.\n")
            tel.record_turn(turn, status="cancelled")
        except urllib.error.URLError:
            if not ping_backend(config):
                cprint(f"\n  Backend at {_backend_url(config)} is not responding.", C.BRED)
                cprint("  Restart it with: python launcher.py", C.DIM)
            else:
                ui.error_box("Network error — backend returned an unexpected response.")
            tel.record_turn(turn, status="error")
        except (ConnectionResetError, ConnectionAbortedError):
            ui.error_box(
                "npurun dropped the stream connection.\n"
                'Add  "use_streaming": false  to shellai.json to avoid this.'
            )
            tel.record_turn(turn, status="error")
        except Exception as exc:  # noqa: BLE001
            ui.error_box(str(exc))
            tel.record_turn(turn, status="error")
            if DEBUG:
                raise


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

DEBUG = False


def main() -> int:
    global DEBUG
    args = parse_args()

    if args.version:
        print(f"Hex CLI {VERSION}")
        return 0

    if args.update:
        return distribution.update(APP_DIR)

    if args.uninstall:
        return distribution.uninstall(APP_DIR)

    if args.raw:
        ui.set_color_enabled(False)
    DEBUG = args.debug
    if args.yolo:
        config_overrides = {"autopilot_confirm_destructive": False}
    else:
        config_overrides = {}

    config_path = Path(args.config).expanduser().resolve()
    config = load_config(config_path)
    config = deep_merge(config, config_overrides)

    # Advisory process lock — warns if another shellai instance is already running.
    lock_warning = lockfile.acquire(Path.cwd() / ".shellai")
    if lock_warning:
        cprint(lock_warning, C.YELLOW)

    if args.backend:
        config["backend"] = args.backend
    if args.model:
        config["model"] = args.model
    if args.fast:
        config["use_streaming"] = False

    if args.print_config:
        print(json.dumps(config, indent=2))
        return 0

    memory.set_local_model_path(APP_DIR / "onnx" / "model_qint8_arm64.onnx")
    distribution.first_run_check(APP_DIR)

    query = " ".join(args.query).strip()
    shell_exe = detect_shell(str(config.get("shell_exe", "") or ""))

    try:
        if not query:
            return run_repl(config, initial_mode=args.mode)
        if args.command_only or args.mode == "command":
            return one_shot_command_mode(config, query, shell_exe, args)
        if args.mode == "chat":
            sessions = load_history_store(config)
            session = create_session()
            append_session_message(session, "user", query)
            tel = telemetry.SessionTelemetry(config)
            turn = tel.start_turn("chat", query)
            llm_start = time.monotonic()
            response = chat_turn(config, [], query)
            turn.record_llm(time.monotonic() - llm_start)
            tel.record_turn(turn)
            append_session_message(session, "assistant", response["message"])
            sync_session_store(sessions, session)
            render_result("Answer", response["message"])
            if response["command"]:
                act_on_command(response["command"], shell_exe, args.copy, args.execute)
            return 0
        return one_shot_autopilot(config, query, shell_exe)
    except (UserCancelled, KeyboardInterrupt):
        cprint("Cancelled.", C.YELLOW, file=sys.stderr)
        return 130
    except urllib.error.HTTPError as error:
        if error.code == 404:
            model = config.get("model", "unknown")
            ui.error_box(f"Model '{model}' not found. Pull it with:  ollama pull {model}")
        else:
            ui.error_box(f"Backend error {error.code}: {error.reason}")
        if DEBUG:
            raise
        return 2
    except urllib.error.URLError:
        if not ping_backend(config):
            ui.error_box(
                f"Backend at {_backend_url(config)} is not responding.\n"
                "Restart it with: python launcher.py"
            )
        else:
            ui.error_box("Network error — backend returned an unexpected response.")
        if DEBUG:
            raise
        return 2
    except (ConnectionResetError, ConnectionAbortedError):
        ui.error_box(
            "npurun dropped the stream connection.\n"
            'Add  "use_streaming": false  to shellai.json to avoid this.'
        )
        if DEBUG:
            raise
        return 2
    except Exception as error:  # noqa: BLE001
        ui.error_box(str(error))
        if DEBUG:
            raise
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
