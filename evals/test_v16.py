#!/usr/bin/env python3
"""evals/test_v16.py — Unit tests for v1.6 features.

Tests: cloud escalation redaction, escalation gating, checkpoint
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
import unittest.mock
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import hexcli.agent as sa
import hexcli.escalate as esc

# Offline suites must never wait on a human at a consent prompt.
sa.ui.CONFIRM_TIMEOUT_S = 0.05

# ---------------------------------------------------------------------------
# Feature 16 — Redaction
# ---------------------------------------------------------------------------

def test_redact_sk_key() -> None:
    text = "My API key is sk-ant-api03-abc123def456ghij and it is secret"
    redacted = esc.redact_text(text)
    assert "sk-ant-api03-abc123def456ghij" not in redacted
    assert "sk-***" in redacted


def test_redact_bearer_token() -> None:
    text = "Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload.sig"
    redacted = esc.redact_text(text)
    assert "eyJhbGciOiJIUzI1NiJ9" not in redacted


def test_redact_password_query_string() -> None:
    text = "db_url?password=hunter2&host=localhost"
    redacted = esc.redact_text(text)
    assert "hunter2" not in redacted
    assert "password=***" in redacted


def test_redact_api_key_param() -> None:
    text = "Request with api_key=super_secret_value_here in params"
    redacted = esc.redact_text(text)
    assert "super_secret_value_here" not in redacted
    assert "api_key=***" in redacted


def test_redact_token_param() -> None:
    text = "Refresh with token=eyRefreshToken123 in body"
    redacted = esc.redact_text(text)
    assert "eyRefreshToken123" not in redacted


def test_redact_postgresql_connection_string() -> None:
    text = "DATABASE_URL=postgresql://admin:s3cr3t@db.prod.example.com:5432/mydb"
    redacted = esc.redact_text(text)
    assert "s3cr3t@db.prod.example.com" not in redacted
    assert "postgresql://***" in redacted


def test_redact_mongodb_connection_string() -> None:
    text = "mongodb://user:pass@cluster0.mongodb.net/myapp?retryWrites=true"
    redacted = esc.redact_text(text)
    assert "user:pass@cluster0" not in redacted


def test_redact_ssh_path_content() -> None:
    ssh_path = str(Path.home() / ".ssh" / "id_rsa")
    text = f"Reading {ssh_path}: -----BEGIN RSA PRIVATE KEY----- abc123privatekey"
    redacted = esc.redact_text(text)
    assert "BEGIN RSA PRIVATE KEY" not in redacted, (
        "content following a sensitive path must be redacted"
    )
    assert "abc123privatekey" not in redacted


def test_redact_aws_path_content() -> None:
    aws_path = str(Path.home() / ".aws" / "credentials")
    text = f"File {aws_path}: aws_access_key_id = AKIAIOSFODNN7EXAMPLE"
    redacted = esc.redact_text(text)
    assert "AKIAIOSFODNN7EXAMPLE" not in redacted


def test_redact_payload_deep_structure() -> None:
    payload = [
        {"role": "user", "content": "My secret sk-prod-abcdefghij1234567890 is here"},
        {"role": "assistant", "content": "Using password=topsecret for the database"},
    ]
    redacted = esc.redact_payload(payload)
    full = json.dumps(redacted)
    assert "sk-prod-abcdefghij1234567890" not in full, "sk- key must be redacted in payload"
    assert "topsecret" not in full, "password must be redacted in payload"


def test_redact_leaves_benign_text_intact() -> None:
    text = "The quick brown fox jumps over the lazy dog."
    assert esc.redact_text(text) == text


def test_redact_value_truncates_long_strings() -> None:
    # Truncation is applied inside _redact_value (payload builder), not redact_text.
    payload = [{"role": "user", "content": "a" * 1000}]
    redacted = esc.redact_payload(payload)
    content = redacted[0]["content"]
    assert len(content) < 800, f"long payload strings must be truncated, got {len(content)} chars"
    assert "truncated" in content, "truncated string must include the ellipsis marker"


# ---------------------------------------------------------------------------
# Feature 16 — Escalation gating
# ---------------------------------------------------------------------------

def test_escalation_returns_message_without_api_key() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    cfg["anthropic_api_key"] = ""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        result = esc.escalate(cfg, [], [])
    assert "ANTHROPIC_API_KEY" in result or "set" in result.lower(), (
        "must mention how to enable when key is absent"
    )


def test_escalation_does_not_raise_without_api_key() -> None:
    cfg: dict[str, Any] = dict(sa.DEFAULT_CONFIG)
    cfg["anthropic_api_key"] = ""
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        try:
            esc.escalate(cfg, [], [])
        except Exception as exc:
            assert False, f"escalate must not raise without API key, got: {exc}"


def test_get_api_key_reads_env_var() -> None:
    cfg: dict[str, Any] = {}
    with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-envkey123456"}):
        key = esc.get_api_key(cfg)
    assert key == "sk-test-envkey123456"


def test_get_api_key_falls_back_to_config() -> None:
    cfg = {"anthropic_api_key": "sk-test-cfgkey987654"}
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    with unittest.mock.patch.dict(os.environ, env, clear=True):
        key = esc.get_api_key(cfg)
    assert key == "sk-test-cfgkey987654"


def test_get_api_key_env_takes_priority_over_config() -> None:
    cfg = {"anthropic_api_key": "sk-config-key"}
    with unittest.mock.patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-env-key"}):
        key = esc.get_api_key(cfg)
    assert key == "sk-env-key", "env var must take priority over config key"


def test_escalation_redacts_before_sending() -> None:
    """Verify that redaction runs on the prompt before any API call."""
    captured_prompt: list[str] = []

    def fake_call_api(api_key: str, model: str, messages: list[dict]) -> str:
        captured_prompt.append(messages[0]["content"])
        return "suggestion"

    cfg = {"anthropic_api_key": "sk-test-fake12345678"}
    turns = [
        {"role": "user", "content": "password=hunter2 in my config"},
        {"role": "assistant", "content": "I see, sk-real-abc123def456 is exposed"},
    ]
    with unittest.mock.patch.object(esc, "_call_api", fake_call_api):
        esc.escalate(cfg, turns, ["run_command"])

    assert captured_prompt, "API must be called when key is present"
    sent = captured_prompt[0]
    assert "hunter2" not in sent, "password must be redacted before sending"
    assert "sk-real-abc123def456" not in sent, "sk- key must be redacted before sending"


def test_escalation_new_keys_in_default_config() -> None:
    assert "anthropic_api_key" in sa.DEFAULT_CONFIG
    assert "escalation_model" in sa.DEFAULT_CONFIG
    assert sa.DEFAULT_CONFIG["escalation_model"] == esc.DEFAULT_ESCALATION_MODEL


# ---------------------------------------------------------------------------
# Feature 17 — Checkpoint round-trip
# ---------------------------------------------------------------------------

def test_checkpoint_save_creates_file() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            session = sa.create_session()
            session["messages"] = [
                {"role": "user", "content": "Hello"},
                {"role": "assistant", "content": "Hi"},
            ]
            cp_path = sa._save_checkpoint("mytest", session, tmp)
            assert cp_path.exists(), "checkpoint file must be created"
        finally:
            os.chdir(orig)


def test_checkpoint_load_restores_messages() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            session = sa.create_session()
            session["messages"] = [
                {"role": "user", "content": "Test message"},
                {"role": "assistant", "content": "Test reply"},
            ]
            sa._save_checkpoint("restore_test", session, tmp)
            loaded = sa._load_checkpoint("restore_test")
            assert loaded is not None
            assert loaded["messages"] == session["messages"]
            assert loaded["message_count"] == 2
        finally:
            os.chdir(orig)


def test_checkpoint_load_nonexistent_returns_none() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            result = sa._load_checkpoint("definitely_does_not_exist")
            assert result is None
        finally:
            os.chdir(orig)


def test_checkpoint_list_returns_all() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            session = sa.create_session()
            session["messages"] = [{"role": "user", "content": "x"}]
            sa._save_checkpoint("cp_alpha", session, tmp)
            sa._save_checkpoint("cp_beta", session, tmp)
            cps = sa._list_checkpoints()
            names = {cp["name"] for cp in cps}
            assert "cp_alpha" in names
            assert "cp_beta" in names
        finally:
            os.chdir(orig)


def test_checkpoint_list_empty_when_no_checkpoints() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            result = sa._list_checkpoints()
            assert result == []
        finally:
            os.chdir(orig)


def test_checkpoint_metadata_includes_workspace() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            session = sa.create_session()
            session["messages"] = []
            sa._save_checkpoint("meta_test", session, tmp)
            loaded = sa._load_checkpoint("meta_test")
            assert "workspace_metadata" in loaded
            assert "created_at" in loaded
            assert loaded["cwd"] == tmp
        finally:
            os.chdir(orig)


def test_checkpoint_name_sanitized() -> None:
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            session = sa.create_session()
            session["messages"] = [{"role": "user", "content": "test"}]
            # Name with special chars
            sa._save_checkpoint("my checkpoint/v1.0", session, tmp)
            # Should still be loadable
            loaded = sa._load_checkpoint("my checkpoint/v1.0")
            assert loaded is not None
            assert loaded["name"] == "my checkpoint/v1.0"  # raw name preserved in JSON
        finally:
            os.chdir(orig)


def test_checkpoint_survives_new_session() -> None:
    """Checkpoints must persist across /new (independent of session history)."""
    orig = os.getcwd()
    with tempfile.TemporaryDirectory() as tmp:
        os.chdir(tmp)
        try:
            session = sa.create_session()
            session["messages"] = [{"role": "user", "content": "important work"}]
            sa._save_checkpoint("before_new", session, tmp)

            # Simulate /new — create fresh session
            # Checkpoint dir is cwd-scoped, so it's still there.
            loaded = sa._load_checkpoint("before_new")
            assert loaded is not None, "checkpoint must survive after /new"
        finally:
            os.chdir(orig)


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
    test_redact_sk_key,
    test_redact_bearer_token,
    test_redact_password_query_string,
    test_redact_api_key_param,
    test_redact_token_param,
    test_redact_postgresql_connection_string,
    test_redact_mongodb_connection_string,
    test_redact_ssh_path_content,
    test_redact_aws_path_content,
    test_redact_payload_deep_structure,
    test_redact_leaves_benign_text_intact,
    test_redact_value_truncates_long_strings,
    test_escalation_returns_message_without_api_key,
    test_escalation_does_not_raise_without_api_key,
    test_get_api_key_reads_env_var,
    test_get_api_key_falls_back_to_config,
    test_get_api_key_env_takes_priority_over_config,
    test_escalation_redacts_before_sending,
    test_escalation_new_keys_in_default_config,
    test_checkpoint_save_creates_file,
    test_checkpoint_load_restores_messages,
    test_checkpoint_load_nonexistent_returns_none,
    test_checkpoint_list_returns_all,
    test_checkpoint_list_empty_when_no_checkpoints,
    test_checkpoint_metadata_includes_workspace,
    test_checkpoint_name_sanitized,
    test_checkpoint_survives_new_session,
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
