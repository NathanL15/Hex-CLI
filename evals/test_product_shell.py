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
from hexcli import diffview, doctor  # noqa: E402

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


TESTS = [
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
