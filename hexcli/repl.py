#!/usr/bin/env python3
"""hexcli.repl — the interactive shell, lifted out of agent.py.

run_repl and its helpers: slash-command dispatch, /config and /memory
handlers, /stats, the backend-failure restart flow, and the command list
that drives Tab completion.

Everything agent-resident is referenced through the agent module (sa.X) at
call time, so every existing sa.<name> patch — run_autopilot,
compact_history, sync_session_store, the tool functions — keeps
intercepting REPL-driven calls, and inspect.getsource(sa.run_repl) keeps
working for the suites that cross-check command handling against this
source.

Split stage 6 (docs/V2X_ROADMAP.md, "The Split"). Bodies moved verbatim
apart from the sa. qualification.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
from pathlib import Path
from typing import Any

from hexcli import (
    chatlog,
    diffview,
    lineedit,
    memory,
    setup_wizard,
    telemetry,
    ui,
)
from hexcli import (
    commands as custom_commands,
)


class _AgentProxy:
    """Call-time window onto hexcli.agent, so this module works regardless of
    which side of the agent<->repl cycle imports first, and every sa.<name>
    patch (run_autopilot, compact_history, tool functions, ...) is seen at
    the moment of use rather than frozen at import."""

    def __getattr__(self, name: str) -> Any:
        from hexcli import agent
        return getattr(agent, name)


sa = _AgentProxy()

REPL_COMMANDS = (
    "/help", "/exit", "/quit", "/clear", "/history", "/resume", "/new",
    "/compact", "/config", "/memory", "/tools", "/undo", "/stats", "/diff",
    "/doctor", "/cwd", "/search", "/setup",
)


def _closest_command(word: str, extra: tuple[str, ...] = ()) -> str | None:
    """Nearest known slash command, for typo hints."""
    import difflib
    known = list(REPL_COMMANDS) + list(extra)
    matches = difflib.get_close_matches(word.lower(), known, n=1, cutoff=0.6)
    return matches[0] if matches else None


def _handle_config_cmd(query: str, config: dict[str, Any]) -> None:
    parts = query.split(None, 2)
    if len(parts) == 1:
        print()
        for key, kind in sorted(sa._CONFIG_SETTABLE.items()):
            val = config.get(key, "(unset)")
            print(f"  {key:<42}  {str(val):<18}  [{kind}]")
        print()
        return
    key = parts[1]
    if key not in sa._CONFIG_SETTABLE:
        sa.cprint(f"  Unknown config key: {key!r}. Run /config to see all settable keys.", sa.C.YELLOW)
        return
    if len(parts) == 2:
        sa.cprint(f"  {key} = {config.get(key, '(unset)')!r}  [{sa._CONFIG_SETTABLE[key]}]", sa.C.DIM)
        return
    value_str = parts[2]
    try:
        new_val = sa._coerce_config_value(value_str, sa._CONFIG_SETTABLE[key])
    except (ValueError, TypeError) as exc:
        sa.cprint(f"  Cannot set {key!r}: {exc}", sa.C.RED)
        return
    config[key] = new_val
    sa.cprint(f"  {key} = {new_val!r}", sa.C.BCYAN)


def _handle_memory_cmd(query: str, config: dict[str, Any]) -> None:
    parts = query.split(None, 2)
    sub = parts[1].lower() if len(parts) > 1 else "status"

    if sub == "status":
        enabled = bool(config.get("memory_enabled", True))
        if not enabled:
            sa.cprint("  Memory disabled  (memory_enabled = false).", sa.C.YELLOW)
            return
        meta_path = Path.cwd() / ".shellai" / "vector_store" / "metadata.json"
        if not meta_path.exists():
            sa.cprint("  Memory store: empty (no entries indexed yet).", sa.C.DIM)
            return
        try:
            entries = json.loads(meta_path.read_text(encoding="utf-8"))
            size_kb = meta_path.stat().st_size // 1024
            sa.cprint(f"  Memory store: {len(entries)} entries, ~{size_kb} KB", sa.C.BCYAN)
            if entries:
                oldest = entries[0].get("created_at", "?")[:16]
                newest = entries[-1].get("created_at", "?")[:16]
                sa.cprint(f"  Oldest: {oldest}  →  Newest: {newest}", sa.C.DIM)
        except Exception as exc:
            sa.cprint(f"  Memory store: error reading metadata ({exc})", sa.C.YELLOW)

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
            sa.cprint(f"  Error reading memory: {exc}", sa.C.YELLOW)

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
                    sa.cprint(f"  Could not delete {fname}: {exc}", sa.C.YELLOW)
        if deleted:
            sa.cprint(f"  Cleared: {', '.join(deleted)}", sa.C.BCYAN)
        else:
            print("  Nothing to clear.")

    elif sub == "prune":
        removed = memory.prune_memory_rules()
        if removed:
            sa.cprint(f"  Pruned {removed} old rule(s) from memory_rules.md.", sa.C.BCYAN)
        else:
            sa.cprint("  Rules file within cap; nothing pruned.", sa.C.DIM)

    else:
        print("  Usage: /memory [status|list [n]|search <query>|clear|prune]")


def _show_stats(config: dict[str, Any], tel: Any, session: dict[str, Any]) -> None:
    """Summarise this session plus recent history from the telemetry logs.

    telemetry.py has always written rich per-turn records (tool calls, latency
    split, tokens, completion status) — and nothing ever read them back. On
    15 tok/s hardware, time-per-task is the cost metric that matters, so this
    is the number users actually want.
    """
    turns = list(getattr(tel, "turns", []) or [])
    print()
    sa.cprint("Session stats", sa.C.BOLD)
    if not turns:
        sa.cprint("  No completed turns yet.", sa.C.DIM)
    else:
        total_time = sum(t.get("total_latency_s", 0) for t in turns)
        think_time = sum(t.get("thinking_latency_s", 0) for t in turns)
        tokens = sum(t.get("tokens_generated", 0) for t in turns)
        agentic = [t for t in turns if t.get("execution_path") == "agentic"]
        errors = [t for t in turns if t.get("completion_status") != "completed"]
        tool_counts: dict[str, int] = {}
        for t in turns:
            for call in t.get("tool_calls", []):
                name = str(call.get("tool", "?"))
                tool_counts[name] = tool_counts.get(name, 0) + 1
        print(f"  Turns:            {len(turns)}  ({len(agentic)} used tools)")
        print(f"  Total time:       {total_time:.0f}s  "
              f"(model {think_time:.0f}s, tools {max(0.0, total_time - think_time):.0f}s)")
        print(f"  Avg turn:         {total_time / len(turns):.1f}s")
        print(f"  Tokens generated: ~{tokens:,}")
        if errors:
            print(f"  Non-clean turns:  {len(errors)}  "
                  f"({', '.join(sorted({str(t.get('completion_status')) for t in errors}))})")
        if tool_counts:
            top = sorted(tool_counts.items(), key=lambda kv: -kv[1])[:6]
            print("  Tools used:       " + ", ".join(f"{n}×{c}" for n, c in top))
    # Lifetime view from the log directory.
    try:
        log_dir = Path.cwd() / ".shellai" / "logs"
        files = sorted(log_dir.glob("session_*.json"))
        if files:
            total_turns = 0
            for f in files[-50:]:
                try:
                    total_turns += len(json.loads(f.read_text(encoding="utf-8")).get("turns", []))
                except Exception:
                    continue
            print(f"  This project:     {len(files)} sessions logged, "
                  f"{total_turns} turns (last 50 sessions)")
    except Exception:
        pass
    print()


def _close_session_resources(session: dict[str, Any] | None) -> None:
    """Release per-session OS resources when a session ends.

    Protocol v2 keeps a persistent PowerShell process per session id. Session
    switches (/new, /resume) used to abandon them, so a long REPL run
    accumulated live shells until process exit.
    """
    if not session:
        return
    sid = str(session.get("id", ""))
    if not sid:
        return
    try:
        from . import loop_v2
        loop_v2.close_session_shell(sid)
    except Exception:
        pass


def _handle_backend_failure(config: dict[str, Any], reason: str) -> None:
    """Explain a backend failure and offer to restart it in place.

    Covers both "server is down" and the measured degradation mode where the
    Genie dialog goes sticky-failed and 500s everything (V2_PLAN §14.4). In
    both cases the fix is the same — a fresh server — so offer it here rather
    than making the user leave the session.
    """
    sa.cprint(f"\n  The model backend failed: {reason}.", sa.C.BRED)
    if str(config.get("backend")) != "openai" or "_npurun_model" not in config:
        sa.cprint("  Restart it, then try again.", sa.C.DIM)
        return
    sa.cprint("  This usually means the NPU server needs a restart "
           "(it degrades after a few hours of use).", sa.C.DIM)
    try:
        answer = input("  Restart the model server now? [Y/n] ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        answer = "n"
    if answer in ("", "y", "yes"):
        if restart_backend(config):
            sa.cprint("  Server restarted. Retry the last request.", sa.C.BGREEN)
        else:
            sa.cprint("  Restart failed. Run: python launcher.py", sa.C.YELLOW)


def restart_backend(config: dict[str, Any]) -> bool:
    """Stop and respawn the local npurun server. Returns True when healthy."""
    model = str(config.get("_npurun_model") or "")
    if not model:
        return False
    exe = Path.home() / ".cargo" / "bin" / "npurun.exe"
    if not exe.exists():
        return False
    try:
        subprocess.run(["taskkill", "/F", "/IM", "npurun.exe"],
                       capture_output=True, timeout=15)
    except Exception:
        pass
    time.sleep(2)
    sdk = Path(os.environ.get("QNN_SDK_ROOT", r"C:\Qualcomm\AIStack\QAIRT_2.47.0"))
    env = os.environ.copy()
    env["QNN_SDK_ROOT"] = str(sdk)
    env["ADSP_LIBRARY_PATH"] = str(sdk / "lib" / "hexagon-v73" / "unsigned")
    env["PATH"] = (f"{sdk / 'bin' / 'aarch64-windows-msvc'};"
                   f"{sdk / 'lib' / 'aarch64-windows-msvc'};{env.get('PATH', '')}")
    try:
        # The launcher owns runtime selection (newest valid QAIRT, and the
        # Rewind/prefix-reuse mode when SDK >= 2.50 and npurun >= 0.2.0);
        # a restart must respawn the same server the launcher started.
        import launcher
        env = launcher._npurun_env()
    except Exception:
        pass
    bind = sa._backend_url(config).split("//")[-1].split("/")[0]
    try:
        subprocess.Popen(
            [str(exe), "serve", "--model", model, "--bind", bind],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            env=env, creationflags=0x00000008,  # DETACHED_PROCESS
        )
    except Exception:
        return False
    with sa.Spinner("restarting the model server"):
        for _ in range(45):
            time.sleep(2)
            if sa.ping_backend(config):
                return True
    return False


def _last_error_text(scope: dict[str, Any]) -> str:
    """The exception bound in the enclosing except-block, as text (for the
    chat log's turn_end); empty when there is none."""
    exc = scope.get("exc")
    return f"{type(exc).__name__}: {exc}" if isinstance(exc, BaseException) else ""


def run_repl(config: dict[str, Any]) -> int:
    shell_exe = sa.detect_shell(str(config.get("shell_exe", "") or ""))
    sessions = sa.load_history_store(config)
    current_session = sa.create_session()
    tel = telemetry.SessionTelemetry(config)
    clog = chatlog.ChatLog(config, version=sa.VERSION)

    ui.print_banner(str(config.get("model", "?")), str(config.get("backend", "ollama")))
    if config.get("memory_dreaming", False):
        memory.start_dreaming(lambda: config, sa.llm_generate)

    # Rich line editing where the terminal supports it; bare input() otherwise
    # (piped stdin, CI, --raw) so nothing depends on it being available.
    # Custom commands are discovered once here for Tab completion; dispatch
    # below re-reads the file each use, so edits apply without a restart.
    custom_names = tuple(sorted(custom_commands.discover()))
    read_line = lineedit.make_reader(
        config, tuple(REPL_COMMANDS) + custom_names, lambda: sorted(sa._CONFIG_SETTABLE)
    ) or (lambda p: input(p))

    while True:
        prompt = sa.repl_prompt(config, sa.context_fill_percent(current_session, config))
        try:
            query = read_line(prompt).strip()
        except EOFError:
            print()
            sa.sync_session_store(sessions, current_session)
            return 0
        except KeyboardInterrupt:
            print()
            continue

        memory.touch_last_turn()

        if not query:
            continue

        norm = sa.normalize_text(query)
        if query.startswith("/"):
            clog.command(query)

        # ── exit ──────────────────────────────────────────────────────────
        if norm in {"/exit", "/quit"}:
            sa.sync_session_store(sessions, current_session)
            clog.event("session_end")
            return 0

        # ── help / tools ──────────────────────────────────────────────────
        if norm == "/help":
            print(f"\n{sa.HELP_TEXT}\n")
            continue
        if norm == "/tools":
            print(f"\n{sa.TOOLS_HELP}\n")
            continue

        # ── history ───────────────────────────────────────────────────────
        if norm == "/history":
            sa.sync_session_store(sessions, current_session)
            sessions = sa.load_history_store(config)
            sa.render_history_list(sessions, str(current_session.get("id", "")))
            continue

        # ── search saved sessions ─────────────────────────────────────────
        if norm == "/search" or norm.startswith("/search "):
            parts = query.split(None, 1)
            term = parts[1].strip() if len(parts) > 1 else ""
            if not term:
                sa.cprint("  Usage: /search <text>   (searches titles and messages "
                       "of saved sessions)", sa.C.DIM)
                continue
            sa.sync_session_store(sessions, current_session)
            sessions = sa.load_history_store(config)
            hits = sa.sessions_search(sessions, term)
            ui.render_search_results(term, hits)
            continue

        # ── diff: what changed in the last turn ───────────────────────────
        if norm == "/diff":
            snaps = sa._SESSION_UNDO_SNAPSHOTS.get(current_session.get("id", ""), {})
            if not snaps:
                sa.cprint("  No file changes in this session's last turn.", sa.C.DIM)
            else:
                def _read_now(p: str) -> str | None:
                    path_obj = Path(p)
                    return path_obj.read_text(encoding="utf-8", errors="replace") \
                        if path_obj.exists() else None
                print(diffview.render_turn_diffs(snaps, _read_now))
            continue

        # ── stats: session summary + context usage (absorbed /context) ────
        if norm == "/stats" or norm.startswith("/stats "):
            _show_stats(config, tel, current_session)
            _sys_tokens = sa.estimate_tokens(sa.build_autopilot_prompt(
                cwd=str(Path.cwd()), max_steps=int(config.get("max_agent_steps", 15))))
            sa.show_context(current_session, config,
                         budget=sa._history_budget_tokens(config),
                         system_prompt_tokens=_sys_tokens)
            if clog.path:
                sa.cprint(f"  Chat log: {clog.path}", sa.C.DIM)
                print()
            continue

        # ── doctor: diagnose the installation without leaving the REPL ────
        if norm == "/doctor":
            from . import doctor
            doctor.run_doctor(config, sa.APP_DIR)
            continue

        # ── setup: interactive config wizard ──────────────────────────────
        if norm == "/setup":
            wizard_path = Path(str(config.get("_config_path", "")) or sa.DEFAULT_CONFIG_PATH)
            setup_wizard.run_wizard(config, wizard_path)
            continue

        # ── clear screen + context ────────────────────────────────────────
        # v2.0 made /clear screen-only because the old silent alias-for-/new
        # lost sessions without a trace. In practice the split was noise: you
        # clear when the current thread is done, and an announced fresh
        # session is not silent data loss — the old one stays one /resume
        # away. /clear is now the one reset command; /new remains an alias
        # that keeps the scrollback.
        if norm == "/clear":
            os.system("cls" if os.name == "nt" else "clear")
            sa.sync_session_store(sessions, current_session)
            _close_session_resources(current_session)
            current_session = sa.create_session()
            sa.cprint("Chat history cleared.", sa.C.DIM)
            continue

        # ── new session ───────────────────────────────────────────────────
        if norm == "/new":
            sa.sync_session_store(sessions, current_session)
            _close_session_resources(current_session)
            current_session = sa.create_session()
            sa.cprint("New session started.", sa.C.DIM)
            continue

        # ── resume ────────────────────────────────────────────────────────
        if norm == "/resume" or norm.startswith("/resume "):
            sa.sync_session_store(sessions, current_session)
            sessions = sa.load_history_store(config)
            parts = norm.split()
            if len(parts) != 2 or not parts[1].isdigit():
                print("Usage: /resume <number>")
                continue
            idx = int(parts[1]) - 1
            if idx < 0 or idx >= len(sessions):
                sa.cprint("No session with that number.", sa.C.YELLOW)
                continue
            _close_session_resources(current_session)
            current_session = sessions[idx]
            sa.cprint(f"Resumed: {current_session['title']}", sa.C.BCYAN)
            continue

        # ── compact ───────────────────────────────────────────────────────
        if norm == "/compact":
            try:
                sa.compact_history(config, current_session)
                sa.sync_session_store(sessions, current_session)
            except sa.UserCancelled:
                print("\nCancelled.\n")
            except Exception as exc:  # noqa: BLE001
                ui.error_box(str(exc))
                if sa.DEBUG:
                    raise
            continue

        # ── undo ──────────────────────────────────────────────────────────
        if norm == "/undo":
            msgs: list[dict[str, str]] = current_session.get("messages", [])
            if len(msgs) >= 2:
                current_session["messages"] = msgs[:-2]
                sa.touch_session(current_session)
                # Restore any files mutated during the last agentic turn.
                snapshots = sa._SESSION_UNDO_SNAPSHOTS.pop(current_session.get("id", ""), {})
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
                        sa.cprint(f"  Files restored: {', '.join(restored)}", sa.C.BCYAN)
                    if failed:
                        sa.cprint(f"  Could not restore: {', '.join(failed)}", sa.C.YELLOW)
                sa.sync_session_store(sessions, current_session)
                sa.cprint("Removed last exchange.", sa.C.DIM)
            elif len(msgs) == 1:
                current_session["messages"] = []
                sa.touch_session(current_session)
                sa._SESSION_UNDO_SNAPSHOTS.pop(current_session.get("id", ""), None)
                sa.cprint("Removed last message.", sa.C.DIM)
            else:
                print("Nothing to undo.")
            continue

        # ── cwd ───────────────────────────────────────────────────────────
        if norm == "/cwd" or norm.startswith("/cwd "):
            parts_cwd = query.strip().split(None, 1)
            if len(parts_cwd) == 2:
                new_path = parts_cwd[1].strip()
                try:
                    os.chdir(sa.resolve_path(new_path))
                    sa.cprint(f"cwd: {Path.cwd()}", sa.C.BCYAN)
                except Exception as exc:
                    sa.cprint(f"Cannot change to '{new_path}': {exc}", sa.C.RED)
            else:
                sa.cprint(f"cwd: {Path.cwd()}", sa.C.DIM)
            continue

        # ── config ────────────────────────────────────────────────────────
        if norm == "/config" or norm.startswith("/config "):
            _handle_config_cmd(query.strip(), config)
            continue

        # ── memory ────────────────────────────────────────────────────────
        if norm == "/memory" or norm.startswith("/memory "):
            _handle_memory_cmd(query.strip(), config)
            continue

        # Custom commands — user-authored prompt templates. Consulted only
        # after every built-in above has declined, so a custom file can
        # never shadow a real command. The expanded template falls through
        # to mode dispatch as an ordinary query.
        ran_custom = False
        if query.startswith("/"):
            cmd_word = query.split()[0]
            # Belt-and-braces on top of dispatch order: a built-in NAME is
            # never eligible for the custom lookup, even when its handler
            # only matched the "<cmd> <arg>" form (a bare built-in must not
            # run a same-named user template as an agent task).
            is_builtin = cmd_word.lower() in REPL_COMMANDS
            template = None if is_builtin else custom_commands.load(cmd_word)
            if template is not None:
                args_text = query[len(cmd_word):].strip()
                query = custom_commands.expand(template, args_text)
                ran_custom = True

        # Unknown slash command: catch typos HERE. Falling through sends
        # "/hlep" to the model as a task — a 10+ second turn on a 4B that
        # may then start running tools to satisfy a typo.
        if query.startswith("/") and not ran_custom:
            cmd_word = query.split()[0]
            suggestion = _closest_command(cmd_word, extra=custom_names)
            hint = f" Did you mean {suggestion}?" if suggestion else ""
            sa.cprint(f"  Unknown command {cmd_word}.{hint} Type /help for the list.", sa.C.YELLOW)
            continue

        # ── agent turn ────────────────────────────────────────────────────
        history: list[dict[str, str]] = current_session.get("messages", [])
        turn = tel.start_turn("autopilot", query)
        probe = clog.turn_start(len(tel.turns), query, history,
                                sa.context_fill_percent(current_session, config))
        try:
            message = sa.run_autopilot(config, history, query, shell_exe,
                                       session=current_session, turn=turn, probe=probe)
            sa.render_result("Result", message)
            sa.append_session_message(current_session, "user", query)
            sa.append_session_message(current_session, "assistant", message)
            sa.sync_session_store(sessions, current_session)
            tel.record_turn(turn)
            clog.turn_end(probe.turn, status="completed", message=message)
            _before = current_session.get("messages", [])
            _n_before, _c_before = len(_before), sum(len(m.get("content", "")) for m in _before)
            sa._maybe_auto_compact(config, current_session, sessions)
            _after = current_session.get("messages", [])
            if len(_after) != _n_before:
                clog.compaction(_n_before, len(_after), _c_before,
                                sum(len(m.get("content", "")) for m in _after))
        except (sa.UserCancelled, KeyboardInterrupt):
            print("\nCancelled.\n")
            tel.record_turn(turn, status="cancelled")
            clog.turn_end(probe.turn, status="cancelled")
        except urllib.error.HTTPError as exc:
            # Measured 2026-07-30 (V2_PLAN §14.4): after 1-2h of traffic the
            # npurun/Genie dialog degrades into sticky ERROR_QUERY_FAILED and
            # 500s EVERY request until restarted. The eval harness was taught
            # to detect this; the REPL was not — a user just saw errors and
            # had to figure out the restart ritual themselves. Now it is
            # named, and recovery is one keypress.
            if exc.code >= 500:
                _handle_backend_failure(config, f"HTTP {exc.code} from the model server")
            else:
                ui.error_box(f"Backend rejected the request (HTTP {exc.code}).")
            tel.record_turn(turn, status="error")
            clog.turn_end(probe.turn, status="error", message=_last_error_text(locals()))
        except urllib.error.URLError:
            if not sa.ping_backend(config):
                _handle_backend_failure(config, "the model server is not responding")
            else:
                ui.error_box("Network error — backend returned an unexpected response.")
            tel.record_turn(turn, status="error")
            clog.turn_end(probe.turn, status="error", message=_last_error_text(locals()))
        except (ConnectionResetError, ConnectionAbortedError):
            ui.error_box(
                "npurun dropped the stream connection.\n"
                'Add  "use_streaming": false  to shellai.json to avoid this.'
            )
            tel.record_turn(turn, status="error")
            clog.turn_end(probe.turn, status="error", message=_last_error_text(locals()))
        except Exception as exc:  # noqa: BLE001
            ui.error_box(str(exc))
            tel.record_turn(turn, status="error")
            clog.turn_end(probe.turn, status="error", message=_last_error_text(locals()))
            if sa.DEBUG:
                raise

