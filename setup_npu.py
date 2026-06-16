#!/usr/bin/env python3
"""
setup_npu.py — Set up the ONNX Runtime GenAI + QNN stack for Hexagon NPU inference.

Run this from the shellai directory:
    python setup_npu.py

What it does:
  1. Creates a dedicated conda environment  (shellai-npu, Python 3.11)
  2. Installs onnxruntime-genai + onnxruntime-qnn
  3. Downloads a QNN-ready LLM (Phi-3.5 mini by default)
  4. Verifies the QNN provider loads against the model
  5. Writes shellai_npu.json so you can launch shellai in NPU mode

NPU reality check (Snapdragon X Elite):
  - onnxruntime-qnn provides QNNExecutionProvider (Hexagon HTP backend)
  - The model's genai_config.json must request QNNExecutionProvider
  - Microsoft's pre-built Phi-3.5 mini ONNX model includes a QNN variant
  - If QNN fails the session falls back to CPU automatically
"""
from __future__ import annotations

import json
import os
import platform
import shutil
import subprocess
import sys
from pathlib import Path

APP_DIR = Path(__file__).resolve().parent
MODELS_DIR = APP_DIR / "models"
ENV_NAME = "shellai-npu"
SERVER_PORT = 8123

# ---------------------------------------------------------------------------
# Recommended models — pick one to download
# ---------------------------------------------------------------------------

MODELS = {
    "phi35-mini-npu": {
        "hf_repo": "microsoft/Phi-3.5-mini-instruct-onnx",
        "hf_subfolder": "npu/directml",          # QNN context binary for Snapdragon
        "hf_subfolder_fallback": "cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4",
        "local_dir": "phi-3.5-mini-npu",
        "template": "phi3",
        "description": "Phi-3.5 Mini (3.8B, INT4) — Microsoft's Snapdragon-optimised ONNX. "
                       "Uses QNN/NPU when the npu/directml subfolder is available.",
    },
    "phi3-mini-cpu": {
        "hf_repo": "microsoft/Phi-3-mini-4k-instruct-onnx",
        "hf_subfolder": "cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4",
        "hf_subfolder_fallback": None,
        "local_dir": "phi-3-mini-cpu",
        "template": "phi3",
        "description": "Phi-3 Mini (3.8B, INT4) — CPU INT4 fallback. "
                       "Reliable baseline; you can add a QNN genai_config.json later.",
    },
    "qwen25-coder-3b": {
        "hf_repo": "Qwen/Qwen2.5-Coder-3B-Instruct-ONNX",
        "hf_subfolder": "onnx/cpu_and_mobile/cpu-int4-rtn-block-32-acc-level-4",
        "hf_subfolder_fallback": None,
        "local_dir": "qwen25-coder-3b",
        "template": "qwen",
        "description": "Qwen 2.5 Coder 3B (INT4) — excellent for code/shell tasks. "
                       "Good match for shellai's autopilot mode.",
    },
}

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
    print(f"  > {' '.join(cmd)}")
    return subprocess.run(cmd, check=True, **kwargs)


def find_conda() -> Path | None:
    for candidate in (
        Path.home() / "miniconda3" / "Scripts" / "conda.exe",
        Path.home() / "anaconda3" / "Scripts" / "conda.exe",
        Path("C:/ProgramData/miniconda3/Scripts/conda.exe"),
    ):
        if candidate.exists():
            return candidate
    return Path(shutil.which("conda")) if shutil.which("conda") else None


def conda_python(conda: Path) -> Path:
    """Return the python.exe inside the shellai-npu env."""
    env_root = conda.parent.parent / "envs" / ENV_NAME
    return env_root / "python.exe"


def env_exists(conda: Path) -> bool:
    result = subprocess.run(
        [str(conda), "env", "list"], capture_output=True, text=True
    )
    return ENV_NAME in result.stdout


# ---------------------------------------------------------------------------
# Steps
# ---------------------------------------------------------------------------

def step_check_platform() -> None:
    print("\n── 1. Platform check ────────────────────────────────────")
    mach = platform.machine()
    proc = platform.processor()
    print(f"  Architecture: {mach}")
    print(f"  Processor:    {proc}")
    if "ARM" not in mach.upper() and "AARCH" not in mach.upper():
        print("  WARNING: This machine does not appear to be ARM/Snapdragon.")
        print("           onnxruntime-qnn targets ARM64 Windows. Continuing anyway.")
    else:
        print("  ARM64 confirmed — QNN provider should be available.")


def step_create_env(conda: Path) -> None:
    print(f"\n── 2. Conda environment '{ENV_NAME}' ───────────────────")
    if env_exists(conda):
        print(f"  Environment '{ENV_NAME}' already exists. Skipping creation.")
    else:
        run([str(conda), "create", "-n", ENV_NAME, "python=3.11", "-y"])
        print(f"  Created '{ENV_NAME}'.")


def step_install_packages(conda: Path) -> None:
    print("\n── 3. Install packages ──────────────────────────────────")
    py = str(conda_python(conda))
    packages = [
        "onnxruntime-genai",   # generation loop + tokenizer
        "onnxruntime-qnn",     # QNNExecutionProvider (Hexagon NPU)
        "huggingface_hub",     # model download
    ]
    # onnxruntime-qnn and onnxruntime must not both be installed — qnn includes its own ORT.
    # Uninstall plain onnxruntime if present.
    subprocess.run([py, "-m", "pip", "uninstall", "-y", "onnxruntime"], capture_output=True)
    run([py, "-m", "pip", "install", "--upgrade", *packages])
    print("  Packages installed.")


def step_check_qnn(conda: Path) -> bool:
    print("\n── 4. QNN provider check ────────────────────────────────")
    py = str(conda_python(conda))
    check = subprocess.run(
        [py, "-c",
         "import onnxruntime as ort; p = ort.get_available_providers();"
         " print('providers:', p);"
         " exit(0 if 'QNNExecutionProvider' in p else 1)"],
        capture_output=True, text=True,
    )
    print(f"  {check.stdout.strip()}")
    if check.returncode == 0:
        print("  \033[92mQNNExecutionProvider available — Hexagon NPU will be used.\033[0m")
        return True
    else:
        print("  \033[93mQNNExecutionProvider not detected. Will use CPU fallback.\033[0m")
        print("  (The server still works; the model runs on CPU.)")
        return False


def step_download_model(conda: Path, model_key: str) -> Path:
    print(f"\n── 5. Download model ({model_key}) ──────────────────────")
    info = MODELS[model_key]
    py = str(conda_python(conda))
    local_dir = MODELS_DIR / info["local_dir"]
    MODELS_DIR.mkdir(exist_ok=True)

    # Try primary subfolder (NPU), then fallback
    for subfolder in filter(None, [info["hf_subfolder"], info.get("hf_subfolder_fallback")]):
        print(f"  Trying subfolder: {subfolder}")
        result = subprocess.run(
            [py, "-c", f"""
from huggingface_hub import snapshot_download
import sys
try:
    path = snapshot_download(
        repo_id={info['hf_repo']!r},
        allow_patterns=[{subfolder!r} + "/*"],
        local_dir={str(local_dir)!r},
        local_dir_use_symlinks=False,
    )
    print("ok:", path)
except Exception as e:
    print("err:", e, file=sys.stderr)
    sys.exit(1)
"""],
            capture_output=True, text=True,
        )
        if result.returncode == 0:
            print(f"  Downloaded to: {local_dir / subfolder}")
            # The actual model dir is local_dir/subfolder
            model_path = local_dir / subfolder.replace("/", os.sep)
            if model_path.exists():
                return model_path
            # Some repos put files directly in local_dir
            return local_dir
        print(f"  {result.stderr.strip()}")

    raise RuntimeError(
        f"Could not download model '{model_key}'. "
        "Check your internet connection or try a different model."
    )


def _qnn_genai_config(model_dir: Path) -> dict:
    """Generate a genai_config.json that enables QNNExecutionProvider."""
    return {
        "model": {
            "decoder": {
                "session_options": {
                    "provider_options": [
                        {
                            "provider_name": "QNNExecutionProvider",
                            "options": {
                                "backend_type": "htp",
                                "htp_performance_mode": "burst",
                                "qnn_context_priority": "high",
                                "profiling_level": "off",
                            },
                        }
                    ]
                }
            }
        }
    }


def step_patch_qnn_config(model_dir: Path, qnn_available: bool) -> None:
    print("\n── 6. genai_config.json ─────────────────────────────────")
    cfg_path = model_dir / "genai_config.json"
    if cfg_path.exists():
        existing = json.loads(cfg_path.read_text())
        providers = (
            existing.get("model", {})
            .get("decoder", {})
            .get("session_options", {})
            .get("provider_options", [])
        )
        if any("QNN" in str(p) for p in providers):
            print("  genai_config.json already requests QNNExecutionProvider. Nothing to do.")
            return
        print("  Existing genai_config.json found but no QNN provider. Patching...")
    else:
        print("  No genai_config.json found. Creating one with QNN settings...")

    if not qnn_available:
        print("  (QNN not available on this system; config written for future use.)")

    cfg = _qnn_genai_config(model_dir)

    # Merge with existing if present
    if cfg_path.exists():
        try:
            base = json.loads(cfg_path.read_text())
        except Exception:
            base = {}
        # Deep-merge: we only add the provider_options
        model_cfg = base.setdefault("model", {})
        decoder_cfg = model_cfg.setdefault("decoder", {})
        sess_opts = decoder_cfg.setdefault("session_options", {})
        existing_providers = sess_opts.get("provider_options", [])
        qnn_po = cfg["model"]["decoder"]["session_options"]["provider_options"][0]
        if not any("QNN" in str(p) for p in existing_providers):
            sess_opts["provider_options"] = [qnn_po] + existing_providers
        cfg_path.write_text(json.dumps(base, indent=2))
    else:
        cfg_path.write_text(json.dumps(cfg, indent=2))

    print(f"  Written: {cfg_path}")


def step_write_shellai_config(model_dir: Path, model_key: str) -> None:
    print("\n── 7. Write shellai_npu.json ────────────────────────────")
    template = MODELS[model_key]["template"]
    config = {
        "backend": "openai",
        "model": model_dir.name,
        "temperature": 0.1,
        "timeout_seconds": 300,
        "max_output_tokens": 512,
        "chat_max_output_tokens": 1024,
        "autopilot_max_output_tokens": 2048,
        "max_agent_steps": 15,
        "tool_output_limit": 12000,
        "use_streaming": True,
        "openai_compatible": {
            "base_url": f"http://127.0.0.1:{SERVER_PORT}/v1",
            "api_key": "local",
        },
        "_npu_model_path": str(model_dir),
        "_npu_template": template,
    }
    out = APP_DIR / "shellai_npu.json"
    out.write_text(json.dumps(config, indent=2))
    print(f"  Written: {out}")


def step_print_launch_instructions(conda: Path, model_dir: Path, model_key: str) -> None:
    template = MODELS[model_key]["template"]
    py = str(conda_python(conda))
    server_cmd = f'{py} "{APP_DIR / "npu_server.py"}" --model "{model_dir}" --template {template} --port {SERVER_PORT}'
    shellai_cmd = f'python "{APP_DIR / "shellai.py"}" --config "{APP_DIR / "shellai_npu.json"}"'

    print("\n── Done ─────────────────────────────────────────────────")
    print()
    print("  \033[1mTo run in NPU mode:\033[0m")
    print()
    print("  1. Start the NPU inference server (in a separate terminal):")
    print(f"     {server_cmd}")
    print()
    print("  2. Start shellai pointing at shellai_npu.json:")
    print(f"     {shellai_cmd}")
    print()
    print("  Or use the launcher:")
    print(f'     "{APP_DIR}\\npu_server.cmd"')
    print()
    print("  Task Manager → NPU tab should show activity during inference.")
    print()


# ---------------------------------------------------------------------------
# Menu
# ---------------------------------------------------------------------------

def choose_model() -> str:
    print("\n  Available models:")
    keys = list(MODELS.keys())
    for i, k in enumerate(keys, 1):
        info = MODELS[k]
        print(f"    {i}. {k}")
        print(f"       {info['description']}")
        print()
    while True:
        choice = input(f"  Choose a model [1-{len(keys)}]: ").strip()
        if choice.isdigit() and 1 <= int(choice) <= len(keys):
            return keys[int(choice) - 1]
        print("  Invalid choice.")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    print()
    print("  \033[1mshellai NPU Setup\033[0m — ONNX Runtime GenAI + QNN (Hexagon NPU)")
    print()

    conda = find_conda()
    if not conda:
        print("  conda not found. Install Miniconda first:")
        print("  https://docs.conda.io/en/latest/miniconda.html")
        sys.exit(1)
    print(f"  conda: {conda}")

    step_check_platform()

    model_key = choose_model()

    step_create_env(conda)
    step_install_packages(conda)
    qnn_ok = step_check_qnn(conda)

    try:
        model_dir = step_download_model(conda, model_key)
    except RuntimeError as exc:
        print(f"\n  [error] {exc}", file=sys.stderr)
        sys.exit(1)

    step_patch_qnn_config(model_dir, qnn_ok)
    step_write_shellai_config(model_dir, model_key)
    step_print_launch_instructions(conda, model_dir, model_key)


if __name__ == "__main__":
    main()
