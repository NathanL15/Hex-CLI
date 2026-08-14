#!/usr/bin/env python3
"""evals/test_setup_wizard.py — the /setup interactive config wizard.

The wizard's contract: Enter everywhere changes nothing; answers write ONLY
the asked-about keys merged over the existing file (never a defaults dump);
cancelling at any point writes nothing; a successful run applies to the
live session config immediately.

Usage:
    python evals/test_setup_wizard.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import hexcli.agent as sa  # noqa: E402
from hexcli import setup_wizard as wiz  # noqa: E402


def _scripted(answers: list[str]):
    """An ask() that pops scripted answers; raises if over-asked."""
    queue = list(answers)

    def ask(_prompt: str) -> str:
        if not queue:
            raise AssertionError("wizard asked more questions than scripted")
        return queue.pop(0)
    return ask


def _base_config() -> dict:
    return {**sa.DEFAULT_CONFIG}


def test_enter_everywhere_keeps_current_values_but_still_saves() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        cfg = _base_config()
        before = {k: cfg.get(k) for k, _, _, _ in wiz.QUESTIONS}
        wrote = wiz.run_wizard(cfg, path, ask=_scripted([""] * (len(wiz.QUESTIONS) + 1)))
        assert wrote is True
        after = {k: cfg.get(k) for k, _, _, _ in wiz.QUESTIONS}
        assert after == before, "Enter must keep every current value"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert set(on_disk) == {k for k, _, _, _ in wiz.QUESTIONS}, \
            "only asked-about keys may be written"


def test_answers_change_config_and_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        cfg = _base_config()
        # destructive=n, sensitive=Enter, scope=Enter, network=deny,
        # diffs=Enter, rich=Enter, save=Enter
        answers = ["n", "", "", "deny", "", "", ""]
        assert wiz.run_wizard(cfg, path, ask=_scripted(answers))
        assert cfg["autopilot_confirm_destructive"] is False
        assert cfg["network_access"] == "deny"
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["autopilot_confirm_destructive"] is False
        assert on_disk["network_access"] == "deny"


def test_existing_file_keys_survive_the_merge() -> None:
    """A user's hand-set keys (model, backend, …) must not be clobbered."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        path.write_text(json.dumps({"model": "my-model", "timeout_seconds": 42}),
                        encoding="utf-8")
        cfg = _base_config()
        assert wiz.run_wizard(cfg, path, ask=_scripted([""] * (len(wiz.QUESTIONS) + 1)))
        on_disk = json.loads(path.read_text(encoding="utf-8"))
        assert on_disk["model"] == "my-model"
        assert on_disk["timeout_seconds"] == 42


def test_declining_the_save_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        cfg = _base_config()
        answers = [""] * len(wiz.QUESTIONS) + ["n"]
        assert wiz.run_wizard(cfg, path, ask=_scripted(answers)) is False
        assert not path.exists()


def test_ctrl_c_mid_wizard_writes_nothing() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        cfg = _base_config()

        def ask(_prompt: str) -> str:
            raise KeyboardInterrupt
        assert wiz.run_wizard(cfg, path, ask=ask) is False
        assert not path.exists()


def test_invalid_choice_reprompts() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        cfg = _base_config()
        # network question gets one garbage answer, then a valid one.
        answers = ["", "", "", "banana", "allow", "", "", ""]
        assert wiz.run_wizard(cfg, path, ask=_scripted(answers))
        assert cfg["network_access"] == "allow"


def test_project_config_shadowing_is_reported() -> None:
    """A repo's .shellai/config.json deep-merges over the user config on every
    load, so it silently wins against whatever /setup just saved. The wizard
    must say so rather than promise settings that will not survive."""
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "shellai.json"
        project = Path(tmp) / "proj" / ".shellai" / "config.json"
        project.parent.mkdir(parents=True)
        project.write_text(json.dumps({"network_access": "allow"}), encoding="utf-8")
        cfg = _base_config()
        cfg["network_access"] = "ask"
        # answer 'deny' to the network question, Enter elsewhere
        answers = ["", "", "", "deny", "", "", ""]
        assert wiz.run_wizard(cfg, path, ask=_scripted(answers), project_cfg=project)
        shadowed = wiz.overridden_by_project({"network_access": "deny"}, project)
        assert shadowed == ["network_access"]


def test_no_shadow_report_when_project_agrees_or_absent() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        missing = Path(tmp) / "nope.json"
        assert wiz.overridden_by_project({"show_diffs": True}, missing) == []
        agreeing = Path(tmp) / "agree.json"
        agreeing.write_text(json.dumps({"show_diffs": True}), encoding="utf-8")
        assert wiz.overridden_by_project({"show_diffs": True}, agreeing) == []


def test_launcher_config_rewrite_preserves_user_keys() -> None:
    """launcher._write_npurun_config regenerates the very file /setup writes
    to (after any model re-pull). It must merge, not clobber, or the wizard's
    answers vanish silently."""
    import launcher

    with tempfile.TemporaryDirectory() as tmp:
        cfg_path = Path(tmp) / "shellai_npurun.json"
        cfg_path.write_text(json.dumps({
            "network_access": "deny",          # a /setup answer
            "autopilot_confirm_destructive": False,
            "model": "qwen3-4b",
        }), encoding="utf-8")
        saved = launcher.NPURUN_CONFIG
        try:
            launcher.NPURUN_CONFIG = cfg_path
            launcher._write_npurun_config()
        finally:
            launcher.NPURUN_CONFIG = saved
        after = json.loads(cfg_path.read_text(encoding="utf-8"))
        assert after["network_access"] == "deny", "wizard answer was clobbered"
        assert after["autopilot_confirm_destructive"] is False
        assert after["openai_compatible"]["base_url"].startswith("http://127.0.0.1:"), \
            "connection keys must still be refreshed"


def test_every_question_key_is_a_real_config_key() -> None:
    """The wizard must never write a key the config system doesn't know —
    an unknown key here would be invisible to /config and the example."""
    for key, _, _, _ in wiz.QUESTIONS:
        assert key in sa.DEFAULT_CONFIG, f"wizard writes unknown config key {key!r}"
        assert key in sa._CONFIG_SETTABLE, f"{key!r} not settable via /config"


def test_setup_is_a_repl_builtin() -> None:
    import inspect

    assert "/setup" in sa.REPL_COMMANDS
    assert '"/setup' in inspect.getsource(sa.run_repl)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

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


TESTS = [
    test_enter_everywhere_keeps_current_values_but_still_saves,
    test_answers_change_config_and_file,
    test_existing_file_keys_survive_the_merge,
    test_declining_the_save_writes_nothing,
    test_ctrl_c_mid_wizard_writes_nothing,
    test_invalid_choice_reprompts,
    test_project_config_shadowing_is_reported,
    test_no_shadow_report_when_project_agrees_or_absent,
    test_launcher_config_rewrite_preserves_user_keys,
    test_every_question_key_is_a_real_config_key,
    test_setup_is_a_repl_builtin,
]


def main() -> int:
    print(f"\nevals/test_setup_wizard.py — {len(TESTS)} tests\n")
    results = [_run(t) for t in TESTS]
    passed = sum(results)
    failed = len(results) - passed
    print(f"\n{passed}/{len(results)} passed", "✓" if failed == 0 else f"— {failed} FAILED")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
