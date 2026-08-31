#!/usr/bin/env python3
"""hexcli.tools — the agent's leaf tools and write-scope guards, lifted out
of agent.py.

Everything the model can invoke through execute_tool_call: shell commands,
file read/edit/write, search, syntax verification, run_code, plus the
workspace snapshot and AGENTS.md loader — and the safety guards these tools
enforce (_check_write_scope / _check_sensitive_path / guard_mutation), which
own their state here (_HOME): tests that relocate the sensitive-home root
patch hexcli.tools._HOME.

The dispatcher (execute_tool_call) deliberately stays in agent.py and calls
these through agent's re-bound names, so tests that patch sa.run_command_tool
or sa.edit_file_tool keep intercepting every dispatch. The active config is
agent state; the three mutation guards read it at call time via
_active_config() below.

Split stage 3b (docs/V2X_ROADMAP.md, "The Split"). Bodies moved verbatim
apart from that lookup.
"""
from __future__ import annotations

import ast
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

from hexcli import memory, ui
from hexcli.cancel import CancelMonitor, UserCancelled
from hexcli.parsing import _RUFF, trim_text

DEFAULT_TIMEOUT_SECONDS = 300


def _active_config() -> dict[str, Any] | None:
    """The config in force for the current turn, owned by hexcli.agent (the
    prompt builder reads it too). Late import: agent imports this module."""
    from hexcli import agent
    return agent._ACTIVE_CONFIG





# ---------------------------------------------------------------------------
# Paths + safety
# ---------------------------------------------------------------------------

def resolve_path(raw: str) -> Path:
    expanded = os.path.expandvars(os.path.expanduser(raw.strip().strip('"')))
    return Path(expanded).resolve()


_SENSITIVE_HOME_DIRS = frozenset({".ssh", ".gnupg", ".gpg", ".aws"})
_HOME = Path.home().resolve()

# Workspace write-scoping (docs/V2_PLAN.md §7). Reads stay unrestricted —
# the agent must be able to consult docs and libraries outside the project —
# but MUTATIONS are confined to the working directory unless explicitly
# allowed. This is the containment half of the safety story: the sensitive-
# command gate stops exfiltration, this stops collateral damage.
_ALWAYS_WRITABLE_PREFIXES = ("temp", "tmp")


def _check_write_scope(path: Path, op: str, config: dict[str, Any] | None = None) -> None:
    """Deny AGENT-INITIATED mutations outside the workspace.

    Scoping is a policy on what the agent may do during a turn, not a
    property of the file helpers themselves. When no config is active
    (config is None) the tools are being driven programmatically — by
    /undo restore, checkpoint load, or a test — and the policy does not
    apply. run_autopilot and the REPL both set the active config before any
    tool can run, so every agent mutation IS scoped; this exemption cannot
    be reached from a model-issued action.
    """
    if config is None:
        return
    cfg = config
    if not cfg.get("workspace_write_scope", True):
        return
    try:
        resolved = path.resolve()
    except Exception:
        resolved = path
    root = Path(cwd_resolved()).resolve()
    if _is_within(resolved, root):
        return
    # NOTE: system temp is deliberately NOT blanket-allowed. It looks harmless
    # and was allowed in the first draft, but %TEMP% is a large shared area
    # (other apps' state, other agents' sandboxes) and exempting it puts a
    # hole through the containment guarantee for no benefit: eval sandboxes
    # and run_code already run with the workspace AS cwd, so their writes are
    # covered by the rule above. Anything else goes through the allow list.
    for extra in cfg.get("workspace_write_allow", []) or []:
        try:
            if _is_within(resolved, Path(str(extra)).expanduser().resolve()):
                return
        except Exception:
            continue
    raise RuntimeError(
        f"{op} is blocked: {resolved} is outside the workspace "
        f"({root}). Mutations are confined to the working directory. If this "
        "is intended, the user can add the path to workspace_write_allow."
    )


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def cwd_resolved() -> str:
    """Indirection so tests can pin the workspace root."""
    return str(Path.cwd())


def _check_sensitive_path(path: Path, op: str) -> None:
    """Block file operations on SSH/GPG key dirs and Windows credential stores."""
    try:
        rel = path.relative_to(_HOME)
        top = rel.parts[0].lower() if rel.parts else ""
    except ValueError:
        top = ""
    # NOTE: these messages must never name an alternative route. Until
    # 2026-07-31 they ended "Use run_command for direct access." — live uc3
    # traces show the model reading that and immediately trying
    # `run_command Get-Content ~/.ssh/id_rsa`. A refusal that teaches the
    # bypass is worse than no refusal, because it also looks safe in review.
    if top in _SENSITIVE_HOME_DIRS:
        raise RuntimeError(
            f"{op} is blocked for paths under ~/{rel.parts[0]} "
            "(SSH/GPG keys and config). This is a hard boundary — do not "
            "attempt another route. Tell the user what you wanted and why."
        )
    path_str = str(path).lower()
    if "appdata" in path_str and any(
        s in path_str for s in ("\\microsoft\\credentials", "\\microsoft\\protect")
    ):
        raise RuntimeError(
            f"{op} is blocked for Windows credential store paths. This is a "
            "hard boundary — do not attempt another route. Tell the user what "
            "you wanted and why."
        )


def guard_mutation(path: Path, op: str, config: dict[str, Any] | None) -> None:
    """The single gate every file-mutating path must pass through.

    Both checks, always, in this order. It exists because the pair kept coming
    apart: protocol v2's `edit` reimplemented v1's and carried only the
    sensitive-path half, so it could write anywhere on disk. Two calls that
    must always appear together are a latent bug; one call is not.

    Mutating tools must either call this directly or delegate to a v1 tool that
    does. `evals/test_write_scope.py` drives every mutating entry point in both
    protocols at an out-of-scope path and requires a refusal, so a new tool that
    skips the gate fails CI rather than shipping.
    """
    _check_sensitive_path(path, op)
    _check_write_scope(path, op, config)


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
        """Kill the command AND everything it spawned.

        process.terminate() only signals the direct child (powershell.exe).
        A command like `npm test` or `python -m http.server` leaves the real
        work running as grandchildren — orphaned, still holding ports/files,
        invisible to the user who just pressed Esc. taskkill /T walks the
        whole tree; the plain kill stays as the fallback.
        """
        if process.poll() is None:
            killed_tree = False
            try:
                subprocess.run(
                    ["taskkill", "/T", "/F", "/PID", str(process.pid)],
                    capture_output=True, timeout=10,
                )
                killed_tree = True
            except Exception:
                pass
            try:
                process.wait(timeout=3 if killed_tree else 2)
            except subprocess.TimeoutExpired:
                try:
                    process.kill()
                except Exception:
                    pass

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


def read_file_tool(path_text: str, output_limit: int,
                   offset: int = 0, limit: int = 0) -> str:
    """Read a file. With offset/limit (1-based line numbers), read one page —
    v1.7 could only ever see the head of a large file, with no way to page."""
    path = resolve_path(path_text)
    _check_sensitive_path(path, "read_file")
    if path.is_dir():
        raise RuntimeError(
            f"{path} is a directory, not a file. Use list_directory to see its contents."
        )
    if offset or limit:
        lines = path.read_text(encoding="utf-8", errors="replace").split("\n")
        total = len(lines)
        start = max(1, int(offset or 1))
        count = max(1, int(limit or 400))
        page = lines[start - 1:start - 1 + count]
        end = min(start - 1 + len(page), total)
        header = f"[lines {start}-{end} of {total}]\n" if (start > 1 or end < total) else ""
        ui.tool_event("read", f"{path}  (lines {start}-{end} of {total})")
        return header + trim_text("\n".join(page), output_limit)
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
    """Replace old_string with new_string.

    Exact match first; when that fails, fall back to the 3-tier fuzzy applier
    (trailing-whitespace-insensitive, then indent-shifted) rather than erroring
    out — the v1.7 audit found the model frequently mis-copies whitespace or
    indentation, and a hard failure there burned whole step budgets. Ambiguity
    is still an error, never a guess, and a genuine no-match now reports the
    closest region with line numbers so the retry has something to work with.
    """
    from .protocol_v2 import apply_search_replace

    path = resolve_path(path_text)
    guard_mutation(path, "edit_file", _active_config())
    if not old_string:
        raise RuntimeError("edit_file requires a non-empty 'old_string'. Use write_file to overwrite the whole file.")
    if not path.exists():
        raise RuntimeError(f"File not found: {path}")
    content = path.read_text(encoding="utf-8")
    if content.count(old_string) == 1:
        new_content = content.replace(old_string, new_string, 1)
    else:
        new_content, err = apply_search_replace(content, [(old_string, new_string)])
        if err:
            raise RuntimeError(err.replace("SEARCH block 1", "old_string"))
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(path)
    delta = new_string.count("\n") - old_string.count("\n")
    ui.tool_event("edit", f"{path}  ({delta:+d} lines)")
    return f"Edited {path}"


def write_file_tool(path_text: str, content: str) -> str:
    path = resolve_path(path_text)
    guard_mutation(path, "write_file", _active_config())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    ui.tool_event("write", f"{path}  ({len(content)} chars)")
    return f"Wrote {path}"


def append_file_tool(path_text: str, content: str) -> str:
    path = resolve_path(path_text)
    guard_mutation(path, "append_file", _active_config())
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(existing + content, encoding="utf-8")
    tmp.replace(path)
    ui.tool_event("append", f"{path}  ({len(content)} chars)")
    return f"Appended to {path}"


def list_directory_tool(path_text: str, output_limit: int) -> str:
    path = resolve_path(path_text or ".")
    _check_sensitive_path(path, "list_directory")
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
    except ValueError as exc:
        raise RuntimeError(f"Invalid glob pattern {glob_pattern!r}: {exc}") from exc
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
    _check_sensitive_path(search_path, "find_files")
    filtered: list[Path] = []
    try:
        candidates = sorted(search_path.rglob(glob_pattern or "*"))
    except ValueError as exc:
        raise RuntimeError(f"Invalid glob pattern {glob_pattern!r}: {exc}") from exc
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
            [_RUFF, "check", "--output-format=concise", str(path)],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=30,
        )
    except Exception as exc:
        raise RuntimeError(f"ruff failed: {exc}") from exc
    output = (result.stdout + result.stderr).strip()
    status = "clean" if result.returncode == 0 else f"{result.returncode} issue(s)"
    ui.tool_event("lint", f"{path}  ({status})")
    if result.returncode == 0:
        return f"OK: no issues in {path}"
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
    sections = [tag_line]

    project = read_project_instructions(p)
    if project:
        sections.append("Project instructions:\n" + project)

    rules = memory.read_memory_rules(5)
    if rules:
        sections.append("Prior knowledge:\n" + "\n".join(f"  {r}" for r in rules))
    return "\n".join(sections)


# Per-project instructions, in precedence order. AGENTS.md is the cross-tool
# convention; the .shellai/ variant lets you keep it out of the repo.
_PROJECT_INSTRUCTION_FILES = ("AGENTS.md", ".shellai/AGENTS.md", "HEXCLI.md")
_PROJECT_INSTRUCTIONS_MAX_CHARS = 1200  # ~300 tokens — see docs/V2_PLAN.md §6.2


def read_project_instructions(cwd: Path, max_chars: int = _PROJECT_INSTRUCTIONS_MAX_CHARS) -> str:
    """Read the project's agent instructions, hard-capped.

    Every character here is prompt tokens on EVERY turn, against a measured
    ~2,600-token degradation cliff — so the cap is deliberate and the
    truncation is loud rather than silent, otherwise a long AGENTS.md would
    quietly push the model over the edge and look like a model regression.
    """
    for name in _PROJECT_INSTRUCTION_FILES:
        path = cwd / name
        try:
            if not path.is_file():
                continue
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if not text:
            continue
        # Drop comment-only and heading-only noise to spend the budget on rules.
        lines = [ln.rstrip() for ln in text.splitlines()]
        body = "\n".join(ln for ln in lines if ln.strip())
        if len(body) > max_chars:
            body = body[:max_chars].rsplit("\n", 1)[0]
            body += f"\n  […{name} truncated to {max_chars} chars to protect the context budget]"
        return body
    return ""
