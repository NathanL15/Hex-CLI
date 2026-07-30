#!/usr/bin/env python3
"""hexcli.ui — presentation layer for Hex CLI.

Pure rendering/formatting: no imports from hexcli.agent (one-way dependency,
hexcli.agent -> hexcli.ui). Functions here take plain data (dicts, strings,
lists) rather than calling back into the data/backend layer.
"""
from __future__ import annotations

import subprocess
import sys
import textwrap
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

_COLOR_ON = sys.stdout.isatty() and __import__("os").environ.get("NO_COLOR") is None


class C:
    RESET   = "\033[0m"   if _COLOR_ON else ""
    BOLD    = "\033[1m"   if _COLOR_ON else ""
    DIM     = "\033[2m"   if _COLOR_ON else ""
    RED     = "\033[31m"  if _COLOR_ON else ""
    GREEN   = "\033[32m"  if _COLOR_ON else ""
    YELLOW  = "\033[33m"  if _COLOR_ON else ""
    BLUE    = "\033[34m"  if _COLOR_ON else ""
    MAGENTA = "\033[35m"  if _COLOR_ON else ""
    CYAN    = "\033[36m"  if _COLOR_ON else ""
    GRAY    = "\033[90m"  if _COLOR_ON else ""
    BRED    = "\033[91m"  if _COLOR_ON else ""
    BGREEN  = "\033[92m"  if _COLOR_ON else ""
    BYELLOW = "\033[93m"  if _COLOR_ON else ""
    BBLUE   = "\033[94m"  if _COLOR_ON else ""
    BMAGENTA = "\033[95m" if _COLOR_ON else ""
    BCYAN   = "\033[96m"  if _COLOR_ON else ""
    BWHITE  = "\033[97m"  if _COLOR_ON else ""


def cprint(text: str, color: str = "", bold: bool = False, file: Any = None) -> None:
    prefix = (C.BOLD if bold else "") + color
    suffix = C.RESET if prefix else ""
    print(f"{prefix}{text}{suffix}", file=file or sys.stdout)


def set_color_enabled(enabled: bool) -> None:
    """Force ANSI styling on/off, overriding the isatty/NO_COLOR autodetect.

    Used by shellai.py's --raw flag, which must take effect before any
    output is printed.
    """
    global _COLOR_ON
    _COLOR_ON = enabled
    codes = {
        "RESET": "\033[0m", "BOLD": "\033[1m", "DIM": "\033[2m",
        "RED": "\033[31m", "GREEN": "\033[32m", "YELLOW": "\033[33m",
        "BLUE": "\033[34m", "MAGENTA": "\033[35m", "CYAN": "\033[36m",
        "GRAY": "\033[90m", "BRED": "\033[91m", "BGREEN": "\033[92m",
        "BYELLOW": "\033[93m", "BBLUE": "\033[94m", "BMAGENTA": "\033[95m",
        "BCYAN": "\033[96m", "BWHITE": "\033[97m",
    }
    for name, code in codes.items():
        setattr(C, name, code if enabled else "")


# ---------------------------------------------------------------------------
# Spinner
# ---------------------------------------------------------------------------

class Spinner:
    _FRAMES = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"

    def __init__(self, label: str) -> None:
        self.label = label
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._spin, daemon=True)

    def _spin(self) -> None:
        i = 0
        while not self._stop.wait(0.08):
            frame = self._FRAMES[i % len(self._FRAMES)]
            sys.stderr.write(f"\r{C.BCYAN}{frame}{C.RESET} {C.DIM}{self.label}...{C.RESET}")
            sys.stderr.flush()
            i += 1

    def __enter__(self) -> Spinner:
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1)
        sys.stderr.write("\r" + " " * (len(self.label) + 8) + "\r")
        sys.stderr.flush()


# ---------------------------------------------------------------------------
# Tool / error / command event rendering
# ---------------------------------------------------------------------------

def tool_event(tag: str, detail: str) -> None:
    cprint(f"{C.GRAY}▸{C.RESET} {C.DIM}[{tag}] {detail}{C.RESET}")


def tool_header(tool_name: str) -> None:
    cprint(f"\n{C.BMAGENTA}◆{C.RESET} {C.BOLD}{C.BCYAN}{tool_name}{C.RESET}")


def command_echo(command: str) -> None:
    cprint(f"\n{C.GRAY}${C.RESET} {C.GREEN}{command}{C.RESET}\n")


def error_box(message: str, *, file: Any = None) -> None:
    lines = str(message).strip().splitlines() or [""]
    width = min(max(len(ln) for ln in lines) + 4, 100)
    out = file or sys.stderr
    cprint("┌" + "─" * width, C.RED, file=out)
    for ln in lines:
        cprint(f"│ {ln}", C.RED, file=out)
    cprint("└" + "─" * width, C.RED, file=out)


def print_banner(model: str, backend: str, mode: str) -> None:
    title = "HEX CLI"
    width = max(len(title) + 4, 44)
    print()
    cprint("┌" + "─" * width + "┐", C.BCYAN)
    cprint("│" + title.center(width) + "│", C.BOLD + C.BCYAN)
    cprint("└" + "─" * width + "┘", C.BCYAN)
    cprint(
        f"  model: {C.BWHITE}{model}{C.RESET}{C.DIM}  backend: {backend}  "
        f"mode: {mode}  /help for commands  Esc cancels{C.RESET}",
        C.DIM,
    )
    print()


# ---------------------------------------------------------------------------
# Help text
# ---------------------------------------------------------------------------

HELP_TEXT = textwrap.dedent("""
    Hex CLI  —  local Hexagon NPU terminal agent

    MODES:
      autopilot   full agent with tools, loops until done  (default)
      chat        conversational, suggests a command when helpful
      command     generate one PowerShell command

    SLASH COMMANDS:
      /help                         this help
      /history                      list saved sessions
      /new                          start a new session
      /resume <n>                   resume session #n from /history
      /clear                        clear the current session (keep in history)
      /compact                      summarise + compress history (saves context)
      /undo                         remove last exchange; restores files if the turn wrote any
      /context                      show estimated context usage
      /models                       list available Ollama models
      /mode autopilot|chat|command  switch mode
      /model <name>                 switch model  e.g. /model qwen2.5-coder:14b
      /cwd [path]                   show or change working directory
      /config [key [value]]         view or set runtime config  e.g. /config temperature 0.2
      /memory [status|list|search|clear|prune]  inspect or manage the memory store
      /profile                      show backend, model, session and memory status
      /save <name>                  save a named checkpoint of the current session
      /load <name>                  restore a checkpoint into the current session
      /checkpoints                  list all saved checkpoints
      /tools                        list agent tools
      /exit  /quit                  exit
      Esc                           cancel the current agent step

    AGENT TOOLS (autopilot):
      run_command     read_file      edit_file      write_file    append_file
      list_directory  search_files   find_files     run_code      verify_syntax
      lint_code       search_memory  fetch_url      batch         delegate
      (/tools for full signatures)

    NPU NOTE:
      Primary path: npurun + qwen3-4b-instruct-2507 on the Hexagon NPU (~15 tok/s).
      Fallback: Phi-4-mini via DirectML (Adreno GPU) or Ollama on CPU.
      See README.md for setup. launcher.py auto-selects the best available backend.

    GOOD MODELS (ollama pull <model>):
      qwen2.5-coder:7b    ~4 GB   best default for agent tasks
      qwen2.5-coder:14b   ~8 GB   better reasoning, slower
      qwen2.5-coder:3b    ~2 GB   fast one-liners
      qwen2.5:7b          ~4 GB   good for non-coding questions
      deepseek-r1:7b      ~4 GB   strong reasoning, strips <think> tags
""").strip()

TOOLS_HELP = textwrap.dedent("""
    Tools available in autopilot mode:
      run_command(command)                        Run a PowerShell command (safety-classified).
      read_file(path)                             Read a file's full contents.
      edit_file(path, old_string, new_string)     Replace a string in a file (undo snapshot).
      write_file(path, content)                   Write or overwrite a file (undo snapshot).
      append_file(path, content)                  Append text to a file.
      list_directory(path)                        List files and folders.
      search_files(pattern, path, glob)           Grep — search across files by content.
      find_files(glob, path)                      Find files by glob pattern.
      verify_syntax(path, language)               Non-destructive syntax check (.py .json .ps1 .js).
      run_code(path, args, timeout)               Execute a script in a sandboxed subprocess.
      lint_code(path)                             Run ruff/pylint/eslint and return findings.
      search_memory(query, top_k)                 Recall relevant prior session context.
      fetch_url(url)                              Fetch a URL (GET, optional headers).
      batch(actions)                              Run up to 8 read-only tools in parallel.
      delegate(task)                              Spawn a focused sub-agent (max 5 steps).
""").strip()


# ---------------------------------------------------------------------------
# History list
# ---------------------------------------------------------------------------

def _utc_now() -> datetime:
    return datetime.now(UTC)


def format_relative_time(timestamp: str) -> str:
    try:
        moment = datetime.fromisoformat(timestamp)
    except ValueError:
        return "unknown"
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    seconds = max(0, int((_utc_now() - moment).total_seconds()))
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m ago"
    if seconds < 86400:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def truncate_summary(text: str, width: int) -> str:
    return text if len(text) <= width else text[: width - 3] + "..."


def render_history_list(sessions: list[dict[str, Any]], current_id: str) -> None:
    if not sessions:
        print("\nNo saved chats.\n")
        return
    print()
    header = f"{'#':<4}{'Summary':<52}{'Modified':<12}{'Created'}"
    cprint(header, C.BOLD)
    cprint("─" * len(header), C.DIM)
    for i, s in enumerate(sessions, start=1):
        marker = "▶" if s.get("id") == current_id else " "
        summary = truncate_summary(str(s.get("title", "New Chat")), 48)
        modified = format_relative_time(str(s.get("modified_at", "")))
        created = format_relative_time(str(s.get("created_at", "")))
        compact = s.get("compact_count", 0)
        compact_str = f" [c×{compact}]" if compact else ""
        color = C.BCYAN if s.get("id") == current_id else ""
        cprint(f"{marker} {i:>2}. {summary:<48}  {modified:<10}  {created}{compact_str}", color)
    print()


# ---------------------------------------------------------------------------
# Models list
# ---------------------------------------------------------------------------

def render_models(models: list[dict[str, Any]], current: str) -> None:
    if not models:
        print("No models installed. Pull one: ollama pull qwen2.5-coder:7b")
        return
    print()
    cprint("Available Ollama models:", C.BOLD)
    for m in models:
        name = m.get("name", "")
        size_bytes = m.get("size", 0)
        size_gb = size_bytes / 1e9
        marker = "▶ " if name == current else "  "
        color = C.BCYAN if name == current else ""
        cprint(f"{marker}{name:<36}  {size_gb:.1f} GB", color)
    print()


def render_models_error(exc: BaseException) -> None:
    cprint(f"Could not reach Ollama: {exc}", C.RED)


# ---------------------------------------------------------------------------
# Context estimate
# ---------------------------------------------------------------------------

def show_context(session: dict[str, Any], config: dict[str, Any]) -> None:
    messages: list[dict[str, str]] = session.get("messages", [])
    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = total_chars // 4
    compact_count = session.get("compact_count", 0)
    print()
    cprint("Context estimate", C.BOLD)
    print(f"  Messages:         {len(messages)}")
    print(f"  Chars (total):    {total_chars:,}")
    print(f"  Tokens (est.):    ~{est_tokens:,}")
    print(f"  Compact runs:     {compact_count}")
    print(f"  Max agent steps:  {config.get('max_agent_steps', 15)}")
    print(f"  Model:            {config.get('model', 'unknown')}")
    print(f"  Backend:          {config.get('backend', 'ollama')}")
    if est_tokens >= 1600:
        cprint("  ✗ Past 4B degradation threshold — auto-compact will fire after the next turn.", C.BRED)
    elif est_tokens >= 1300:
        cprint("  ⚠ Approaching 4B degradation threshold — auto-compact fires after this turn.", C.BYELLOW)
    print()


# ---------------------------------------------------------------------------
# REPL prompt
# ---------------------------------------------------------------------------

def get_git_branch() -> str | None:
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            stderr=subprocess.DEVNULL,
            timeout=2,
        )
        branch = out.decode().strip()
        return branch if branch and branch != "HEAD" else None
    except Exception:
        return None


def short_cwd() -> str:
    cwd = Path.cwd()
    home = Path.home()
    try:
        rel = cwd.relative_to(home)
        return "~\\" + str(rel) if str(rel) != "." else "~"
    except ValueError:
        return str(cwd)


def repl_prompt(config: dict[str, Any], mode: str) -> str:
    model = str(config.get("model", "?"))
    cwd_str = short_cwd()
    branch = get_git_branch()
    branch_str = f" ({branch})" if branch else ""
    if _COLOR_ON:
        return (
            f"{C.DIM}[{C.BCYAN}{model}{C.DIM} | {C.BGREEN}{mode}{C.DIM} | "
            f"{C.BYELLOW}{cwd_str}{branch_str}{C.DIM}]{C.RESET}\n"
            f"{C.BOLD}you>{C.RESET} "
        )
    return f"[{model} | {mode} | {cwd_str}{branch_str}]\nyou> "


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------

def confirm_destructive_command(cmd: str) -> bool:
    """Print a destructive-command warning and return True only if the user types y/yes."""
    print()
    cprint("⚠  Agent wants to run a destructive command:", C.BYELLOW, bold=True)
    cprint(f"   {cmd}", C.RED)
    print()
    try:
        answer = input("Allow? [y/N] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    return answer in {"y", "yes"}


def render_result(title: str, body: str) -> None:
    print()
    cprint(f"── {title} ", C.BOLD + C.BCYAN)
    cprint("─" * 60, C.DIM)
    print(body)
    print()
