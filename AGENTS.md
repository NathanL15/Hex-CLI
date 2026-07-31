# Working on Hex CLI

Local-first terminal agent. Python stdlib + numpy/onnxruntime only — do not add
dependencies without a strong reason.

- Run tests before claiming anything works: `python evals/test_core.py` (and the
  suite that covers what you touched). CI runs all of them on windows-latest.
- Lint with `ruff check hexcli/ evals/` — it must be clean.
- The agent loop lives in `hexcli/agent.py` (protocol v1, the default) and
  `hexcli/loop_v2.py` (protocol v2, opt-in). Changes to safety, verification,
  or file tools usually need to land in BOTH.
- Live evals need the NPU server up and a FRESH restart per suite; a server
  left running for hours degrades and fakes regressions.
- Never weaken a test to make it pass. If a test is wrong, fix the test and say
  why in the commit.
