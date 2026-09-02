#!/usr/bin/env python3
"""evals/test_chatlog.py — the full-detail chat log (hexcli.chatlog).

What must hold: every record type is written with the fields the report tool
reads; secrets never reach disk; the system prompt is stored once and
referenced after; the probe records exactly the messages added between
calls; a disabled or unwritable log is a silent no-op; the agent loop
drives the probe end to end against the mock backend; and the report tool
summarises and replays what was written.

Usage:
    python evals/test_chatlog.py
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

import hexcli.agent as sa  # noqa: E402
from hexcli import chatlog  # noqa: E402


def _cfg(tmp: str, **extra: Any) -> dict[str, Any]:
    return {**sa.DEFAULT_CONFIG, "backend": "mock", "memory_enabled": False,
            "telemetry_enabled": False, "chat_log_dir": tmp,
            "anthropic_api_key": "sk-secret-123", **extra}


def _records(path: Path) -> list[dict[str, Any]]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def test_session_start_has_versions_and_redacts_secrets() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = chatlog.ChatLog(_cfg(tmp), version="9.9.9")
        assert log.path and log.path.exists(), "log file must be created at start"
        start = _records(log.path)[0]
        assert start["kind"] == "session_start"
        assert start["version"] == "9.9.9"
        assert start["model"] == sa.DEFAULT_CONFIG["model"]
        assert "python" in start and "os" in start
        text = log.path.read_text(encoding="utf-8")
        assert "sk-secret-123" not in text, "secrets must never reach disk"
        assert start["config"]["anthropic_api_key"] == "<redacted>"
        assert "chat_log_dir" in start["config"]


def test_probe_records_requests_replies_tools_and_end() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = chatlog.ChatLog(_cfg(tmp), version="t")
        probe = log.turn_start(0, "list files", [], 0)
        sys_prompt = "SYSTEM " * 50
        msgs = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": "list files"}]
        probe.on_start(sys_prompt, msgs)
        probe.on_request(0, 0, msgs)
        probe.on_llm(0, 0, '{"action":"tool","tool":"list_directory","args":{"path":"."}}', 1.5)
        probe.on_tool(0, "list_directory", {"path": "."}, "a.py\nb.py", 0.02, "ok")
        msgs = msgs + [{"role": "assistant", "content": "{...}"}, {"role": "user", "content": "Tool output:\na.py\nb.py"}]
        probe.on_request(1, 0, msgs)
        probe.on_llm(1, 0, "", 0.4)
        probe.on_end("finish", "two files")
        log.turn_end(0, status="completed", message="two files")
        kinds = [r["kind"] for r in _records(log.path)]
        assert kinds == ["session_start", "turn_start", "system_prompt", "request", "reply", "tool",
                         "request", "reply", "turn_result", "turn_end"], kinds
        recs = _records(log.path)
        first_req = recs[3]
        assert first_req["new_messages"][0] == {"role": "system", "ref": recs[2]["hash"]}, "system prompt by reference"
        assert first_req["new_messages"][1]["content"] == "list files"
        second_req = recs[6]
        assert [m["role"] for m in second_req["new_messages"]] == ["assistant", "user"], "only the delta"
        assert second_req["total_messages"] == 4
        assert recs[4]["raw"].startswith('{"action"') and recs[4]["latency_s"] == 1.5
        assert recs[7]["empty"] is True, "an empty reply is flagged"
        tool = recs[5]
        assert tool["tool"] == "list_directory" and tool["output"] == "a.py\nb.py" and tool["status"] == "ok"
        assert recs[-1]["status"] == "completed" and recs[-1]["duration_s"] is not None


def test_system_prompt_stored_once_per_session() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = chatlog.ChatLog(_cfg(tmp), version="t")
        for turn in range(3):
            probe = log.turn_start(turn, "q", [], 0)
            msgs = [{"role": "system", "content": "SAME PROMPT"}, {"role": "user", "content": "q"}]
            probe.on_start("SAME PROMPT", msgs)
            probe.on_request(0, 0, msgs)
        kinds = [r["kind"] for r in _records(log.path)]
        assert kinds.count("system_prompt") == 1, kinds


def test_disabled_and_unwritable_are_silent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        off = chatlog.ChatLog(_cfg(tmp, chat_log_enabled=False), version="t")
        assert off.path is None and not off.enabled
        off.turn_start(0, "q", [], 0).on_llm(0, 0, "x", 0.1)   # must not raise
        off.turn_end(0, status="completed")
        assert list(Path(tmp).glob("*.jsonl")) == []
        bad_dir = str(Path(tmp) / "a_file_not_a_dir")
        Path(bad_dir).write_text("x", encoding="utf-8")
        broken = chatlog.ChatLog(_cfg(tmp, chat_log_dir=bad_dir), version="t")
        assert not broken.enabled
        broken.command("/help")   # no raise


def test_agent_loop_drives_the_probe() -> None:
    """End to end through run_autopilot against the mock backend: the loop
    must call on_request before every model call and on_tool for each tool."""
    with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as ws:
        (Path(ws) / "hello.txt").write_text("hi", encoding="utf-8")
        cfg = _cfg(tmp)
        log = chatlog.ChatLog(cfg, version="t")
        probe = log.turn_start(0, "what files are here", [], 0)
        import os
        cwd = os.getcwd()
        os.chdir(ws)
        try:
            sa.run_autopilot(cfg, [], "what files are here", sa.detect_shell(""), probe=probe)
        finally:
            os.chdir(cwd)
        recs = _records(log.path)
        kinds = [r["kind"] for r in recs]
        assert "request" in kinds and "reply" in kinds and "turn_result" in kinds, kinds
        n_req = kinds.count("request")
        n_rep = kinds.count("reply")
        assert n_req == n_rep, f"one request per reply: {n_req} vs {n_rep}"
        first_req = next(r for r in recs if r["kind"] == "request")
        assert first_req["new_messages"][0]["role"] == "system"
        assert any(m.get("role") == "user" and "what files are here" in m.get("content", "")
                   for m in first_req["new_messages"])


def test_report_tool_summarises_and_replays() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        log = chatlog.ChatLog(_cfg(tmp), version="1.2.3")
        probe = log.turn_start(0, "say hi", [], 0)
        msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "say hi"}]
        probe.on_start("S", msgs)
        probe.on_request(0, 0, msgs)
        probe.on_llm(0, 0, '{"action":"finish","message":"hi"}', 2.0)
        probe.on_end("finish", "hi")
        log.turn_end(0, status="completed", message="hi")
        tool = REPO / "tools" / "chatlog_report.py"
        r = subprocess.run([sys.executable, str(tool), "--dir", tmp, "--json"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        summary = json.loads(r.stdout)
        assert summary["sessions"] == 1 and summary["turns"] == 1 and summary["llm_calls"] == 1
        assert any("1.2.3" in k for k in summary["versions"]), summary["versions"]
        assert summary["latency_s"]["first_call_median"] == 2.0
        r = subprocess.run([sys.executable, str(tool), "--dir", tmp, "--last"], capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "you> say hi" in r.stdout and "model 2.0s" in r.stdout, r.stdout


TESTS = [
    test_session_start_has_versions_and_redacts_secrets,
    test_probe_records_requests_replies_tools_and_end,
    test_system_prompt_stored_once_per_session,
    test_disabled_and_unwritable_are_silent,
    test_agent_loop_drives_the_probe,
    test_report_tool_summarises_and_replays,
]


def _run(fn) -> bool:
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


def main() -> int:
    print(f"\nevals/test_chatlog.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    print(f"\n{passed}/{len(results)} passed", "✓" if passed == len(results) else f"— {len(results) - passed} FAILED")
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
