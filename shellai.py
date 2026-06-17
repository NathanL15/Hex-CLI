"""shellai.py — entry-point shim. Delegates to hexcli.agent.

Kept at the project root so launcher.py and existing scripts that call
  python shellai.py ...
continue to work unchanged.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from hexcli.agent import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
