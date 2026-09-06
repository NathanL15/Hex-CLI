"""Hold a Windows power availability request (PowerCreateRequest/PowerSetRequest,
ExecutionRequired + SystemRequired) for the duration of an npu_ab hexconv run,
with NO CPU spinner. If the battery hangs disappear with this alone, npurun can
hold the same request only while a query is in flight (cheaper than spinning).

  python power_request_test.py --reps 6 [--variant poll0]
"""
from __future__ import annotations

import argparse
import ctypes
import ctypes.wintypes as wt
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent.parent

POWER_REQUEST_CONTEXT_VERSION = 0
POWER_REQUEST_CONTEXT_SIMPLE_STRING = 0x1
PowerRequestSystemRequired = 1
PowerRequestExecutionRequired = 3


class REASON_CONTEXT(ctypes.Structure):
    _fields_ = [("Version", wt.ULONG), ("Flags", wt.DWORD), ("SimpleReasonString", wt.LPWSTR)]


def hold_power_request() -> object:
    k32 = ctypes.windll.kernel32
    ctx = REASON_CONTEXT(POWER_REQUEST_CONTEXT_VERSION, POWER_REQUEST_CONTEXT_SIMPLE_STRING, "npurun inference in flight")
    k32.PowerCreateRequest.restype = wt.HANDLE
    h = k32.PowerCreateRequest(ctypes.byref(ctx))
    if not h:
        raise OSError(ctypes.get_last_error())
    for t in (PowerRequestSystemRequired, PowerRequestExecutionRequired):
        if not k32.PowerSetRequest(h, t):
            raise OSError(f"PowerSetRequest({t}) failed: {ctypes.get_last_error()}")
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--reps", type=int, default=6)
    ap.add_argument("--variant", default="poll0")
    ap.add_argument("--out", default=str(REPO / "docs/backend_study/data_npu_ab/battery_powerreq"))
    args = ap.parse_args()
    h = hold_power_request()
    print("power request held:", bool(h))
    p = subprocess.run([sys.executable, str(HERE / "npu_ab.py"), "--variant", args.variant, "--phases", "hexconv", "--repeats", str(args.reps), "--out", args.out],
                       capture_output=True, text=True, timeout=1800, cwd=str(REPO))
    lines = [ln for ln in p.stdout.splitlines() if "[hex_turn" in ln]
    tt = [float(re.search(r"ttft=\s*([\d.]+)", ln).group(1)) for ln in lines]
    print(f"requests {len(lines)}, max ttft {max(tt):.1f} s, >60 s: {sum(t > 60 for t in tt)}")
    for ln in lines:
        if float(re.search(r"ttft=\s*([\d.]+)", ln).group(1)) > 60:
            print(ln)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
