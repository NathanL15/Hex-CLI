#!/usr/bin/env python3
"""launcher.py — Self-bootstrapping launcher for shellai with NPU/GPU acceleration.

Run by  Hex CLI.cmd  on every launch.

Priority order:
  1. Qwen3-4B via npurun (Genie/QNN, Hexagon NPU) — ~15 tok/s, requires QAIRT SDK
  2. Phi-4-mini via onnxruntime-genai (Adreno GPU/DirectML) — auto-downloaded on first run
  3. Ollama CPU fallback

First run picks the best available model, installs deps, writes configs.
Subsequent runs just start the server and shellai.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Windows consoles often default to cp1252, which can't encode the arrows/
# checkmarks this script prints. Force UTF-8 so it works regardless of caller.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

APP_DIR        = Path(__file__).resolve().parent
MODELS_DIR     = APP_DIR / "models"
ENV_NAME       = "shellai-npu"
SHELLAI_SCRIPT = APP_DIR / "shellai.py"

# npurun — Qwen3-4B on Hexagon NPU via Qualcomm Genie SDK.
# Discovery mirrors install.ps1: a source build wins, then the prebuilt
# binary the installer downloads next to this script, then PATH.

def find_npurun_exe(home: Path | None = None, app_dir: Path | None = None) -> Path | None:
    home = home or Path.home()
    app_dir = app_dir or APP_DIR
    for candidate in (
        home / ".cargo" / "bin" / "npurun.exe",
        app_dir / "npurun-arm64.exe",
    ):
        if candidate.exists():
            return candidate
    import shutil
    found = shutil.which("npurun")
    return Path(found) if found else None


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
        warn(f"QNN_SDK_ROOT={env_value} is missing the expected "
             f"lib/bin/aarch64-windows-msvc and lib/hexagon-v73/unsigned "
             f"layout — ignoring it and searching for another install.")
    stack = stack_dir or Path("C:/Qualcomm/AIStack")
    if stack.exists():
        for candidate in sorted(stack.glob("QAIRT_*"), key=_qairt_version_key, reverse=True):
            if _qairt_valid(candidate):
                return candidate
    return None


NPURUN_MODEL     = "qwen3-4b-instruct-2507"
NPURUN_MODEL_DIR = Path.home() / "AppData" / "Local" / "npurun" / "models" / NPURUN_MODEL
NPURUN_PORT      = 11435
NPURUN_LOG       = APP_DIR / "npurun_server.log"
NPURUN_CONFIG    = APP_DIR / "shellai_npurun.json"

# Phi-4-mini DirectML fallback
DML_PORT         = 8123
DML_SERVER       = APP_DIR / "npu_server.py"
DML_LOG          = APP_DIR / "npu_server.log"
DML_CONFIG       = APP_DIR / "shellai_npu.json"
DML_MARKER       = APP_DIR / ".npu-ready"
HF_REPO          = "microsoft/Phi-4-mini-instruct-onnx"
HF_SUBFOLDER     = "gpu/gpu-int4-rtn-block-32"
DML_MODEL_SUBDIR = "phi4-mini-gpu"

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
    hwnd = k32.GetConsoleWindow()
    ico = APP_DIR / "assets" / "hexcli.ico"
    if not (hwnd and ico.exists()):
        return
    u32 = ctypes.windll.user32
    WM_SETICON, IMAGE_ICON, LR_LOADFROMFILE = 0x80, 1, 0x10
    for which, size in ((0, 16), (1, 32)):  # ICON_SMALL, ICON_BIG
        h_icon = u32.LoadImageW(None, str(ico), IMAGE_ICON, size, size,
                                LR_LOADFROMFILE)
        if h_icon:
            u32.SendMessageW(hwnd, WM_SETICON, which, h_icon)


try:
    _dress_console_window()
except Exception:
    pass  # cosmetics only; never block launch over them

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
    print(f"        {yellow('!')} {msg}", flush=True)


# Resolved after the printing helpers exist: find_qairt_root() warns when it
# rejects an explicitly-set QNN_SDK_ROOT, and a NameError there would crash
# the launcher at import for exactly the users that warning is meant for.
NPURUN_EXE   = find_npurun_exe() or (Path.home() / ".cargo" / "bin" / "npurun.exe")
QNN_SDK_ROOT = find_qairt_root() or Path("C:/Qualcomm/AIStack/QAIRT_2.47.0")

def err(msg: str) -> None:
    print(f"        {red('✗')} {msg}", flush=True)

# ---------------------------------------------------------------------------
# conda helpers
# ---------------------------------------------------------------------------

def find_conda() -> Path | None:
    for candidate in (
        Path.home() / "miniconda3"  / "Scripts" / "conda.exe",
        Path.home() / "anaconda3"   / "Scripts" / "conda.exe",
        Path("C:/ProgramData/miniconda3/Scripts/conda.exe"),
        Path("C:/ProgramData/anaconda3/Scripts/conda.exe"),
    ):
        if candidate.exists():
            return candidate
    import shutil
    found = shutil.which("conda")
    return Path(found) if found else None


def env_python(conda: Path) -> Path:
    return conda.parent.parent / "envs" / ENV_NAME / "python.exe"


def env_exists(conda: Path) -> bool:
    r = subprocess.run([str(conda), "env", "list"], capture_output=True, text=True)
    return ENV_NAME in r.stdout


def _run(cmd: list[str], **kwargs) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kwargs)

# ---------------------------------------------------------------------------
# Server health checks
# ---------------------------------------------------------------------------

def _is_up(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _wait(port: int, timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if _is_up(port):
            return True
        time.sleep(1)
        dots += 1
        print(f"\r  Waiting for server{'.' * (dots % 4)}   ", end="", flush=True)
    print()
    return False

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
    return env


def _is_npurun_up() -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{NPURUN_PORT}/healthz", timeout=2) as r:
            return r.status == 200
    except Exception:
        return False


def _wait_npurun(timeout: int = 60) -> bool:
    deadline = time.time() + timeout
    dots = 0
    while time.time() < deadline:
        if _is_npurun_up():
            return True
        time.sleep(1)
        dots += 1
        print(f"\r  Waiting for server{'.' * (dots % 4)}   ", end="", flush=True)
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
        "chat_max_output_tokens": 2048,
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
    NPURUN_CONFIG.write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    ok(str(NPURUN_CONFIG))


def _start_npurun_server() -> None:
    cmd = [str(NPURUN_EXE), "serve", "--model", NPURUN_MODEL,
           "--bind", f"127.0.0.1:{NPURUN_PORT}"]
    log = open(str(NPURUN_LOG), "w", encoding="utf-8")
    subprocess.Popen(cmd, stdout=log, stderr=log, env=_npurun_env(),
                      creationflags=0x00000008)


def run_npurun_path(conda: Path | None) -> int:
    """Full npurun setup and launch. Returns exit code."""
    print()
    print(bold("  ─── Qwen3-4B on Hexagon NPU (Genie/QNN) ─────────────────"))
    print(f"  Model:  {bold('Qwen3-4B-Instruct-2507')}  w4a16  (~2.5 GB, ~15 tok/s)")
    print(f"  Engine: {bold('npurun')}  (Genie SDK / Hexagon HTP)")
    print()

    if not _npurun_model_ok():
        print(f"  Downloading {NPURUN_MODEL} …", flush=True)
        try:
            _pull_npurun_model()
            _write_npurun_config()
            print()
        except Exception as exc:
            err(f"npurun pull failed: {exc}")
            warn("Falling back to DirectML / Phi-4-mini path.")
            if conda:
                return run_dml_path(conda)
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode
    elif not NPURUN_CONFIG.exists():
        _write_npurun_config()

    if not _is_npurun_up():
        print(f"  Starting npurun server on port {NPURUN_PORT} …", flush=True)
        try:
            _start_npurun_server()
        except Exception as exc:
            err(f"Cannot start npurun server: {exc}")
            warn("Falling back to DirectML / Phi-4-mini.")
            if conda:
                return run_dml_path(conda)
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode

        if not _wait_npurun(timeout=60):
            print()
            err("npurun server did not start within 60 s.")
            print(dim(f"  Check log: {NPURUN_LOG}"))
            warn("Falling back to DirectML / Phi-4-mini.")
            if conda:
                return run_dml_path(conda)
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode

        print(f"\r  {green('✓')} npurun server ready on port {NPURUN_PORT}                  ")
    else:
        print(f"  {green('✓')} npurun server already running on port {NPURUN_PORT}")

    print()
    print(dim("  Model: Qwen3-4B · Engine: npurun (Hexagon NPU via Genie)"))
    print(dim(f"  Server log: {NPURUN_LOG.name}"))
    print()

    return subprocess.run(
        [sys.executable, str(SHELLAI_SCRIPT), "--config", str(NPURUN_CONFIG)]
    ).returncode

# ---------------------------------------------------------------------------
# DirectML fallback — Phi-4-mini on Adreno GPU
# ---------------------------------------------------------------------------

def _setup_dml_env(conda: Path) -> Path:
    TOTAL = 4
    step(1, TOTAL, "Creating conda env  shellai-npu  (Python 3.11) …")
    if env_exists(conda):
        ok("already exists")
    else:
        _run([str(conda), "create", "-n", ENV_NAME, "python=3.11", "-y"])
        ok()

    step(2, TOTAL, "Installing  onnxruntime-genai  +  onnxruntime-directml  …")
    py = str(env_python(conda))
    subprocess.run([py, "-m", "pip", "uninstall", "-y", "onnxruntime",
                    "onnxruntime-qnn"], capture_output=True)
    _run([py, "-m", "pip", "install", "--upgrade",
          "onnxruntime-genai", "onnxruntime-directml", "huggingface_hub[cli]"])
    ok()

    step(3, TOTAL, "Downloading  Phi-4-mini GPU-INT4  (~2.5 GB) …")
    py = str(env_python(conda))
    model_dir = MODELS_DIR / DML_MODEL_SUBDIR
    MODELS_DIR.mkdir(exist_ok=True)
    script = f"""
import sys
from huggingface_hub import snapshot_download
try:
    snapshot_download(
        repo_id={HF_REPO!r},
        allow_patterns=[{HF_SUBFOLDER!r} + "/*"],
        local_dir={str(model_dir)!r},
        local_dir_use_symlinks=False,
    )
    print("ok")
except Exception as e:
    print("err:", e, file=sys.stderr); sys.exit(1)
"""
    r = subprocess.run([py, "-c", script], text=True, capture_output=True)
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or "download failed")
    actual = model_dir / HF_SUBFOLDER.replace("/", os.sep)
    if not actual.exists():
        raise RuntimeError(f"Expected model dir not found: {actual}")
    ok(str(actual))

    step(4, TOTAL, "Writing configs …")
    cfg_path = actual / "genai_config.json"
    try:
        cfg = json.loads(cfg_path.read_text()) if cfg_path.exists() else {}
        sess = cfg.setdefault("model", {}).setdefault("decoder", {}).setdefault("session_options", {})
        existing = sess.get("provider_options", [])
        dml = {"provider_name": "DmlExecutionProvider"}
        if not any("Dml" in str(p) for p in existing):
            sess["provider_options"] = [dml] + existing
        cfg_path.write_text(json.dumps(cfg, indent=2))
    except Exception:
        pass

    dml_cfg = {
        "backend": "openai", "model": "phi4-mini",
        "temperature": 0.1, "timeout_seconds": 300,
        "max_output_tokens": 512, "chat_max_output_tokens": 1024,
        "autopilot_max_output_tokens": 2048, "max_agent_steps": 15,
        "tool_output_limit": 12000, "use_streaming": True,
        "openai_compatible": {"base_url": f"http://127.0.0.1:{DML_PORT}/v1", "api_key": "local"},
        "_npu_model_path": str(actual), "_npu_template": "phi3",
    }
    DML_CONFIG.write_text(json.dumps(dml_cfg, indent=2))
    DML_MARKER.write_text(str(actual))
    ok()
    return actual


def _start_dml_server(conda: Path, model_dir: Path) -> None:
    py = str(env_python(conda))
    cmd = [py, str(DML_SERVER), "--model", str(model_dir),
           "--template", "phi3", "--port", str(DML_PORT)]
    log = open(str(DML_LOG), "w", encoding="utf-8")
    subprocess.Popen(cmd, stdout=log, stderr=log, creationflags=0x00000008)


def run_dml_path(conda: Path) -> int:
    """DirectML / Phi-4-mini path. Returns exit code."""
    if not DML_SERVER.exists():
        warn("DirectML server script not found — falling back to Ollama CPU.")
        return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode
    print()
    print(bold("  ─── Phi-4-mini on Adreno GPU (DirectML) ──────────────────"))
    print(f"  Model:  {bold('Phi-4-mini')}  4.2B INT4  (~2.5 GB)")
    print(f"  Engine: {bold('onnxruntime-genai')}  (DmlExecutionProvider)")
    print()

    model_dir: Path | None = None
    if DML_MARKER.exists():
        p = Path(DML_MARKER.read_text().strip())
        if p.exists():
            model_dir = p
        else:
            DML_MARKER.unlink(missing_ok=True)

    if model_dir is None:
        try:
            model_dir = _setup_dml_env(conda)
            print()
            print(bold("  ─── DirectML setup complete ───────────────────────────"))
            print()
        except Exception as exc:
            err(f"DirectML setup failed: {exc}")
            warn("Falling back to CPU Ollama.")
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode

    if not _is_up(DML_PORT):
        print(f"  Starting Phi-4-mini server on port {DML_PORT} …", flush=True)
        try:
            _start_dml_server(conda, model_dir)
        except Exception as exc:
            err(f"Cannot start DirectML server: {exc}")
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode

        if not _wait(DML_PORT, timeout=60):
            print()
            err("Server did not start within 60 s.")
            print(dim(f"  Check log: {DML_LOG}"))
            warn("Falling back to CPU Ollama.")
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode

        print(f"\r  {green('✓')} DirectML server ready on port {DML_PORT}              ")
    else:
        print(f"  {green('✓')} DirectML server already running on port {DML_PORT}")

    print()
    print(dim("  Model: Phi-4-mini · Provider: DmlExecutionProvider (Adreno GPU)"))
    print(dim(f"  Server log: {DML_LOG.name}"))
    print()

    return subprocess.run(
        [sys.executable, str(SHELLAI_SCRIPT), "--config", str(DML_CONFIG)]
    ).returncode

# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> int:
    print()
    print(bold(cyan("  Hex CLI")))
    print(dim("  Qwen3-4B (Hexagon NPU) → Phi-4-mini (Adreno GPU) → Ollama CPU"))
    print()

    conda = find_conda()

    try:
        # Priority 1: Qwen3-4B on Hexagon NPU via npurun (no conda needed)
        if _npurun_ready():
            print(f"  {green('✓')} npurun + QAIRT SDK found — using Hexagon NPU path")
            return run_npurun_path(conda)

        # Priority 2: Phi-4-mini on Adreno GPU (needs conda)
        print(f"  {yellow('!')} npurun/QAIRT SDK not found — using Phi-4-mini / DirectML")
        if not conda:
            print(yellow("  conda not found — falling back to CPU (Ollama) mode."))
            return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode
        return run_dml_path(conda)

    except KeyboardInterrupt:
        print()
        return 0
    except Exception as exc:
        err(f"Unexpected: {exc}")
        warn("Falling back to CPU Ollama.")
        return subprocess.run([sys.executable, str(SHELLAI_SCRIPT)]).returncode


if __name__ == "__main__":
    raise SystemExit(main())
