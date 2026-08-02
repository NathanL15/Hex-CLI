#!/usr/bin/env python3
"""evals/test_product_shell.py — v2.0 product-shell features.

Covers the pieces that make Hex CLI usable day to day rather than just
correct: diff preview, project instructions (AGENTS.md), and the installation
doctor. These came out of the product review; each targets a specific
measured or reported failure.

Usage:
    python evals/test_product_shell.py
"""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from hexcli import diffview, doctor, ui  # noqa: E402

# ---------------------------------------------------------------------------
# Diff preview
# ---------------------------------------------------------------------------

def test_diff_shows_added_and_removed() -> None:
    out = diffview.render_diff("a = 1\nb = 2\n", "a = 1\nb = 3\n", "x.py", color=False)
    assert "-b = 2" in out and "+b = 3" in out, out
    assert "(+1 −1)" in out, out


def test_diff_for_created_file() -> None:
    out = diffview.render_diff(None, "one\ntwo\n", "new.txt", color=False)
    assert "created new.txt" in out and "+one" in out


def test_diff_no_change_is_quiet() -> None:
    out = diffview.render_diff("same\n", "same\n", "x.txt", color=False)
    assert "no change" in out
    assert "+" not in out and "-" not in out


def test_diff_elides_huge_changes() -> None:
    before = "\n".join(f"line{i}" for i in range(300))
    after = "\n".join(f"CHANGED{i}" for i in range(300))
    out = diffview.render_diff(before, after, "big.txt", color=False, max_lines=20)
    assert "more diff lines" in out
    assert len(out.splitlines()) < 40, "elision must actually bound the output"


def test_diff_clips_absurdly_long_lines() -> None:
    out = diffview.render_diff("x\n", "y" * 5000 + "\n", "min.js", color=False)
    assert all(len(ln) < 300 for ln in out.splitlines()), "long lines must be clipped"


def test_turn_diffs_cover_every_touched_path() -> None:
    snaps = {"a.txt": "old", "b.txt": None}
    now = {"a.txt": "new", "b.txt": "created"}
    out = diffview.render_turn_diffs(snaps, lambda p: now.get(p), color=False)
    assert "a.txt" in out and "b.txt" in out


def test_turn_diffs_report_deletions() -> None:
    out = diffview.render_turn_diffs({"gone.txt": "had content"},
                                     lambda p: None, color=False)
    assert "deleted gone.txt" in out


# ---------------------------------------------------------------------------
# Project instructions (AGENTS.md)
# ---------------------------------------------------------------------------

def test_agents_md_is_read() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "AGENTS.md").write_text(
            "# Rules\nAlways run tests first.\n", encoding="utf-8")
        text = sa.read_project_instructions(Path(tmp))
        assert "Always run tests first." in text


def test_project_instructions_precedence() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        (root / ".shellai").mkdir()
        (root / "AGENTS.md").write_text("from AGENTS", encoding="utf-8")
        (root / ".shellai" / "AGENTS.md").write_text("from dot-shellai", encoding="utf-8")
        assert "from AGENTS" in sa.read_project_instructions(root)


def test_project_instructions_are_capped_loudly() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        long_text = "\n".join(f"rule number {i} " + "x" * 60 for i in range(200))
        (Path(tmp) / "AGENTS.md").write_text(long_text, encoding="utf-8")
        text = sa.read_project_instructions(Path(tmp), max_chars=500)
        assert len(text) < 900, "cap must bound the prompt cost"
        assert "truncated" in text, "truncation must be visible, not silent"


def test_missing_instructions_returns_empty() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert sa.read_project_instructions(Path(tmp)) == ""


def test_workspace_snapshot_includes_instructions() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "AGENTS.md").write_text("Use tabs, not spaces.", encoding="utf-8")
        prev = Path.cwd()
        os.chdir(tmp)
        try:
            snap = sa.workspace_snapshot(tmp)
        finally:
            os.chdir(prev)
        assert "Use tabs, not spaces." in snap
        assert snap.startswith("["), "the workspace tag line must still come first"


# ---------------------------------------------------------------------------
# Doctor
# ---------------------------------------------------------------------------

def test_doctor_detects_missing_embedding_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        checks = doctor.check_embedding_model(Path(tmp))
        assert any(c.status == doctor.WARN for c in checks)
        assert any("huggingface" in c.fix for c in checks), "must give the download command"


def test_doctor_detects_present_embedding_model() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        onnx = Path(tmp) / "onnx"
        onnx.mkdir()
        (onnx / "model_qint8_arm64.onnx").write_bytes(b"x" * 2_000_000)
        (onnx / "tokenizer.json").write_text("{}", encoding="utf-8")
        checks = doctor.check_embedding_model(Path(tmp))
        assert all(c.status == doctor.PASS for c in checks), [c.detail for c in checks]


def test_doctor_python_check_passes_here() -> None:
    assert doctor.check_python().status == doctor.PASS


def test_doctor_reports_exit_code_on_failure() -> None:
    import contextlib
    import io
    buf = io.StringIO()
    with tempfile.TemporaryDirectory() as tmp, contextlib.redirect_stdout(buf):
        # An empty app dir: no model, and QAIRT likely absent in CI.
        rc = doctor.run_doctor({"openai_compatible": {"base_url": ""}}, Path(tmp))
    text = buf.getvalue()
    assert "installation check" in text.lower()
    assert rc in (0, 1)


def test_doctor_every_failing_check_offers_a_fix() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        groups = [doctor.check_embedding_model(Path(tmp)), doctor.check_qairt(),
                  doctor.check_npurun(), [doctor.check_ruff()]]
        for group in groups:
            for c in group:
                if c.status != doctor.PASS:
                    assert c.fix, f"{c.name} fails with no remedy — that's a useless check"


# ---------------------------------------------------------------------------
# Example config
# ---------------------------------------------------------------------------

def _example_config_generator() -> Any:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
    import gen_example_config
    return gen_example_config


def test_example_config_matches_defaults() -> None:
    """The example config is copy-paste documentation, so drift is a real bug:
    it once shipped a prompt override that replaced the tuned agent prompt."""
    gen = _example_config_generator()
    current = gen.TARGET.read_text(encoding="utf-8")
    assert current == gen.render(), \
        "shellai.example.json is stale — run tools/gen_example_config.py"


def test_example_config_excludes_prompt_overrides() -> None:
    import json
    gen = _example_config_generator()
    data = json.loads(gen.TARGET.read_text(encoding="utf-8"))
    offenders = [k for k in data if gen.is_prompt_override(k)]
    assert not offenders, f"prompt overrides must never be suggested: {offenders}"


def test_example_config_has_no_unknown_keys() -> None:
    import json
    gen = _example_config_generator()
    data = json.loads(gen.TARGET.read_text(encoding="utf-8"))
    unknown = set(data) - set(sa.DEFAULT_CONFIG) - {"_comment"}
    assert not unknown, f"example config documents keys that do not exist: {unknown}"


# ---------------------------------------------------------------------------
# Consent prompts must never stall an unattended run
#
# A detached/hidden console reports isatty() True while no human can ever type
# into it. That shape hung an eval for 7.5 hours on one destructive-command
# prompt. Note the fix cannot use a worker thread: the Windows console read holds
# the GIL, so Thread.join(timeout) does not fire (measured 3s join -> 60s).
# ---------------------------------------------------------------------------

class _FakeStdin:
    def __init__(self, tty: bool) -> None:
        self._tty = tty

    def isatty(self) -> bool:
        return self._tty


def _with_console(monkey: dict[str, Any], fn: Any) -> Any:
    """Run fn with sys.stdin / msvcrt.kbhit / msvcrt.getwch swapped out."""
    import msvcrt

    saved = (sys.stdin, msvcrt.kbhit, msvcrt.getwch)
    try:
        sys.stdin = monkey["stdin"]
        msvcrt.kbhit = monkey["kbhit"]
        msvcrt.getwch = monkey["getwch"]
        return fn()
    finally:
        sys.stdin, msvcrt.kbhit, msvcrt.getwch = saved


def test_confirm_denies_when_stdin_not_a_tty() -> None:
    out = _with_console(
        {"stdin": _FakeStdin(False), "kbhit": lambda: False, "getwch": lambda: ""},
        lambda: ui.confirm_or_deny("Allow? ", timeout_s=5.0),
    )
    assert out is False, "piped/redirected stdin must deny immediately"


def test_confirm_times_out_and_denies_on_dead_console() -> None:
    import time as _t

    started = _t.monotonic()
    out = _with_console(
        {"stdin": _FakeStdin(True), "kbhit": lambda: False, "getwch": lambda: ""},
        lambda: ui.confirm_or_deny("Allow? ", timeout_s=0.5),
    )
    elapsed = _t.monotonic() - started
    assert out is False, "an unanswered prompt must fail closed"
    assert elapsed < 5.0, f"must give up near the timeout, took {elapsed:.1f}s"


def test_confirm_accepts_an_explicit_yes() -> None:
    keys = iter(["y", "\r"])
    out = _with_console(
        {"stdin": _FakeStdin(True), "kbhit": lambda: True, "getwch": lambda: next(keys)},
        lambda: ui.confirm_or_deny("Allow? ", timeout_s=5.0),
    )
    assert out is True, "a typed 'y' must still consent"


def test_confirm_treats_bare_enter_as_no() -> None:
    keys = iter(["\r"])
    out = _with_console(
        {"stdin": _FakeStdin(True), "kbhit": lambda: True, "getwch": lambda: next(keys)},
        lambda: ui.confirm_or_deny("Allow? ", timeout_s=5.0),
    )
    assert out is False, "[y/N] default must be no"


def test_destructive_confirm_routes_through_the_guard() -> None:
    """Non-vacuity: the real entry point must inherit the timeout, not re-implement."""
    out = _with_console(
        {"stdin": _FakeStdin(False), "kbhit": lambda: False, "getwch": lambda: ""},
        lambda: ui.confirm_destructive_command("Remove-Item -Recurse C:\\"),
    )
    assert out is False


# ---------------------------------------------------------------------------
# Clarification grading accepts imperative requests, not just questions
# ---------------------------------------------------------------------------

def test_clarification_accepts_imperative_requests() -> None:
    from types import SimpleNamespace

    from evals import checks

    verify = checks.asks_clarification()
    for msg in (
        "Please describe the code you want fixed.",
        "Please clarify the specifics of what needs fixing.",
        "I would need details such as the file and the specific error.",
    ):
        ok, why = verify(None, SimpleNamespace(final_message=msg, tool_calls=0, tools_used=[]))
        assert ok, f"should count as asking: {msg!r} ({why})"


def test_clarification_still_rejects_a_non_answer() -> None:
    from types import SimpleNamespace

    from evals import checks

    verify = checks.asks_clarification()
    msg = "The request was ambiguous and no file was provided. No action could be taken."
    ok, _ = verify(None, SimpleNamespace(final_message=msg, tool_calls=0, tools_used=[]))
    assert not ok, "a statement that asks for nothing must not pass"


TESTS = [
    test_confirm_denies_when_stdin_not_a_tty,
    test_confirm_times_out_and_denies_on_dead_console,
    test_confirm_accepts_an_explicit_yes,
    test_confirm_treats_bare_enter_as_no,
    test_destructive_confirm_routes_through_the_guard,
    test_clarification_accepts_imperative_requests,
    test_clarification_still_rejects_a_non_answer,
    test_example_config_matches_defaults,
    test_example_config_excludes_prompt_overrides,
    test_example_config_has_no_unknown_keys,
    test_diff_shows_added_and_removed,
    test_diff_for_created_file,
    test_diff_no_change_is_quiet,
    test_diff_elides_huge_changes,
    test_diff_clips_absurdly_long_lines,
    test_turn_diffs_cover_every_touched_path,
    test_turn_diffs_report_deletions,
    test_agents_md_is_read,
    test_project_instructions_precedence,
    test_project_instructions_are_capped_loudly,
    test_missing_instructions_returns_empty,
    test_workspace_snapshot_includes_instructions,
    test_doctor_detects_missing_embedding_model,
    test_doctor_detects_present_embedding_model,
    test_doctor_python_check_passes_here,
    test_doctor_reports_exit_code_on_failure,
    test_doctor_every_failing_check_offers_a_fix,
]


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


def main() -> int:
    print(f"\nevals/test_product_shell.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
