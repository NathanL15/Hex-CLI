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
import difflib
import itertools
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
    open_row = False   # the last printed output line had no newline
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
                # Dim on screen: command output is evidence, not the answer.
                print(f"{ui.C.DIM}{line.rstrip(chr(10))}{ui.C.RESET}", end="\n" if line.endswith("\n") else "")
                open_row = not line.endswith("\n")
                if parts_chars < _BUF_CAP:
                    parts.append(line)
                    parts_chars += len(line)
    except KeyboardInterrupt:
        _terminate()
        raise UserCancelled()
    process.wait()
    output = "".join(parts)
    if open_row:
        print()   # the command left its last line unterminated; the exit line gets its own row
    ui.tool_event("run", f"exit {process.returncode}")
    return trim_text(f"Exit code: {process.returncode}\n{output}".strip(), output_limit)


# ── A path that is not there ─────────────────────────────────────────────
#
# "File not found: C:\\...\\hielo.ps1" is a dead end, and the 4B model does
# not treat it as one: in the owner's 2026-09-15 17:13 session it had just
# written hilo.ps1, asked for hielo.ps1, got that line, and then spent four
# turns asserting from memory which name was real ("Checked the file
# system." with no tool call) while the owner told it it was hallucinating.
# The directory holds the answer, so the error carries it.

_HINT_SCAN_LIMIT = 2000


def missing_path_hint(path: Path) -> str:
    """A sentence to append to a not-found error: the closest existing names
    in the nearest directory that does exist, or a pointer at list_directory
    when nothing is close. Returns "" when there is nothing useful to say."""
    try:
        parent = path.parent
        near = parent
        while not near.is_dir() and near != near.parent:
            near = near.parent
        if not near.is_dir():
            return ""
        try:                                    # never name what we would refuse to read
            _check_sensitive_path(near, "read_file")
        except Exception:
            return ""
        names: list[str] = []
        for child in itertools.islice(near.iterdir(), _HINT_SCAN_LIMIT):
            names.append(child.name)
        # Below the nearest existing directory, the first missing component
        # is the one to correct, not the filename the model asked for.
        try:
            wanted = path.relative_to(near).parts[0]
        except ValueError:
            wanted = path.name
        # Name the component that is actually missing when it is a directory
        # further up: "thing.py is not there" would send the model looking in
        # the wrong place.
        if near == parent:
            where, lead = "that directory", ""
        else:
            where, lead = str(near), f" {wanted} does not exist in {near}."
        if not names:
            return lead or f" {near} is empty."
        close = difflib.get_close_matches(wanted, names, n=3, cutoff=0.6)
        # Same name, different extension: hilo.py for hilo.ps1. get_close_matches
        # ranks by whole-string ratio and can miss it on a short stem.
        stem = Path(wanted).stem.lower()
        close += [n for n in names if Path(n).stem.lower() == stem and n not in close]
        if close:
            return f"{lead} Did you mean {', '.join(close[:3])}, in {where}?"
        if lead:
            return f"{lead} Call list_directory on it to see what is there."
        return (" Nothing with a similar name is in that directory. "
                "Call list_directory on it to see what is there.")
    except OSError:
        return ""


def read_file_tool(path_text: str, output_limit: int,
                   offset: int = 0, limit: int = 0) -> str:
    """Read a file. With offset/limit (1-based line numbers), read one page —
    v1.7 could only ever see the head of a large file, with no way to page."""
    path = resolve_path(path_text)
    _check_sensitive_path(path, "read_file")
    if not path.exists():
        raise RuntimeError(f"File not found: {path}.{missing_path_hint(path)}")
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
        # Byte reads skip universal-newline translation; normalise so CRLF
        # files page on "\n" like the read_text path does.
        content = raw_bytes.decode("utf-8", errors="replace").replace("\r\n", "\n")
        # Count the real line total by streaming — the header must not claim
        # the file ends where our memory-safe pre-read happened to stop.
        total = 1
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                total += chunk.count(b"\n")
    else:
        content = path.read_text(encoding="utf-8", errors="replace")
        total = content.count("\n") + 1
    if len(content) <= output_limit and size <= max_bytes:
        ui.tool_event("read", str(path))
        return content
    # Too big for this step's budget: return the FIRST PAGE, line-aligned,
    # with a header that tells the model how to page — a mid-line head cut
    # with "[truncated]" taught it nothing about how much it had not seen.
    page: list[str] = []
    used = 0
    for ln in content.split("\n"):
        if used + len(ln) + 1 > output_limit:
            break
        page.append(ln)
        used += len(ln) + 1
    if not page:
        # A single line bigger than the whole budget (minified/binary-ish):
        # fall back to a plain head cut so the model still sees something.
        ui.tool_event("read", f"{path}  (first {output_limit} chars of a {total}-line file)")
        return (f"[first {output_limit} chars of {total} lines; use offset/limit]\n"
                + content[:output_limit])
    end = len(page)
    header = (f"[lines 1-{end} of {total}. The file continues: call read_file again "
              f"with offset={end + 1} to read the next part.]\n")
    ui.tool_event("read", f"{path}  (lines 1-{end} of {total})")
    return header + "\n".join(page)


def edit_file_tool(path_text: str, old_string: str, new_string: str) -> str:
    """Replace old_string with new_string.

    Exact match first; when that fails, fall back to the 3-tier fuzzy applier
    (trailing-whitespace-insensitive, then indent-shifted) rather than erroring
    out — the v1.7 audit found the model frequently mis-copies whitespace or
    indentation, and a hard failure there burned whole step budgets. Ambiguity
    is still an error, never a guess, and a genuine no-match now reports the
    closest region with line numbers so the retry has something to work with.
    """
    from . import editing as p2
    from .editing import apply_search_replace

    path = resolve_path(path_text)
    guard_mutation(path, "edit_file", _active_config())
    if not old_string:
        raise RuntimeError("edit_file requires a non-empty 'old_string'. Use write_file to overwrite the whole file.")
    if not path.exists():
        raise RuntimeError(f"File not found: {path}.{missing_path_hint(path)}")
    if old_string == new_string:
        # 37 of the 840 edit_file calls on record (2026-09-17) sent the same
        # text as old and new and were told "Edited"; the model then ran or
        # read the file believing it had changed it (tests-claim-1: "The
        # median function was correctly fixed", after an edit of mean to
        # itself). A no-op is not an edit, and saying so is the only way the
        # next step can be the change that was meant.
        raise RuntimeError(
            f"old_string and new_string are identical, so nothing changed in {path}. "
            "Put the changed text in new_string, or read_file to see what is there now."
        )
    content = path.read_text(encoding="utf-8")
    tier = "exact"
    if content.count(old_string) == 1:
        new_content = content.replace(old_string, new_string, 1)
    else:
        new_content, err = apply_search_replace(content, [(old_string, new_string)])
        if err:
            raise RuntimeError(err.replace("SEARCH block 1", "old_string"))
        tier = p2.LAST_APPLY_TIER
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(new_content, encoding="utf-8")
    tmp.replace(path)
    delta = new_string.count("\n") - old_string.count("\n")
    # The tier is UI-only (arm logs read it); the model sees "Edited <path>".
    suffix = "" if tier == "exact" else f", {tier} match"
    ui.tool_event("edit", f"{path}  ({delta:+d} lines{suffix})")
    return f"Edited {path}"


def write_file_tool(path_text: str, content: str) -> str:
    from . import editing as p2

    path = resolve_path(path_text)
    guard_mutation(path, "write_file", _active_config())
    path.parent.mkdir(parents=True, exist_ok=True)
    note = ""
    if p2.looks_double_escaped(content):
        content = p2.unescape_body(content, str(path))
        note = ", unescaped"
    tmp = path.parent / (path.name + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)
    ui.tool_event("write", f"{path}  ({len(content)} chars{note})")
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
        raise RuntimeError(f"Directory not found: {path}.{missing_path_hint(path)}")
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


_PY_IMPLICIT = {"__name__", "__file__", "__doc__", "__builtins__", "__spec__", "__loader__",
                "__package__", "__path__", "__annotations__", "__debug__"}


def undefined_python_names(source: str) -> list[str]:
    """Names the module reads but never binds anywhere and Python does not
    provide: the cross-reference rung for Python. Scopes are collapsed to
    the module (a name bound anywhere counts), so this never flags a name
    that some function defines; it only catches the calculator-class
    mistake of calling something that does not exist. A star import turns
    the check off."""
    import builtins
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    bound: set[str] = set(dir(builtins)) | _PY_IMPLICIT
    loads: dict[str, int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(a.name == "*" for a in node.names):
            return []
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                bound.add((a.asname or a.name).split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
            if not isinstance(node, ast.ClassDef):
                args = node.args
                for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                    bound.add(a.arg)
                for a in (args.vararg, args.kwarg):
                    if a is not None:
                        bound.add(a.arg)
        elif isinstance(node, ast.Lambda):
            args = node.args
            for a in [*args.posonlyargs, *args.args, *args.kwonlyargs]:
                bound.add(a.arg)
            for a in (args.vararg, args.kwarg):
                if a is not None:
                    bound.add(a.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, (ast.Global, ast.Nonlocal)):
            bound.update(node.names)
        elif isinstance(node, ast.MatchAs) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchStar) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            bound.add(node.rest)
        elif isinstance(node, ast.Name):
            if isinstance(node.ctx, (ast.Store, ast.Del)):
                bound.add(node.id)
            else:
                loads.setdefault(node.id, node.lineno)
    return [f"{name} (line {line})" for name, line in sorted(loads.items(), key=lambda kv: kv[1])
            if name not in bound]


_JS_BUILTIN_CALLS = {"alert", "confirm", "prompt", "eval", "parseInt", "parseFloat", "String",
                     "Number", "Boolean", "Math", "setTimeout", "setInterval", "console",
                     "document", "window", "event", "this", "if", "for", "while", "return",
                     "function", "new", "Array", "Object", "JSON", "isNaN", "Date", "fetch",
                     "requestAnimationFrame", "clearTimeout", "clearInterval", "Promise"}


def html_wiring_report(path: Path) -> tuple[bool, str]:
    """Cross-reference rung for a page: the handlers the markup calls must be
    functions the scripts define, the ids the scripts look up must be
    elements the markup has, and buttons must be wired to something. Reading
    a page back proves the bytes; this proves the parts are connected
    (2026-09-14: a calculator with buttons that called nothing, a script that
    looked up an id no element had, and a function nothing called was
    "verified" three times by read_file)."""
    from html.parser import HTMLParser

    class _Walk(HTMLParser):
        def __init__(self) -> None:
            super().__init__()
            self.ids: set[str] = set()
            self.handlers: list[str] = []
            self.buttons = 0
            self.scripts: list[str] = []
            self.script_srcs: list[str] = []
            self._in_script = False
            self._body_closed = False
            self.script_after_body = False
            self._buf: list[str] = []

        def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
            a = {k.lower(): (v or "") for k, v in attrs}
            if a.get("id"):
                self.ids.add(a["id"])
            for k, v in a.items():
                if k.startswith("on") and v:
                    self.handlers.extend(m for m in re.findall(r"([A-Za-z_$][\w$]*)\s*\(", v))
            if tag == "button" or (tag == "input" and a.get("type", "").lower() in {"button", "submit"}):
                self.buttons += 1
            if tag == "script":
                self._in_script = True
                self._buf = []
                if a.get("src"):
                    self.script_srcs.append(a["src"])
                if self._body_closed:
                    self.script_after_body = True

        def handle_endtag(self, tag: str) -> None:
            if tag == "script" and self._in_script:
                self._in_script = False
                self.scripts.append("".join(self._buf))
            if tag in {"body", "html"}:
                self._body_closed = True

        def handle_data(self, data: str) -> None:
            if self._in_script:
                self._buf.append(data)

    source = path.read_text(encoding="utf-8", errors="replace")
    w = _Walk()
    try:
        w.feed(source)
        w.close()
    except Exception as exc:  # noqa: BLE001 — html.parser is lenient; anything else is a report
        return False, f"FAIL: could not parse the page: {exc}"
    script = "\n".join(w.scripts)
    for src in w.script_srcs:
        try:
            script += "\n" + (path.parent / src).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    defined = set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(", script))
    defined |= set(re.findall(r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?(?:function\b|\(|[A-Za-z_$][\w$]*\s*=>)", script))
    defined |= set(re.findall(r"\b(?:window\.)?([A-Za-z_$][\w$]*)\s*=\s*(?:async\s+)?function\b", script))
    looked_up = set(re.findall(r"getElementById\(\s*['\"]([^'\"]+)['\"]", script))
    looked_up |= set(re.findall(r"querySelector(?:All)?\(\s*['\"]#([\w-]+)", script))
    listeners = bool(re.search(r"addEventListener\s*\(|\.on[a-z]+\s*=", script))
    problems: list[str] = []
    undefined = sorted({h for h in w.handlers if h not in defined and h not in _JS_BUILTIN_CALLS})
    if undefined:
        problems.append("handlers the markup calls but no script defines: " + ", ".join(undefined))
    missing = sorted(i for i in looked_up if i not in w.ids)
    if missing:
        problems.append("ids the script looks up but no element has: " + ", ".join(missing))
    if w.buttons and not w.handlers and not listeners:
        problems.append(f"{w.buttons} button(s) and none is wired to a script (no on* attribute, no addEventListener)")
    notes: list[str] = []
    if w.script_after_body:
        notes.append("a <script> sits after </body>")
    if defined and not w.handlers and not listeners:
        notes.append("functions defined but nothing calls them: " + ", ".join(sorted(defined)))
    if problems:
        return False, "FAIL: parsed, cross-referenced: " + "; ".join(problems + notes)
    summary = f"OK: parsed, cross-referenced ({len(w.handlers)} handler call(s), {len(w.ids)} id(s), {w.buttons} button(s))"
    if notes:
        summary += "; " + "; ".join(notes)
    return True, summary


def verify_syntax_tool(path_text: str, language: str, shell_exe: str) -> str:
    """The verification ladder. Every result says which rung was reached:
    executed (not here: run_code does that), parsed and cross-referenced,
    parsed only, or NOT CHECKED. A file this tool cannot check is reported
    as unchecked, never as OK: the model used to read "OK: skipped" and
    finish with "verified"."""
    path = resolve_path(path_text)
    if not path.exists():
        raise RuntimeError(f"File not found: {path}.{missing_path_hint(path)}")
    suffix = path.suffix.lower()
    lang = (language or "").strip().lower() or _LANGUAGE_BY_EXT.get(suffix, "")
    if suffix in {".html", ".htm"} or lang in {"html", "htm"}:
        ok, detail = html_wiring_report(path)
    elif lang == "python":
        ok, detail = _verify_python_syntax(path)
        if ok and not detail.startswith("OK: skipped"):
            undefined = undefined_python_names(path.read_text(encoding="utf-8", errors="replace"))
            if undefined:
                ok, detail = False, "FAIL: parsed, cross-referenced: references undefined name(s): " + ", ".join(undefined)
            else:
                detail = "OK: parsed, cross-referenced: no syntax errors, every name it reads is defined"
    elif lang == "json":
        ok, detail = _verify_json_syntax(path)
        if ok:
            detail = detail.replace("OK: valid JSON", "OK: parsed: valid JSON")
    elif lang == "powershell":
        ok, detail = _verify_powershell_syntax(path, shell_exe)
        if ok and detail.startswith("OK: skipped"):
            ok, detail = True, "NOT CHECKED: " + detail[len("OK: skipped ("):].rstrip(")")
        elif ok:
            detail = "OK: parsed: no syntax errors"
    elif lang == "node" or suffix in {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx"}:
        ok, detail = _verify_node_syntax(path)
        if ok and detail.startswith("OK: skipped"):
            ok, detail = True, "NOT CHECKED: " + detail[len("OK: skipped ("):].rstrip(")")
        elif ok:
            detail = "OK: parsed: no syntax errors"
    else:
        ok, detail = True, (f"NOT CHECKED: no checker for '{suffix or language or 'this file'}'. "
                            "The file was not verified; say so if you report on it.")
    ui.tool_event("verify", f"{path}  ({'fail' if not ok else ('unchecked' if detail.startswith('NOT CHECKED') else 'pass')})")
    return detail


def lint_code_tool(path_text: str) -> str:
    if not _RUFF:
        raise RuntimeError("ruff is not on PATH — lint_code is unavailable.")
    path = resolve_path(path_text)
    if not path.exists():
        raise RuntimeError(f"File not found: {path}.{missing_path_hint(path)}")
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
        raise RuntimeError(f"File not found: {path}.{missing_path_hint(path)}")
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
