"""launcher.py — checkout entry point. `Hex CLI.cmd` runs this; the
installed package provides the same thing as the `hex` command.

`import launcher` from a script in the checkout gives hexcli.launcher
itself, so patches and attribute reads land on the one real module."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import hexcli.launcher as _launcher  # noqa: E402

if __name__ != "__main__":
    sys.modules[__name__] = _launcher

if __name__ == "__main__":
    raise SystemExit(_launcher.main())
