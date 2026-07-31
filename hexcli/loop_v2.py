#!/usr/bin/env python3
"""hexcli.loop_v2 — the v2 agent loop (docs/V2_PLAN.md §5-§6).

Selected via config {"protocol": "v2"}; hexcli.agent.run_autopilot delegates
here so the v1 loop stays untouched while v2 is A/B-tested on the eval
instrument. Differences from v1, by design:

  * Byte-stable system core (protocol_v2.SYSTEM_PROMPT_V2) + one per-session
    context line — no per-turn workspace snapshot, no date/cwd churn, no
    keyword-conditional tool schemas. Append-only message layout.
  * Native-format actions parsed by protocol_v2; multi-line payloads never
    touch JSON. A plain-text reply IS the final answer.
  * Unconditional retry-with-precise-error-feedback on malformed output
    (v1 retried only when the raw text happened to contain a tool name).
  * Persistent PowerShell session: cd/env/variables survive across steps
    (and across turns in the REPL).
  * Fuzzy error-loop detection on (tool, args, payload) — near-identical
    failing calls trip it, not just byte-identical (tool, output) pairs.

Shares with v1: call_llm transport (mock backend included), safety
classification + audit log + destructive confirm, sensitive-path blocks,
undo snapshots, memory indexing, telemetry, and the AutopilotProbe seam —
so the eval instrument drives both protocols unchanged.
"""
from __future__ import annotations

import atexit
import hashlib
import json
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from . import memory, safety, telemetry, ui
from .protocol_v2 import (
    SYSTEM_PROMPT_V2,
    apply_search_replace,
    build_session_context,
    parse_response,
    render_tool_result,
)
from .shell_session import ShellSession


def trim_middle(text: str, limit: int) -> str:
    """Head+tail truncation: command output usually carries its verdict at the
    END (exit summaries, stack traces), so the tail must survive — v1's
    head-only trim hid exactly the informative part."""
    if len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = limit - head
    omitted = len(text) - head - tail
    return (text[:head] + f"\n[... {omitted} chars omitted ...]\n" + text[-tail:])


_MAX_FORMAT_RETRIES = 2       # per step, mirrors v1's retry budget
_LOOP_WINDOW = 3              # near-identical calls before the loop detector trips
_READ_DEFAULT_LIMIT = 400     # lines per read page

# Persistent shells for REPL sessions, keyed by session id. One-shot and eval
# runs (session=None) get an ephemeral shell that lives for the turn only.
_SESSION_SHELLS: dict[str, ShellSession] = {}


@atexit.register
def _close_all_shells() -> None:
    for sh in list(_SESSION_SHELLS.values()):
        try:
            sh.close()
        except Exception:
            pass
    _SESSION_SHELLS.clear()


def _get_shell(session: dict[str, Any] | None, cwd: str,
               shell_exe: str = "") -> tuple[ShellSession, bool]:
    # Honour the user's shell_exe (and v1's pwsh-over-powershell preference);
    # v2 previously hardcoded powershell.exe and silently ignored the setting.
    exe = shell_exe or "powershell.exe"
    if session and session.get("id"):
        sid = str(session["id"])
        sh = _SESSION_SHELLS.get(sid)
        if sh is None:
            sh = ShellSession(cwd=cwd, shell_exe=exe)
            _SESSION_SHELLS[sid] = sh
        return sh, False
    return ShellSession(cwd=cwd, shell_exe=exe), True


def close_session_shell(session_id: str) -> None:
    sh = _SESSION_SHELLS.pop(session_id, None)
    if sh is not None:
        sh.close()


# ---------------------------------------------------------------------------
# v2 tool dispatch
# ---------------------------------------------------------------------------

def _tool_read(agent: Any, args: dict[str, Any], output_limit: int) -> str:
    path_text = str(args.get("path") or "").strip()
    if not path_text:
        return "Error: read requires 'path'."
    path = agent.resolve_path(path_text)
    agent._check_sensitive_path(path, "read")
    if not path.exists():
        return f"Error: {path} does not exist."
    if path.is_dir():
        # The 4B model habitually tries read(".") to list a directory; a raw
        # PermissionError here reads as "access denied" and derails the task.
        return (f"Error: {path} is a directory, not a file. To list its "
                f"contents, use shell with: Get-ChildItem \"{path}\"")
    lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
    total = len(lines)
    offset = max(1, int(args.get("offset") or 1))
    limit = max(1, int(args.get("limit") or _READ_DEFAULT_LIMIT))
    page = lines[offset - 1:offset - 1 + limit]
    body = "\n".join(page)
    header = ""
    if offset > 1 or offset - 1 + limit < total:
        end = min(offset - 1 + len(page), total)
        header = (f"[lines {offset}-{end} of {total}; use offset/limit to read more]\n")
    ui.tool_event("read", f"{path} ({total} lines)")
    return header + agent.trim_text(body, output_limit)


def _tool_write(agent: Any, args: dict[str, Any], payload: str | None) -> str:
    path_text = str(args.get("path") or "").strip()
    if not path_text:
        return "Error: write requires 'path'."
    if payload is None:
        return "Error: write requires its file content in a fenced block after </action>."
    return agent.write_file_tool(path_text, payload)


def _tool_edit(agent: Any, args: dict[str, Any], payload: list[tuple[str, str]] | None) -> str:
    path_text = str(args.get("path") or "").strip()
    if not path_text:
        return "Error: edit requires 'path'."
    if not payload:
        return "Error: edit requires at least one SEARCH/REPLACE block after </action>."
    path = agent.resolve_path(path_text)
    # BOTH guards, in the same order as v1's edit_file_tool. This block
    # reimplements the edit (payload blocks instead of old/new strings), and
    # the write-scope check was missing here while v1 had it — so protocol v2
    # could edit files anywhere on disk. Any new mutating path must call both.
    agent._check_sensitive_path(path, "edit")
    agent._check_write_scope(path, "edit", agent._ACTIVE_CONFIG)
    if not path.exists():
        return f"Error: {path} does not exist. Use write to create new files."
    content = path.read_text(encoding="utf-8", errors="replace")
    new_content, err = apply_search_replace(content, payload)
    if err:
        return f"Error: {err}"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(path)
    ui.tool_event("edit", f"{path} ({len(payload)} block(s))")
    return f"Edited {path}: {len(payload)} block(s) applied."


def _tool_shell(agent: Any, config: dict[str, Any], sh: ShellSession, args: dict[str, Any]) -> str:
    cmd = str(args.get("command") or "").strip()
    if not cmd:
        return "Error: shell requires 'command'."
    classification = safety.classify_command(cmd)
    if classification == "destructive" and config.get("autopilot_confirm_destructive", True):
        if not ui.confirm_destructive_command(cmd):
            safety.append_audit_log(agent._CURRENT_SESSION_ID, classification, cmd, "blocked")
            return "Blocked by user."
    if classification == "sensitive" and config.get("autopilot_confirm_sensitive", True):
        if not ui.confirm_sensitive_command(cmd):
            safety.append_audit_log(agent._CURRENT_SESSION_ID, classification, cmd, "blocked")
            return ("Blocked: this command accesses sensitive data (credentials, keys, "
                    "or security files) and was not confirmed. Explain to the user what "
                    "you wanted and why, instead of retrying.")
    ui.command_echo(cmd)
    result = sh.run(cmd, timeout_s=int(config.get("timeout_seconds", 300)))
    safety.append_audit_log(agent._CURRENT_SESSION_ID, classification, cmd, result.get("exit_code"))
    code = result.get("exit_code")
    prefix = f"Exit code: {code}\n" if code is not None else ""
    return prefix + (result.get("output") or "").strip()


def _dispatch(agent: Any, config: dict[str, Any], sh: ShellSession, parsed: Any,
              shell_exe: str, output_limit: int) -> str:
    tool, args, payload = parsed.tool, parsed.args, parsed.payload
    try:
        if tool == "shell":
            return _tool_shell(agent, config, sh, args)
        if tool == "read":
            return _tool_read(agent, args, output_limit)
        if tool == "write":
            return _tool_write(agent, args, payload)
        if tool == "edit":
            return _tool_edit(agent, args, payload)
        if tool == "grep":
            action = {"action": "tool", "tool": "search_files",
                      "args": {"pattern": args.get("pattern", ""), "path": args.get("path", ".")}}
            return agent.execute_tool_call(config, action, shell_exe)
        if tool in ("recall", "fetch_url"):
            v1_name = "search_memory" if tool == "recall" else tool
            action = {"action": "tool", "tool": v1_name, "args": dict(args)}
            return agent.execute_tool_call(config, action, shell_exe)
    except agent.UserCancelled:
        raise
    except Exception as exc:  # noqa: BLE001 — tool failures feed back to the model
        return f"Error: {exc}"
    return f"Error: unknown tool {tool!r}."


def _call_signature(parsed: Any) -> str:
    payload_repr = ""
    if parsed.payload is not None:
        payload_repr = hashlib.sha1(repr(parsed.payload).encode()).hexdigest()[:12]
    return f"{parsed.tool}|{json.dumps(parsed.args, sort_keys=True)}|{payload_repr}"


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

def run(
    config: dict[str, Any],
    history: list[dict[str, str]],
    query: str,
    shell_exe: str,
    session: dict[str, Any] | None = None,
    turn: telemetry.TurnRecorder | None = None,
    probe: Any = None,
) -> str:
    import hexcli.agent as agent  # late import; agent imports us lazily too

    agent.set_active_config(config)
    cwd = str(Path.cwd())
    max_steps = int(config.get("max_agent_steps", 15))
    output_limit = int(config.get("tool_output_limit", 12000))

    system_prompt = (
        SYSTEM_PROMPT_V2
        + "\n\n"
        + build_session_context(cwd, datetime.now().strftime("%Y-%m-%d"))
    )
    messages: list[dict[str, str]] = [
        {"role": "system", "content": system_prompt},
        *history,
        {"role": "user", "content": query.strip()},
    ]
    agent._probe(probe, "on_start", system_prompt, [dict(m) for m in messages])

    sh, ephemeral_shell = _get_shell(session, cwd, shell_exe)
    tools_used: list[str] = []
    touched_paths: list[str] = []
    turn_snapshots: dict[str, str | None] = {}
    recent_sigs: list[tuple[str, bool]] = []  # (signature, was_error)
    last_tool_output = ""
    # Verification-gated finish (docs/V2_PLAN.md §5.3): after a successful file
    # mutation, the model must observe SOMETHING (run/read/check) before its
    # final answer is accepted. One nudge max — the gate guides, never traps.
    unverified_mutation = False
    verify_nudge_used = False

    def _finish(kind: str, message: str, outcome: str) -> str:
        if session and last_tool_output:
            agent.store_observation(session, query, last_tool_output)
        memory.maybe_index_turn(config, query, tools_used, touched_paths, outcome=outcome)
        if session:
            agent._SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = turn_snapshots
        if ephemeral_shell:
            sh.close()
        agent._probe(probe, "on_end", kind, message)
        return message

    try:
        for step in range(max_steps):
            step_label = "thinking" if step == 0 else f"step {step + 1}/{max_steps}"
            agent.cprint(f"\n  {step_label}...", agent.C.DIM, file=sys.stderr)

            parsed = None
            raw = ""
            for attempt in range(_MAX_FORMAT_RETRIES + 1):
                llm_start = time.monotonic()
                raw, eval_count = agent.call_llm(
                    config, messages, "autopilot_max_output_tokens",
                    label=step_label, json_format=False,
                )
                llm_latency = time.monotonic() - llm_start
                if turn:
                    turn.record_llm(llm_latency, eval_count)
                agent._probe(probe, "on_llm", step, attempt, raw, llm_latency)
                parsed = parse_response(raw)
                if parsed.kind != "malformed":
                    break
                if attempt < _MAX_FORMAT_RETRIES:
                    messages.append({"role": "assistant", "content": agent.strip_thinking(raw)})
                    messages.append({"role": "user", "content": f"Format error: {parsed.error}"})
            assert parsed is not None

            if parsed.kind == "malformed":
                # Retries exhausted — report truthfully instead of pretending.
                return _finish(
                    "malformed",
                    f"I could not produce a valid action ({parsed.error}). "
                    f"Last tool output, if any, follows:\n{last_tool_output}".strip(),
                    "malformed",
                )

            if parsed.kind == "final":
                if (unverified_mutation and not verify_nudge_used
                        and config.get("require_verification", True)):
                    verify_nudge_used = True
                    messages.append({"role": "assistant", "content": agent.strip_thinking(raw)})
                    changed = touched_paths[-1] if touched_paths else "the file"
                    messages.append({
                        "role": "user",
                        "content": (
                            f"You modified {changed} but never verified the result. "
                            f"Use the read tool on {changed} (or run it via shell if "
                            "it is code) to confirm the change, then report what you "
                            "actually observed."
                        ),
                    })
                    continue
                return _finish("finish", parsed.final_text, "completed")

            # Tool action.
            tool = parsed.tool
            tools_used.append(tool)
            tool_path = parsed.args.get("path") if isinstance(parsed.args, dict) else None
            if tool_path:
                touched_paths.append(str(tool_path))
            if tool in ("write", "edit") and tool_path:
                try:
                    snap_key = str(agent.resolve_path(str(tool_path)))
                    if snap_key not in turn_snapshots:
                        p = Path(snap_key)
                        turn_snapshots[snap_key] = (
                            p.read_text(encoding="utf-8") if p.exists() else None
                        )
                except Exception:
                    pass

            ui.tool_header(tool)
            tool_start = time.monotonic()
            tool_output = _dispatch(agent, config, sh, parsed, shell_exe, output_limit)
            tool_latency = time.monotonic() - tool_start
            tool_status = "error" if tool_output.startswith("Error:") else "ok"
            if turn:
                turn.record_tool(tool, parsed.args, tool_latency, tool_status)
            agent._probe(probe, "on_tool", step, tool, dict(parsed.args or {}),
                         tool_output, tool_latency, tool_status)
            last_tool_output = tool_output

            if tool in ("write", "edit") and tool_status == "ok":
                unverified_mutation = True
            elif tool in ("shell", "read") and tool_status == "ok":
                # Any successful observation after the mutation counts as
                # verification — the model saw real post-change state.
                unverified_mutation = False

            # Fuzzy loop detection: N consecutive near-identical calls, at
            # least one of which errored, means the agent is spinning.
            sig = _call_signature(parsed)
            recent_sigs.append((sig, tool_status == "error"))
            if len(recent_sigs) > _LOOP_WINDOW:
                recent_sigs.pop(0)
            if (len(recent_sigs) == _LOOP_WINDOW
                    and len({s for s, _ in recent_sigs}) == 1
                    and any(err for _, err in recent_sigs)):
                agent.cprint(
                    f"\n  ⚠ Agent repeated the same failing call {_LOOP_WINDOW}x. Stopping.",
                    agent.C.BYELLOW,
                )
                return _finish(
                    "loop_stop",
                    f"I kept repeating the same failing action and stopped. Last error:\n{tool_output}",
                    "error_loop",
                )

            messages.append({"role": "assistant", "content": agent.strip_thinking(raw)})
            messages.append({
                "role": "user",
                "content": render_tool_result(tool, trim_middle(tool_output, output_limit)),
            })

        return _finish("step_limit", last_tool_output or "Hit the step limit without finishing.", "step_limit")
    except (agent.UserCancelled, KeyboardInterrupt):
        if session:
            agent._SESSION_UNDO_SNAPSHOTS[session.get("id", "")] = turn_snapshots
        if ephemeral_shell:
            sh.close()
        raise
