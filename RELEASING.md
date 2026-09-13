# Releasing Hex CLI

Written 2026-09-08 after ten releases in 38 days, of which the last five
were set by the npurun fork's binary and by same-day fixes (CHANGELOG,
"Releases, re-organised"). The rules below are what would have prevented
that.

## One version, one place

`hexcli/__init__.py` holds `__version__`. `pyproject.toml` reads it
(`[tool.hatch.version]`), `agent.VERSION` re-exports it, the chat log and
`--version` print it, and the README does not restate it. CI runs on `v*`
tag pushes and fails when the tag does not equal `v<__version__>`.

## Numbering

- **Patch**: fixes with no model-facing change and no change to what the
  launcher hands the server. Console, grader, docs, installer.
- **Minor**: features; any change to `hexcli/prompts.py`; any change to the
  launcher's environment or to the runtime config keys it writes
  (`prompt_stable_prefix`, `prompt_split`, `NPURUN_*`); a bump of
  `REQUIRED_NPURUN`.
- **Major**: a model, runtime-generation or history-representation change.
  Nothing else.

2.5.1 shipped a prompt change and a rewritten eval gate as a patch while
2.6.1 shipped 24 lines as a patch; under this rule 2.5.1 would have been
2.6.0.

## The fork binary

npurun lives in its own repository, [NathanL15/npurun](https://github.com/NathanL15/npurun)
(branch `hexcli-fork`), with its own tags and releases and the prebuilt
`npurun-arm64.exe` attached there. Hex CLI releases carry no asset.

- A fork change ships as a fork release first (`vX.Y.Z` on the fork, binary
  attached, fork CHANGELOG section). It needs no Hex release unless Hex has
  to change to use it.
- Hex pins the build it is written for in `launcher.py`
  (`REQUIRED_NPURUN`). Bump it in the Hex release that needs the new build;
  `--doctor`, the launcher, `install.ps1` and `hexcli --update` enforce it.
- Never bump `REQUIRED_NPURUN` past a fork release that does not exist yet.

## The gate

Always, before the tag:

1. `ruff check hexcli/ evals/` clean, every offline suite green (CI).
2. `python evals/cases_smoke.py` on a fresh server: 10/10.

When the release changes the binary, `REQUIRED_NPURUN`, the launcher's
environment, the runtime config keys, compaction, or the prompt — also:

3. `python evals/cases_extended.py --runs 3` on a fresh server, then
   `python evals/gate.py --baseline <b1> --baseline <b2> <candidate>`: PASS,
   rechecks included.
4. `python evals/cases_multiturn.py --runs 3 --think-time 15` on a fresh
   server, compared with `evals/compare.py` against the last arm: no case
   at 3/3 lost.
5. `python tools/backend_bench/stall_rate.py --user-tokens 800 --n 20
   --max-tokens 400` on AC power under the launcher's environment: at most
   one hang in twenty.

Steps 4 and 5 exist because 2.4.0 (silent empty replies, a multi-turn
effect) and 2.6.0 (hangs at 3K context) both passed the extended suite.
Restart the server before each suite; a server that has run for hours
degrades and looks like a regression.

## Steps

1. Move the CHANGELOG `Unreleased` section under `## X.Y.Z — YYYY-MM-DD`.
2. Set `__version__` in `hexcli/__init__.py`.
3. Commit `Release X.Y.Z` and push the branch. Wait for the remote CI run
   on `main` to be green before tagging: the runner is not the development
   machine (its temp folder is an 8.3 short name, `C:\Users\RUNNER~1\...`,
   which is how 2.7.0 shipped a path bug that 797 green local tests never
   saw). Then tag `vX.Y.Z` and push the tag. CI must be green on the tag.
4. `gh release create vX.Y.Z --title vX.Y.Z --notes-file <the changelog
   section>`. Publishing the release runs `.github/workflows/publish.yml`,
   which builds the wheel and the source distribution, attaches both to
   the release and publishes them to PyPI (`pip install hexcli`) through
   trusted publishing: the `hexcli` project on PyPI names this repository,
   that workflow file and the `pypi` environment as its publisher, so no
   token exists anywhere. The npurun binary is not attached; it lives on
   the fork's releases. Check the Publish run is green; `workflow_dispatch`
   with the tag re-runs it for a release that was published without it.

A release that exists to fix the previous release is a gate that was
skipped. If the fix is needed, ship it, then add the case that would have
caught it to the gate.
