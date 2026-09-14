#!/usr/bin/env python3
"""evals/test_v16.py — Unit tests for v1.6 features.

Tests: cloud escalation redaction, escalation gating,
round-trip, and per-project config merge order.
All offline — no LLM endpoint required.

Usage:
    python evals/test_v16.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hexcli.agent as sa

# Offline suites must never wait on a human at a consent prompt.
sa.ui.CONFIRM_TIMEOUT_S = 0.05

# ---------------------------------------------------------------------------
# Feature 16 — Redaction
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Feature 16 — Escalation gating
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Feature 18 — Per-project config merge order
# ---------------------------------------------------------------------------

def test_project_config_overrides_global() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            # Project config pins temperature and max_agent_steps.
            shellai_dir = Path(tmp) / ".shellai"
            shellai_dir.mkdir()
            (shellai_dir / "config.json").write_text(
                json.dumps({"temperature": 0.9, "max_agent_steps": 99}),
                encoding="utf-8",
            )
            # Global config has different temperature.
            global_cfg = Path(tmp) / "shellai.json"
            global_cfg.write_text(
                json.dumps({**sa.DEFAULT_CONFIG, "temperature": 0.3}),
                encoding="utf-8",
            )
            config = sa.load_config(global_cfg)
            assert abs(config["temperature"] - 0.9) < 1e-9, (
                "project config must override global config temperature"
            )
            assert config["max_agent_steps"] == 99, (
                "project config must set max_agent_steps"
            )
        finally:
            os.chdir(orig)


def test_global_config_overrides_defaults() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)  # no project config in this fresh dir
        try:
            global_cfg = Path(tmp) / "shellai.json"
            global_cfg.write_text(
                json.dumps({**sa.DEFAULT_CONFIG, "temperature": 0.75}),
                encoding="utf-8",
            )
            config = sa.load_config(global_cfg)
            assert abs(config["temperature"] - 0.75) < 1e-9, (
                "global config must override built-in defaults"
            )
        finally:
            os.chdir(orig)


def test_defaults_apply_when_no_overrides() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            global_cfg = Path(tmp) / "shellai.json"
            global_cfg.write_text(json.dumps(sa.DEFAULT_CONFIG), encoding="utf-8")
            config = sa.load_config(global_cfg)
            assert config["backend"] == sa.DEFAULT_CONFIG["backend"]
            assert config["max_agent_steps"] == sa.DEFAULT_CONFIG["max_agent_steps"]
        finally:
            os.chdir(orig)


def test_project_config_partial_override() -> None:
    """Project config only overrides keys it specifies; others come from global/default."""
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            shellai_dir = Path(tmp) / ".shellai"
            shellai_dir.mkdir()
            (shellai_dir / "config.json").write_text(
                json.dumps({"max_agent_steps": 7}),
                encoding="utf-8",
            )
            global_cfg = Path(tmp) / "shellai.json"
            global_cfg.write_text(json.dumps(sa.DEFAULT_CONFIG), encoding="utf-8")
            config = sa.load_config(global_cfg)
            assert config["max_agent_steps"] == 7
            assert config["backend"] == sa.DEFAULT_CONFIG["backend"]  # unaffected key
        finally:
            os.chdir(orig)


def test_invalid_project_config_silently_ignored() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            shellai_dir = Path(tmp) / ".shellai"
            shellai_dir.mkdir()
            (shellai_dir / "config.json").write_text("NOT VALID JSON", encoding="utf-8")
            global_cfg = Path(tmp) / "shellai.json"
            global_cfg.write_text(json.dumps(sa.DEFAULT_CONFIG), encoding="utf-8")
            try:
                config = sa.load_config(global_cfg)
            except Exception as exc:
                assert False, f"invalid project config must not raise, got: {exc}"
            assert config["backend"] == sa.DEFAULT_CONFIG["backend"]
        finally:
            os.chdir(orig)


def test_project_config_deep_merge_nested() -> None:
    """Project config deep-merges nested dicts (e.g. openai_compatible)."""
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            shellai_dir = Path(tmp) / ".shellai"
            shellai_dir.mkdir()
            (shellai_dir / "config.json").write_text(
                json.dumps({"openai_compatible": {"base_url": "http://custom:9999/v1"}}),
                encoding="utf-8",
            )
            global_cfg = Path(tmp) / "shellai.json"
            global_cfg.write_text(json.dumps(sa.DEFAULT_CONFIG), encoding="utf-8")
            config = sa.load_config(global_cfg)
            # base_url overridden, api_key still from default
            assert config["openai_compatible"]["base_url"] == "http://custom:9999/v1"
            assert "api_key" in config["openai_compatible"]
        finally:
            os.chdir(orig)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

def _run(fn: Any) -> bool:
    try:
        fn()
        print(f"  PASS  {fn.__name__}")
        return True
    except AssertionError as exc:
        print(f"  FAIL  {fn.__name__}: {exc}")
        return False
    except Exception as exc:
        print(f"  ERROR {fn.__name__}: {type(exc).__name__}: {exc}")
        return False


TESTS = [
    test_project_config_overrides_global,
    test_global_config_overrides_defaults,
    test_defaults_apply_when_no_overrides,
    test_project_config_partial_override,
    test_invalid_project_config_silently_ignored,
    test_project_config_deep_merge_nested,
]


def main() -> int:
    print(f"\nevals/test_v16.py — {len(TESTS)} unit tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
