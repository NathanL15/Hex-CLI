"""hexcli — Hex CLI Python package."""

# The one place the version is written. pyproject.toml reads it (hatch
# dynamic version), agent.VERSION re-exports it, and CI refuses a release
# tag that does not match it.
__version__ = "2.7.1"
