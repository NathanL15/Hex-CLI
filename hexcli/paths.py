"""hexcli.paths — where the app keeps its things.

Two homes:

* the package (``hexcli/``), read-only once installed: code and the icon;
* the data directory, ``~/.shellai``: the user config, the runtime config
  the launcher writes, the embedding model for memory, the server log,
  the session history, chat logs, memory stores, input history.

A git checkout is also recognised (``pyproject.toml`` next to the
package). It never wins over the data directory, but files that older
versions kept in the checkout — ``shellai.json``, ``history.json``,
``onnx/``, a downloaded ``npurun-arm64.exe`` — are still found there, so
a developer's checkout keeps working and a first run migrates the
history file rather than losing it.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
CHECKOUT_DIR: Path | None = (
    PACKAGE_DIR.parent if (PACKAGE_DIR.parent / "pyproject.toml").exists() else None
)
DATA_DIR_NAME = ".shellai"

EMBEDDING_MODEL_FILE = "model_qint8_arm64.onnx"
EMBEDDING_TOKENIZER_FILE = "tokenizer.json"
EMBEDDING_BASE_URL = "https://huggingface.co/sentence-transformers/all-MiniLM-L6-v2/resolve/main"
NPURUN_ASSET = "npurun-arm64.exe"


def data_dir(create: bool = True) -> Path:
    """``~/.shellai`` (``HEXCLI_HOME`` overrides it, for tests and sandboxes)."""
    override = os.environ.get("HEXCLI_HOME")
    d = Path(override).expanduser() if override else Path.home() / DATA_DIR_NAME
    if create:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass
    return d


def _first_existing(*candidates: Path | None) -> Path | None:
    for c in candidates:
        if c is not None and c.exists():
            return c
    return None


def user_config_path() -> Path:
    """``~/.shellai/shellai.json``; a checkout's ``shellai.json`` if only that exists."""
    home = data_dir() / "shellai.json"
    checkout = CHECKOUT_DIR / "shellai.json" if CHECKOUT_DIR else None
    return _first_existing(home, checkout) or home


def runtime_config_path() -> Path:
    """The config the launcher writes for the npurun server (backend wiring)."""
    home = data_dir() / "shellai_npurun.json"
    checkout = CHECKOUT_DIR / "shellai_npurun.json" if CHECKOUT_DIR else None
    return _first_existing(home, checkout) or home


def history_path() -> Path:
    """Session history. Migrated once from a checkout's ``history.json``."""
    home = data_dir() / "history.json"
    if not home.exists() and CHECKOUT_DIR is not None:
        old = CHECKOUT_DIR / "history.json"
        if old.exists():
            try:
                shutil.copy2(old, home)
            except OSError:
                return old
    return home


def npurun_log_path() -> Path:
    return data_dir() / "npurun_server.log"


def embedding_dir(for_download: bool = False) -> Path:
    """Where the MiniLM files live: ``~/.shellai/onnx``, or a checkout's
    ``onnx/`` when only that has them. ``for_download`` asks for the place
    to put them."""
    home = data_dir() / "onnx"
    if for_download:
        return home
    checkout = CHECKOUT_DIR / "onnx" if CHECKOUT_DIR else None
    for d in (home, checkout):
        if d is not None and (d / EMBEDDING_MODEL_FILE).exists():
            return d
    return home


def embedding_model_path() -> Path:
    return embedding_dir() / EMBEDDING_MODEL_FILE


def embedding_tokenizer_path() -> Path:
    return embedding_dir() / EMBEDDING_TOKENIZER_FILE


def npurun_bin_dir() -> Path:
    """Where a downloaded npurun binary goes (``~/.shellai/bin``)."""
    return data_dir() / "bin"


def npurun_download_candidates() -> list[Path]:
    """Downloaded binaries, newest home first, then a checkout's copy."""
    out = [npurun_bin_dir() / NPURUN_ASSET]
    if CHECKOUT_DIR is not None:
        out.append(CHECKOUT_DIR / NPURUN_ASSET)
    return out


def icon_path() -> Path:
    return PACKAGE_DIR / "assets" / "hexcli.ico"


def icon_png_path() -> Path:
    return PACKAGE_DIR / "assets" / "hexcli.png"
