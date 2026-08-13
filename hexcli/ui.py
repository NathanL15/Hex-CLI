#!/usr/bin/env python3
"""hexcli.ui — presentation layer for Hex CLI.

Pure rendering/formatting: no imports from hexcli.agent (one-way dependency,
hexcli.agent -> hexcli.ui). Functions here take plain data (dicts, strings,
lists) rather than calling back into the data/backend layer.
"""
from __future__ import annotations

import msvcrt
import subprocess
import sys
import textwrap
import threading
import time
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
      /new                          start a new session (current one stays in history)
      /resume <n>                   resume session #n from /history
      /clear                        clear the screen
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
      /diff                         show what the agent changed this turn
      /stats                        turns, elapsed time, tokens, tool usage
      /doctor                       diagnose the installation
      /tools                        list agent tools
      /exit  /quit                  exit

    CUSTOM COMMANDS:
      Drop a .md file in .shellai/commands/ (project) or ~/.shellai/commands/
      (global) — /<filename> runs its content as a prompt. $ARGUMENTS in the
      file is replaced with whatever you type after the command; without the
      placeholder, arguments are appended. Built-ins always win name clashes.

    KEYS:
      Up / Down                     history (prefix-searched once you type)
      Tab                           complete commands, config keys, file paths
      Ctrl+Left / Ctrl+Right        move by word
      Ctrl+W / Ctrl+U / Ctrl+K      kill word back / to line start / to line end
      Esc                           clear the line — or cancel a running step
      \\ then Enter                  continue on a new line (pastes keep theirs)

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
      run_command(command)                        Run a PowerShell command (safety-classified;
                                                  sensitive/destructive ones ask first).
      read_file(path, offset, limit)              Read a file; offset/limit page through big ones.
      edit_file(path, old_string, new_string)     Replace text (fuzzy match if whitespace or
                                                  indentation differs; undo snapshot).
      write_file(path, content)                   Write or overwrite a file (undo snapshot).
      append_file(path, content)                  Append text to a file.
      list_directory(path)                        List files and folders.
      search_files(pattern, path, glob)           Grep — search across files by content.
      find_files(glob, path)                      Find files by glob pattern.
      verify_syntax(path, language)               Non-destructive syntax check (.py .json .ps1 .js).
      run_code(path, args, timeout)               Execute a script in a sandboxed subprocess.
      lint_code(path)                             Run ruff and return findings (needs ruff on PATH).
      search_memory(query, top_k)                 Recall relevant prior session context.
      fetch_url(url, max_chars)                   Fetch a URL and return readable text.
      batch(actions)                              Run up to 8 read-only tools in parallel.
      delegate(task)                              Spawn a focused sub-agent (max 5 steps).

    File writes are confined to the working directory (see workspace_write_scope).
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

def show_context(
    session: dict[str, Any],
    config: dict[str, Any],
    budget: tuple[int, int] | None = None,
    system_prompt_tokens: int | None = None,
) -> None:
    """Show context usage against the REAL per-turn budget.

    Pre-v1.8 this printed hardcoded 1,300/1,600 thresholds that were calibrated
    to a system prompt half the actual size, so it told users they had headroom
    they did not have. Thresholds now come from the caller's measured budget.
    """
    messages: list[dict[str, str]] = session.get("messages", [])
    total_chars = sum(len(m.get("content", "")) for m in messages)
    est_tokens = total_chars // 4
    compact_count = session.get("compact_count", 0)
    warn, crit = budget if budget else (1_300, 1_600)
    print()
    cprint("Context estimate", C.BOLD)
    print(f"  Messages:         {len(messages)}")
    print(f"  Chars (total):    {total_chars:,}")
    print(f"  History (est.):   ~{est_tokens:,} tokens  (budget {warn:,})")
    if system_prompt_tokens:
        print(f"  System prompt:    ~{system_prompt_tokens:,} tokens")
        print(f"  Turn total:       ~{est_tokens + system_prompt_tokens:,} tokens")
    print(f"  Compact runs:     {compact_count}")
    print(f"  Max agent steps:  {config.get('max_agent_steps', 15)}")
    print(f"  Model:            {config.get('model', 'unknown')}")
    print(f"  Backend:          {config.get('backend', 'ollama')}")
    if est_tokens >= crit:
        cprint("  ✗ Past the degradation threshold — auto-compact fires after the next turn.", C.BRED)
    elif est_tokens >= warn:
        cprint("  ⚠ At the history budget — auto-compact fires after this turn.", C.BYELLOW)
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
# Consent prompts
# ---------------------------------------------------------------------------

CONFIRM_TIMEOUT_S: float = 30.0


def confirm_or_deny(prompt: str, timeout_s: float | None = None) -> bool:
    """Ask for y/N consent without ever blocking forever; anything but an explicit
    yes is a deny.

    Three ways there is no human to answer, all of which must fail closed:
      * stdin is a pipe / redirected file -> isatty() is False, deny at once;
      * stdin is at EOF -> the read yields nothing, deny;
      * stdin is a **hidden or detached console** -> isatty() is True and a normal
        read never returns. Not hypothetical: this shape hung an unattended eval
        for 7.5 hours on one prompt.

    That third case rules out both obvious implementations. ``input()`` blocks
    forever, and running it on a daemon thread does NOT help, because the Windows
    console read holds the GIL - the main thread never runs, so ``join(timeout)``
    is itself blocked (measured: a 3 s join took 60 s). So poll ``msvcrt`` for a
    keypress instead, the same way ``lineedit`` reads keys, and give up on time.
    """
    if timeout_s is None:  # resolved per call so the constant stays patchable
        timeout_s = CONFIRM_TIMEOUT_S
    if not sys.stdin.isatty():
        return False
    sys.stdout.write(prompt)
    sys.stdout.flush()
    deadline = time.monotonic() + timeout_s
    buf = ""
    while time.monotonic() < deadline:
        if not msvcrt.kbhit():
            time.sleep(0.05)
            continue
        ch = msvcrt.getwch()
        if ch in ("\r", "\n"):
            print()
            return buf.strip().lower() in {"y", "yes"}
        if ch == "\x03":  # Ctrl-C
            print()
            raise KeyboardInterrupt
        if ch in ("\b", "\x7f"):
            buf = buf[:-1]
            continue
        if ch == "\x00" or ch == "\xe0":  # function/arrow key: consume the scan code
            msvcrt.getwch()
            continue
        buf += ch
        sys.stdout.write(ch)
        sys.stdout.flush()
    print()
    cprint(f"   No response after {timeout_s:.0f}s — denying.", C.DIM)
    return False


def confirm_network_fetch(url: str) -> bool:
    """Outbound network access is the exception in an offline-first product;
    require explicit consent per fetch. Denied when non-interactive."""
    print()
    cprint("⚠  Agent wants to fetch a URL (the only network access it has):", C.BYELLOW, bold=True)
    cprint(f"   {url}", C.CYAN)
    print()
    return confirm_or_deny("Allow this fetch? [y/N] ")


def confirm_sensitive_command(cmd: str) -> bool:
    """Sensitive-data access (keys, credentials, security files, obfuscated
    execution) requires explicit consent; denied when non-interactive."""
    print()
    cprint("⚠  Agent wants to access sensitive data or run an obfuscated command:", C.BYELLOW, bold=True)
    cprint(f"   {cmd}", C.RED)
    cprint("   (credentials / keys / security files — deny unless YOU asked for exactly this)", C.DIM)
    print()
    return confirm_or_deny("Allow? [y/N] ")


def confirm_destructive_command(cmd: str) -> bool:
    """Print a destructive-command warning and return True only if the user types
    y/yes; denied when non-interactive or unanswered."""
    print()
    cprint("⚠  Agent wants to run a destructive command:", C.BYELLOW, bold=True)
    cprint(f"   {cmd}", C.RED)
    print()
    return confirm_or_deny("Allow? [y/N] ")


def render_result(title: str, body: str) -> None:
    print()
    cprint(f"── {title} ", C.BOLD + C.BCYAN)
    cprint("─" * 60, C.DIM)
    print(body)
    print()
