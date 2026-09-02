#!/usr/bin/env python3
"""hexcli.doctor — diagnose an installation and say exactly how to fix it.

The QAIRT SDK cannot be redistributed and the NPU bundles are multi-GB, so
this project can never be a one-click install. The honest response is to
diagnose perfectly: every check prints PASS/WARN/FAIL plus the exact command
that fixes it.

This exists because of a real failure: the ONNX embedding model was never
installed on the development machine, so semantic memory silently no-opped
for months (docs/V2_PLAN.md §14.1). `first_run_check` printed a hint that
scrolled past. Checks that only whisper are checks that don't work.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ui import C, cprint

PASS, WARN, FAIL = "PASS", "WARN", "FAIL"


@dataclass
class Check:
    name: str
    status: str
    detail: str
    fix: str = ""


def _mark(status: str) -> str:
    return {
        PASS: f"{C.BGREEN}  ok  {C.RESET}",
        WARN: f"{C.BYELLOW} warn {C.RESET}",
        FAIL: f"{C.BRED} fail {C.RESET}",
    }[status]


def check_python() -> Check:
    v = sys.version_info
    if v < (3, 10):
        return Check("Python", FAIL, f"{v.major}.{v.minor} — 3.10+ required",
                     "Install Python 3.11+ from python.org (ARM64 build).")
    return Check("Python", PASS, f"{v.major}.{v.minor}.{v.micro} ({sys.executable})")


def check_packages() -> list[Check]:
    out: list[Check] = []
    for mod, why, fix in (
        ("numpy", "vector math for memory", "pip install numpy"),
        ("onnxruntime", "embedding model runtime", "pip install onnxruntime"),
    ):
        try:
            __import__(mod)
            out.append(Check(mod, PASS, "importable"))
        except ImportError:
            out.append(Check(mod, WARN, f"missing — {why} disabled", fix))
    return out


def check_embedding_model(app_dir: Path) -> list[Check]:
    """The silent-failure case that motivated this whole command."""
    model = app_dir / "onnx" / "model_qint8_arm64.onnx"
    tok = app_dir / "onnx" / "tokenizer.json"
    fix = (
        "Download both into onnx/ :\n"
        "      curl -L -o onnx/model_qint8_arm64.onnx https://huggingface.co/"
        "sentence-transformers/all-MiniLM-L6-v2/resolve/main/onnx/model_qint8_arm64.onnx\n"
        "      curl -L -o onnx/tokenizer.json https://huggingface.co/"
        "sentence-transformers/all-MiniLM-L6-v2/resolve/main/tokenizer.json"
    )
    checks = []
    if model.exists() and model.stat().st_size > 1_000_000:
        checks.append(Check("embedding model", PASS, f"{model.stat().st_size // 1_000_000} MB"))
    else:
        checks.append(Check("embedding model", WARN,
                            "missing — semantic memory silently does nothing", fix))
    if tok.exists():
        checks.append(Check("embedding tokenizer", PASS, "present"))
    else:
        checks.append(Check("embedding tokenizer", WARN, "missing — memory disabled", fix))
    return checks


def _launcher():
    try:
        import launcher
        return launcher
    except Exception:
        return None


def check_qairt() -> list[Check]:
    sdk = Path(os.environ.get("QNN_SDK_ROOT", r"C:\Qualcomm\AIStack\QAIRT_2.47.0"))
    ln = _launcher()
    if ln is not None:
        sdk = ln.QNN_SDK_ROOT   # what the launcher actually exports (newest valid; Rewind SDK first)
    checks: list[Check] = []
    if not sdk.exists():
        checks.append(Check("QAIRT SDK", FAIL, f"not found at {sdk}",
                            "Download from the Qualcomm developer portal (free account, "
                            "not redistributable), then set QNN_SDK_ROOT."))
        return checks
    checks.append(Check("QAIRT SDK", PASS, str(sdk)))
    lib = sdk / "lib" / "aarch64-windows-msvc"
    checks.append(Check("QAIRT libs", PASS if lib.exists() else FAIL,
                        str(lib) if lib.exists() else "missing aarch64-windows-msvc libs",
                        "" if lib.exists() else "Re-run the QAIRT installer."))
    adsp = Path(os.environ.get("ADSP_LIBRARY_PATH", ""))
    if not adsp.exists():
        checks.append(Check("ADSP_LIBRARY_PATH", WARN,
                            "unset — npurun crashes with STATUS_STACK_BUFFER_OVERRUN without it",
                            rf'setx ADSP_LIBRARY_PATH "{sdk}\lib\hexagon-v73\unsigned"'))
    else:
        checks.append(Check("ADSP_LIBRARY_PATH", PASS, str(adsp)))
    return checks


def check_npurun() -> list[Check]:
    exe = shutil.which("npurun") or shutil.which("npurun.exe")
    local = Path.home() / ".cargo" / "bin" / "npurun.exe"
    path = Path(exe) if exe else (local if local.exists() else None)
    if path is None:
        return [Check("npurun", FAIL, "binary not found",
                      "Build it: cd npurun && scripts\\dev-shell-local.bat "
                      "cargo install --path crates\\npurun-cli")]
    checks = [Check("npurun", PASS, str(path))]
    ln = _launcher()
    if ln is not None:
        ver = ".".join(str(n) for n in ln._npurun_version(path)) or "unknown"
        if ln.REWIND_ROOT is not None:
            checks.append(Check("KV prefix reuse", PASS,
                                f"on — npurun {ver}, {ln.REWIND_ROOT.name}"))
        else:
            checks.append(Check("KV prefix reuse", WARN,
                                f"off — npurun {ver}, {ln.QNN_SDK_ROOT.name}",
                                "Needs QAIRT >= 2.50 under C:/Qualcomm/AIStack and npurun >= 0.2.0 "
                                "(turns are ~40% faster with it)."))
    try:
        r = subprocess.run([str(path), "list"], capture_output=True, text=True, timeout=20)
        models = [ln.split()[0] for ln in r.stdout.splitlines() if ln.strip()]
        if models:
            checks.append(Check("model bundles", PASS, ", ".join(models)))
        else:
            checks.append(Check("model bundles", FAIL, "none downloaded",
                                "npurun pull qwen3-4b-instruct-2507   (~2.5 GB)"))
    except Exception as exc:
        checks.append(Check("model bundles", WARN, f"could not list ({exc.__class__.__name__})"))
    return checks


def check_server(config: dict[str, Any]) -> Check:
    base = str(config.get("openai_compatible", {}).get("base_url", ""))
    if not base:
        return Check("model server", WARN, "no openai_compatible.base_url configured")
    host = base.split("//")[-1].split("/")[0]
    try:
        with urllib.request.urlopen(f"http://{host}/healthz", timeout=3) as r:
            if r.status == 200:
                detail = f"healthy at {host}"
                try:
                    with urllib.request.urlopen(f"http://{host}/v1/models", timeout=3) as m:
                        models = json.loads(m.read().decode("utf-8")).get("data") or []
                    first = models[0] if models else {}
                    if first.get("input_token_budget"):
                        detail += (f" — input budget {first['input_token_budget']} of "
                                   f"{first.get('context_size', '?')} tokens")
                except Exception:
                    pass
                return Check("model server", PASS, detail)
    except Exception:
        pass
    return Check("model server", WARN, f"not responding at {host}",
                 "Start it: python launcher.py   (or hexcli will offer to "
                 "restart it when a turn fails)")


def check_ruff() -> Check:
    if shutil.which("ruff"):
        return Check("ruff (optional)", PASS, "on PATH — lint_code tool enabled")
    return Check("ruff (optional)", WARN, "not found — lint_code tool is hidden from the agent",
                 "pip install ruff")


def check_workspace() -> list[Check]:
    cwd = Path.cwd()
    checks = [Check("working directory", PASS, str(cwd))]
    for name in ("AGENTS.md", ".shellai/AGENTS.md", "HEXCLI.md"):
        if (cwd / name).is_file():
            checks.append(Check("project instructions", PASS, f"{name} found"))
            break
    else:
        checks.append(Check("project instructions", WARN,
                            "no AGENTS.md — the agent has no project-specific rules",
                            "Create AGENTS.md with a few lines about this project."))
    return checks


def run_doctor(config: dict[str, Any], app_dir: Path) -> int:
    """Print the full report. Returns 1 if any check FAILed."""
    checks: list[Check] = [check_python()]
    checks += check_packages()
    checks += check_embedding_model(app_dir)
    checks += check_qairt()
    checks += check_npurun()
    checks.append(check_server(config))
    checks.append(check_ruff())
    checks += check_workspace()

    print()
    cprint("Hex CLI installation check", C.BOLD)
    print()
    for c in checks:
        print(f"  {_mark(c.status)}  {c.name:<22} {c.detail}")
        if c.fix and c.status != PASS:
            for line in c.fix.splitlines():
                cprint(f"          → {line}", C.DIM)
    fails = sum(1 for c in checks if c.status == FAIL)
    warns = sum(1 for c in checks if c.status == WARN)
    print()
    if fails:
        cprint(f"  {fails} blocking problem(s), {warns} warning(s).", C.BRED)
    elif warns:
        cprint(f"  Ready, with {warns} optional feature(s) unavailable.", C.BYELLOW)
    else:
        cprint("  Everything checks out.", C.BGREEN)
    print()
    return 1 if fails else 0
