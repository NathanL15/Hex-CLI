# Working on Hex CLI

Local-first terminal agent. Python stdlib + numpy/onnxruntime only — do not add
dependencies without a strong reason.

- Run tests before claiming anything works: `python evals/test_core.py` (and the
  suite that covers what you touched). CI runs all of them on windows-latest.
- Lint with `ruff check hexcli/ evals/` — it must be clean.
- The agent loop lives in `hexcli/agent.py`; the edit applier in `hexcli/editing.py`.
- Live evals need the NPU server up and a FRESH restart per suite; a server
  left running for hours degrades and fakes regressions.
- Releases follow RELEASING.md: one version source (`hexcli/__init__.py`),
  the fork build pin (`REQUIRED_NPURUN` in launcher.py), and the gate a
  runtime or prompt change must pass before it is tagged.
- Never weaken a test to make it pass. If a test is wrong, fix the test and say
  why in the commit.
