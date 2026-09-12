#!/usr/bin/env python3
"""hexcli.launcher — start the NPU server, then the REPL.

The `hex` command (and `Hex CLI.cmd` / `launcher.py` in a checkout) runs
this. It finds the QAIRT SDK and the npurun build, pulls the model bundle
on first use, starts `npurun serve` if it is not up, writes the runtime
config under ~/.shellai, and runs `python -m hexcli.agent` with the
server's environment. Qwen3-4B on the Hexagon NPU is the only path: a
missing prerequisite is reported with the fix, never substituted.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles often default to cp1252, which can't encode the arrows/
# checkmarks this script prints. Force UTF-8 so it works regardless of caller.
if sys.platform == "win32":
    for _stream in (sys.stdout, sys.stderr):
        if hasattr(_stream, "reconfigure"):
            _stream.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

from . import paths

APP_DIR = paths.CHECKOUT_DIR or paths.PACKAGE_DIR   # kept for callers; see hexcli.paths

# npurun — Qwen3-4B on Hexagon NPU via Qualcomm Genie SDK.
# Discovery mirrors install.ps1: a source build wins, then the prebuilt
# binary the installer downloads next to this script, then PATH.

# The fork build this version of Hex CLI is written for. An older build runs,
# but without whatever the newer fork added (2.6.x: host polling off, the
# start-up prime, the request-ending watchdog, the async-init override), and
# nothing used to say so. Now --doctor fails on it, the launcher warns, and
# install.ps1 / hexcli --update replace it. install.ps1 reads this line by
# regex, so keep the shape `REQUIRED_NPURUN = (a, b, c)`.
REQUIRED_NPURUN = (0, 2, 3)
NPURUN_RELEASES = "https://github.com/NathanL15/npurun/releases"


def version_str(version: tuple[int, ...]) -> str:
    return ".".join(str(n) for n in version)


def _npurun_version(exe: Path) -> tuple[int, ...]:
    """(major, minor, patch) from `npurun --version`; () if unknown."""
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=10).stdout
    except Exception:
        return ()
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", out or "")
    return tuple(int(x) for x in m.groups()) if m else ()


def find_npurun_exe(home: Path | None = None, app_dir: Path | None = None,
                    required: tuple[int, ...] | None = None) -> Path | None:
    """A source build wins, then the downloaded binary, then PATH — except
    that a candidate older than `required` yields to a later one that is not,
    so a stale cargo build cannot shadow the binary --update just fetched."""
    home = home or Path.home()
    required = REQUIRED_NPURUN if required is None else required
    downloaded = ([app_dir / paths.NPURUN_ASSET] if app_dir is not None
                  else paths.npurun_download_candidates())
    candidates = [c for c in (home / ".cargo" / "bin" / "npurun.exe", *downloaded) if c.exists()]
    if not candidates:
        import shutil
        found = shutil.which("npurun")
        return Path(found) if found else None
    for candidate in candidates:
        version = _npurun_version(candidate)
        if version and tuple(version) >= tuple(required):
            return candidate
    return candidates[0]


def npurun_outdated(exe: Path | None = None,
                    version: tuple[int, ...] | None = None) -> tuple[int, ...] | None:
    """The installed build's version when it is known and older than
    REQUIRED_NPURUN; None when it is current or unknown."""
    if version is None:
        version = _npurun_version(exe or NPURUN_EXE)
    if version and tuple(version) < tuple(REQUIRED_NPURUN):
        return tuple(version)
    return None


def _qairt_valid(root: Path) -> bool:
    return (
        (root / "lib" / "aarch64-windows-msvc").exists()
        and (root / "bin" / "aarch64-windows-msvc").exists()
        and (root / "lib" / "hexagon-v73" / "unsigned").exists()
    )


def _qairt_version_key(path: Path) -> tuple[int, ...]:
    """Numeric sort key for QAIRT_<a>.<b>.<c> directory names.

    String sort is wrong and quietly so: 'QAIRT_2.9.0' > 'QAIRT_2.47.0'
    lexicographically, which would export a stale SDK and produce exactly the
    DLL/stack-overrun failures this discovery code exists to prevent.
    """
    parts = path.name[len("QAIRT_"):].split(".")
    key: list[int] = []
    for part in parts:
        digits = "".join(c for c in part if c.isdigit())
        key.append(int(digits) if digits else 0)
    return tuple(key)


def find_qairt_root(env_value: str | None = None, stack_dir: Path | None = None) -> Path | None:
    """QNN_SDK_ROOT env wins if valid; otherwise the newest valid
    C:\\Qualcomm\\AIStack\\QAIRT_* install (compared numerically)."""
    env_value = env_value if env_value is not None else os.environ.get("QNN_SDK_ROOT", "")
    if env_value:
        root = Path(env_value)
        if _qairt_valid(root):
            return root
        # An explicitly-set root that fails validation is a user intention we
        # are about to ignore; say so, or the resulting failure gets blamed on
        # the SDK we silently substituted.
        warn(f"QNN_SDK_ROOT={env_value} is not a valid QAIRT install; ignored.")
    stack = stack_dir or Path("C:/Qualcomm/AIStack")
    if stack.exists():
        for candidate in sorted(stack.glob("QAIRT_*"), key=_qairt_version_key, reverse=True):
            if _qairt_valid(candidate):
                return candidate
    return None


NPURUN_MODEL     = "qwen3-4b-instruct-2507"
NPURUN_MODEL_DIR = Path.home() / "AppData" / "Local" / "npurun" / "models" / NPURUN_MODEL
NPURUN_PORT      = 11435
NPURUN_LOG       = paths.npurun_log_path()
NPURUN_CONFIG    = paths.runtime_config_path()

# ---------------------------------------------------------------------------
# Console dressing (classic conhost only)
# ---------------------------------------------------------------------------
# The Start Menu shortcut launches via conhost.exe on purpose: Windows
# Terminal has no per-profile taskbar icon, so under WT the running app
# always groups under the generic terminal icon. Classic conhost windows
# accept WM_SETICON, which puts the Hex logo on the taskbar. Conhost does
# not enable ANSI processing by itself the way WT does, so switch that on
# too. Both calls are harmless no-ops under WT/ConPTY.

def _dress_console_window() -> None:
    if sys.platform != "win32":
        return
    import ctypes
    k32 = ctypes.windll.kernel32
    for std in (-11, -12):  # stdout, stderr
        handle = k32.GetStdHandle(std)
        mode = ctypes.c_uint32()
        if k32.GetConsoleMode(handle, ctypes.byref(mode)):
            k32.SetConsoleMode(handle, mode.value | 0x0004)  # VT processing
    # QuickEdit off. With it on (the conhost default), a click inside the
    # window starts a selection and every console write blocks until a key
    # is pressed — the answer streams into a frozen screen and Ctrl+C, which
    # is "copy" while text is selected, is what releases it. Measured
    # 2026-09-04: a click-drag froze the next write for as long as the
    # selection lived, and the process never received an interrupt.
    stdin = k32.GetStdHandle(-10)
    mode = ctypes.c_uint32()
    # Mouse input off too: Windows Terminal gives the mouse to the app when
    # that flag is on and QuickEdit is off, and text selection stops working.
    if k32.GetConsoleMode(stdin, ctypes.byref(mode)):
        ENABLE_MOUSE_INPUT, ENABLE_QUICK_EDIT, ENABLE_EXTENDED_FLAGS = 0x0010, 0x0040, 0x0080
        k32.SetConsoleMode(stdin, (mode.value & ~ENABLE_QUICK_EDIT & ~ENABLE_MOUSE_INPUT) | ENABLE_EXTENDED_FLAGS)
    hwnd = k32.GetConsoleWindow()
    ico = paths.icon_path()
    if not (hwnd and ico.exists()):
        return
    u32 = ctypes.windll.user32
    WM_SETICON, IMAGE_ICON, LR_LOADFROMFILE = 0x80, 1, 0x10
    for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
        h_icon = u32.LoadImageW(None, str(ico), IMAGE_ICON, size, size,
                                LR_LOADFROMFILE)
        if h_icon:
            u32.SendMessageW(hwnd, WM_SETICON, which, h_icon)

# Called from main(), NOT at import time: the evals import this module, and
# an import-time WM_SETICON re-badges whatever console the importing process
# happens to be running in (a test run turned the developer's own terminal
# tab into a Hex window).

# ---------------------------------------------------------------------------
# ANSI helpers
# ---------------------------------------------------------------------------

_TTY = sys.stdout.isatty()

def _c(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text

def bold(t:   str) -> str: return _c(t, "1")
def dim(t:    str) -> str: return _c(t, "2")
def green(t:  str) -> str: return _c(t, "92")
def cyan(t:   str) -> str: return _c(t, "96")
def yellow(t: str) -> str: return _c(t, "93")
def red(t:    str) -> str: return _c(t, "91")

def step(n: int, total: int, msg: str) -> None:
    print(f"  {bold(f'[{n}/{total}]')} {msg}", flush=True)

def ok(msg: str = "done") -> None:
    print(f"        {green('✓')} {msg}", flush=True)

def warn(msg: str) -> None:
    print(f"        {yellow('⚠')} {msg}", flush=True)


# Resolved after the printing helpers exist: find_qairt_root() warns when it
# rejects an explicitly-set QNN_SDK_ROOT, and a NameError there would crash
# the launcher at import for exactly the users that warning is meant for.
NPURUN_EXE   = find_npurun_exe() or (Path.home() / ".cargo" / "bin" / "npurun.exe")
QNN_SDK_ROOT = find_qairt_root() or Path("C:/Qualcomm/AIStack/QAIRT_2.47.0")

# KV prefix reuse ("Rewind runtime"), measured 2026-09-02: on QAIRT >= 2.50
# the fork's NPURUN_REWIND=2 mode keeps the system prompt's KV cache across
# steps AND turns (first-token latency 2-7 s vs 6-10 s). It needs BOTH a
# >= 2.50 SDK and a fork build that knows the mode (>= 0.2.0); older builds
# reset the dialog per request, which 2.50 cannot tolerate after a large
# prefill. When both are present the newest SDK wins over QNN_SDK_ROOT.
MIN_REWIND_QAIRT = (2, 50)
MIN_REWIND_NPURUN = (0, 2, 0)


def rewind_runtime_root(stack_dir: Path | None = None,
                        npurun_version: tuple[int, ...] | None = None) -> Path | None:
    """The QAIRT root to use for the Rewind runtime, or None when the
    machine lacks a new-enough SDK or npurun build."""
    version = npurun_version if npurun_version is not None else _npurun_version(NPURUN_EXE)
    if tuple(version) < MIN_REWIND_NPURUN:
        return None
    stack = stack_dir or Path("C:/Qualcomm/AIStack")
    if not stack.exists():
        return None
    for candidate in sorted(stack.glob("QAIRT_*"), key=_qairt_version_key, reverse=True):
        if _qairt_version_key(candidate)[:2] >= MIN_REWIND_QAIRT and _qairt_valid(candidate):
            return candidate
    return None


REWIND_ROOT = rewind_runtime_root()
if REWIND_ROOT is not None:
    QNN_SDK_ROOT = REWIND_ROOT

def err(msg: str) -> None:
    print(f"        {red('✗')} {msg}", flush=True)

# ---------------------------------------------------------------------------
# npurun path — Qwen3-4B on Hexagon NPU (Genie SDK)
# ---------------------------------------------------------------------------

def _npurun_ready() -> bool:
    """npurun.exe built/installed and QAIRT SDK present."""
    return NPURUN_EXE.exists() and (QNN_SDK_ROOT / "lib" / "aarch64-windows-msvc").exists()


def _npurun_model_ok() -> bool:
    return (NPURUN_MODEL_DIR / "manifest.json").exists() or NPURUN_MODEL_DIR.exists()


def _npurun_env() -> dict:
    env = os.environ.copy()
    bin_dir = str(QNN_SDK_ROOT / "bin" / "aarch64-windows-msvc")
    lib_dir = str(QNN_SDK_ROOT / "lib" / "aarch64-windows-msvc")
    env["QNN_SDK_ROOT"] = str(QNN_SDK_ROOT)
    env["ADSP_LIBRARY_PATH"] = str(QNN_SDK_ROOT / "lib" / "hexagon-v73" / "unsigned")
    env["PATH"] = f"{bin_dir};{lib_dir};{NPURUN_EXE.parent};{env.get('PATH', '')}"
    if REWIND_ROOT is not None:
        env["NPURUN_REWIND"] = "2"   # never reset; prefix-match every warm query
    else:
        env.pop("NPURUN_REWIND", None)
    # Host polling of the NPU (hexcli-fork >= 0.2.2, NPURUN_HTP_POLL). The
    # bundle ships poll=true, which spins ~3 cores even while idle (+11 W,
    # SoC ~70 C) and costs decode speed; measured 2026-09-05 with it off:
    # idle at the machine floor, 19 tok/s instead of 15.5 at half the power.
    # 0.2.2 defaults to off; set it explicitly so the log records intent and
    # a user can flip it back with NPURUN_HTP_POLL=1 in their environment.
    env.setdefault("NPURUN_HTP_POLL", "0")
    # Async dialog init (hexcli-fork >= 0.2.2, NPURUN_HTP_ASYNC_INIT). The
    # fork turns Genie's `allow-async-init` on for a 1.4 s faster dialog
    # rebuild. Measured 2026-09-06 (docs/backend_study/PROMPT_LEVER.md §5):
    # at ~3K tokens of context it multiplies the NPU hang rate about tenfold
    # (7 of 14 requests hung with it on, 1 of 25 with it off, AC power, host
    # polling irrelevant). Off by default; NPURUN_HTP_ASYNC_INIT=1 restores it.
    env.setdefault("NPURUN_HTP_ASYNC_INIT", "0")
    return env


def _is_npurun_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{NPURUN_PORT}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"   # the REPL's frames, so the two spinners match


def _wait_npurun(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    i = 0
    while time.time() < deadline:
        if i % 10 == 0 and _is_npurun_up():
            print("\r" + " " * 40 + "\r", end="", flush=True)
            return True
        print(f"\r  {cyan(_SPINNER[i % len(_SPINNER)])} {dim('starting the model server')}", end="", flush=True)
        time.sleep(0.1)
        i += 1
    print()
    return False


def _pull_npurun_model() -> None:
    """Download the Qwen3-4B Genie bundle via `npurun pull` (~2.5 GB)."""
    r = subprocess.run(
        [str(NPURUN_EXE), "pull", NPURUN_MODEL],
        env=_npurun_env(), text=True, capture_output=True,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "npurun pull failed")


def _write_npurun_config() -> None:
    """Write the backend wiring, preserving anything the user set themselves.

    This file is what `hexcli --config` loads, so /setup writes its answers
    here too. Regenerating it wholesale (which happens after any model
    re-pull) silently reverted those answers, making /setup's "applies on
    every launch" promise false. Only the connection keys are ours to own.
    """
    cfg = {
        "backend": "openai",
        "model": "qwen3-4b",
        "temperature": 0.1,
        "timeout_seconds": 300,
        "max_output_tokens": 1024,
        "autopilot_max_output_tokens": 4096,
        "max_agent_steps": 15,
        "tool_output_limit": 12000,
        "use_streaming": True,
        "openai_compatible": {
            "base_url": f"http://127.0.0.1:{NPURUN_PORT}/v1",
            "api_key": "local",
        },
        "_npurun_model": NPURUN_MODEL,
    }
    if NPURUN_CONFIG.exists():
        try:
            existing = json.loads(NPURUN_CONFIG.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                # User keys win over our defaults; our connection block is
                # rewritten because the port/model may legitimately change.
                connection = {"openai_compatible", "_npurun_model", "backend"}
                for key, value in existing.items():
                    if key not in connection:
                        cfg[key] = value
        except (json.JSONDecodeError, OSError):
            pass  # unreadable: fall back to a clean write
    # Runtime-coupled prompt keys are ours when the Rewind runtime is on:
    # prefix reuse needs a byte-stable system prompt, and the no-tools direct
    # stage becomes a cost (every knowledge query diverges -> dialog rebuild).
    if REWIND_ROOT is not None:
        cfg["prompt_stable_prefix"] = True
        cfg["prompt_split"] = False
    NPURUN_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    ok(str(NPURUN_CONFIG))


def _start_npurun_server() -> None:
    cmd = [str(NPURUN_EXE), "serve", "--model", NPURUN_MODEL,
           "--bind", f"127.0.0.1:{NPURUN_PORT}"]
    log = open(str(NPURUN_LOG), "w", encoding="utf-8")
    subprocess.Popen(cmd, stdout=log, stderr=log, env=_npurun_env(),
                      creationflags=0x00000008)


def _hold_window() -> None:
    """In a classic console the window closes with the process, so a failure
    message would vanish before it could be read. Windows Terminal keeps the
    pane open on a non-zero exit; nothing to do there."""
    if os.environ.get("WT_SESSION") or not sys.stdin.isatty():
        return
    try:
        input("  Press Enter to close.")
    except (EOFError, KeyboardInterrupt):
        pass


def _fail(*lines: str) -> int:
    print()
    for i, line in enumerate(lines):
        err(line) if i == 0 else print(dim(f"  {line}"))
    _hold_window()
    return 1


def run_npurun_path() -> int:
    """npurun setup and launch. Returns the agent's exit code.

    Quiet on the happy path: when the server is already up nothing is
    printed and the REPL's banner is the first thing on screen. Progress
    lines appear only for work that takes time (a model download, a server
    start), and a failure ends here with the log path rather than a silent
    fall-through to a tier that is not maintained.
    """
    outdated = npurun_outdated()
    if outdated:
        warn(f"npurun {version_str(outdated)} is older than the required "
             f"{version_str(REQUIRED_NPURUN)}. Run  hexcli --update.")

    if not _npurun_model_ok():
        print(f"  Downloading {NPURUN_MODEL}...", flush=True)
        try:
            _pull_npurun_model()
            _write_npurun_config()
        except Exception as exc:
            return _fail(f"Model download failed: {exc}")
    elif not NPURUN_CONFIG.exists():
        _write_npurun_config()

    if not _is_npurun_up():
        try:
            _start_npurun_server()
        except Exception as exc:
            return _fail(f"The model server did not start: {exc}")
        if not _wait_npurun(timeout=60):
            return _fail("The model server did not start within 60 s.", f"Log: {NPURUN_LOG}")

    # The REPL gets the server's environment too: an in-session restart
    # (/undo a dead server, "Restart the model server? [Y/n]") then brings
    # up the same SDK and Rewind settings, not whatever the shell had.
    return subprocess.run(
        [sys.executable, "-m", "hexcli.agent", "--config", str(NPURUN_CONFIG), *sys.argv[1:]],
        env=_npurun_env(),
    ).returncode


# Flags the REPL answers on its own; no server needed, so `hex --version`
# must not start one (or write the runtime config) first.
_NO_SERVER_FLAGS = frozenset({"-h", "--help", "--version", "--doctor", "--update",
                              "--uninstall", "--print-config"})

def main() -> int:
    if any(a in _NO_SERVER_FLAGS for a in sys.argv[1:]):
        from . import agent
        return agent.main()
    try:
        _dress_console_window()
    except Exception:
        pass  # cosmetics only; never block launch over them

    # The NPU path is the product (CLAUDE.md §3): a missing prerequisite is
    # reported with the fix, never silently substituted.
    try:
        if not _npurun_ready():
            return _fail("npurun or the QAIRT SDK was not found.", "Run  hexcli --doctor  for the fix.")
        return run_npurun_path()
    except KeyboardInterrupt:
        print()
        return 0
    except Exception as exc:
        return _fail(f"Error: {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
