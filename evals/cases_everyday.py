#!/usr/bin/env python3
"""evals/cases_everyday.py — the common-prompt sweep.

Every prior suite measured either things we already fixed (regression), or
adversarial inputs (traps, injections), or multi-step agentic work. Nothing
measured the space a normal user hits in their first five minutes: "what cpu
do i have", "convert my salary to hourly", "how many days until christmas".
Two live failures (a confabulated Intel CPU on this Snapdragon machine, and
five different wrong answers to one salary division) both came from exactly
this unmeasured region — these are not edge cases, they are the front door.

Four categories, each with a distinct root-cause hypothesis:

  livestate  — questions about THIS machine. Rule 9 says these must run a
               command; the failure mode is answering from prior instead
               (confabulation) or running the wrong query.
  numeric    — everyday arithmetic. Rule 4 routes ALL math to direct answers;
               the hypothesis is that single-step is fine and multi-step is
               beyond the 4B, which is what run_code exists for.
  datemath   — the system prompt includes today's date, so these are fair
               direct-answer questions; the load is calendar arithmetic.
  knowledge  — control group. Must stay 0-tool direct answers, so any prompt
               fix for the above cannot regress into over-tooling.

Grading is against COMPUTED machine truth (RAM, cores, hostname, free disk,
wall-clock time) or exact arithmetic — never against what the model usually
says. Numeric answers tolerate commas, currency symbols, and rounding.

Live-state content checks are machine-specific by nature (this is a
Snapdragon X Elite with an Adreno GPU); like every live suite here, the
numbers only mean something on the machine that ran them.

Usage:
    python evals/cases_everyday.py                 # 3 runs/case
    python evals/cases_everyday.py --runs 1        # fast screening pass
    python evals/cases_everyday.py --case live-cpu
"""
from __future__ import annotations

import ctypes
import os
import re
import shutil
import socket
import sys
from datetime import date, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evals import checks as ck  # noqa: E402
from evals.runner import Case, Trace, run_suite_cli  # noqa: E402

# ---------------------------------------------------------------------------
# Machine truth, computed once at load
# ---------------------------------------------------------------------------


def _ram_gb() -> float:
    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [
            ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
            ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
            ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
            ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
            ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
        ]
    stat = MEMORYSTATUSEX()
    stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
    return stat.ullTotalPhys / 2**30


RAM_GB = _ram_gb()                      # ~15.6 on this 16 GB machine
CORES = os.cpu_count() or 0             # 12
HOSTNAME = socket.gethostname()
USERNAME = os.environ.get("USERNAME", "")
PY_VERSION = f"{sys.version_info.major}.{sys.version_info.minor}"
TODAY = date.today()

# ---------------------------------------------------------------------------
# Graders
# ---------------------------------------------------------------------------

_NUM_RE = re.compile(r"-?\d+(?:\.\d+)?")


def _numbers_in(text: str) -> list[float]:
    cleaned = text.replace(",", "").replace("$", "")
    out = []
    for m in _NUM_RE.finditer(cleaned):
        try:
            out.append(float(m.group()))
        except ValueError:
            pass
    return out


def answer_number_close(expected: float, rel_tol: float = 0.01, abs_tol: float = 0.0):
    """Pass if ANY number in the final message is within tolerance of expected.
    Tolerant of commas, $, and reasonable rounding — strict about the value."""
    def _verify(_s: Path, trace: Trace) -> tuple[bool, str]:
        nums = _numbers_in(trace.final_message)
        tol = max(abs(expected) * rel_tol, abs_tol)
        for n in nums:
            if abs(n - expected) <= tol:
                return True, f"answered ~{expected}"
        return False, (f"expected ~{expected} (±{tol:.3g}); numbers in answer: "
                       f"{nums[:8]} — {trace.final_message[:160]!r}")
    return _verify


def answer_time_is_now(window_min: int = 3):
    """The message must contain the current wall-clock time (graded at verify
    time, so run latency doesn't fail it) within a few minutes, 12h or 24h."""
    time_re = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?\s*([ap]\.?m\.?)?", re.IGNORECASE)

    def _verify(_s: Path, trace: Trace) -> tuple[bool, str]:
        now = datetime.now()
        now_min = now.hour * 60 + now.minute
        for m in time_re.finditer(trace.final_message):
            hh, mm = int(m.group(1)), int(m.group(2))
            ampm = (m.group(3) or "").lower().replace(".", "")
            if not 0 <= mm < 60:
                continue
            candidates = []
            if ampm == "pm" and hh != 12:
                candidates.append((hh + 12) * 60 + mm)
            elif ampm == "am" and hh == 12:
                candidates.append(mm)
            else:
                candidates.append(hh * 60 + mm)
                if not ampm and 1 <= hh <= 11:      # bare 12h clock
                    candidates.append((hh + 12) * 60 + mm)
            for cand in candidates:
                diff = abs(cand - now_min)
                if min(diff, 1440 - diff) <= window_min:
                    return True, "current time given"
        return False, f"no current-time (now {now:%H:%M}) in: {trace.final_message[:160]!r}"
    return _verify


def free_disk_close(drive: str = "C:\\", rel_tol: float = 0.05):
    """Free-space truth moves, so compute it at verify time. A correct value
    counts whether it is stated in bytes, GB, or TB — grading is about the
    fact, not the unit (a bytes answer is ugly but right)."""
    def _verify(_s: Path, trace: Trace) -> tuple[bool, str]:
        free_gb = shutil.disk_usage(drive).free / 2**30
        nums = _numbers_in(trace.final_message)
        for n in nums:
            for candidate_gb in (n, n * 1024, n / 2**30):      # GB, TB, bytes
                if abs(candidate_gb - free_gb) <= free_gb * rel_tol:
                    return True, "free space correct"
        return False, (f"free space is ~{free_gb:.0f} GB; numbers in answer: "
                       f"{nums[:8]} — {trace.final_message[:160]!r}")
    return _verify


answer_matches = ck.answer_matches  # shared since 2026-09-01 (was a local copy)


def ran_a_command(*also_ok: str):
    """The live-state contract: the answer must come from the machine — via
    run_command or any equally legitimate tool named in also_ok."""
    ok_tools = ("run_command",) + also_ok

    def _verify(_s: Path, trace: Trace) -> tuple[bool, str]:
        if any(t in trace.tools_used for t in ok_tools):
            return True, ""
        return False, f"never consulted the machine; tools_used={trace.tools_used}"
    return _verify


# ---------------------------------------------------------------------------
# Cases
# ---------------------------------------------------------------------------

_days_to_christmas = (date(TODAY.year, 12, 25) - TODAY).days
_weeks_left = (date(TODAY.year, 12, 31) - TODAY).days / 7
_age = (TODAY - date(2005, 3, 12)).days // 365

EVERYDAY_CASES = [
    # ---- livestate: must hit the machine, and be right ----
    Case("live-cpu", "livestate", "what cpu do i have",
         verify=ck.all_of(
             ran_a_command(),
             answer_matches([r"snapdragon|oryon|qualcomm|x1e|arm"],
                            [r"intel|ryzen|core i[3579]"]),
         )),
    Case("live-gpu", "livestate", "what gpu does this machine have",
         verify=ck.all_of(
             ran_a_command(),
             answer_matches([r"adreno|qualcomm"], [r"nvidia|geforce|radeon"]),
         )),
    Case("live-ram", "livestate", "how much ram do i have",
         verify=ck.all_of(ran_a_command(), answer_number_close(RAM_GB, rel_tol=0.08))),
    Case("live-cores", "livestate", "how many cpu cores does this machine have",
         verify=ck.all_of(ran_a_command(), answer_number_close(CORES, abs_tol=0.1))),
    Case("live-disk", "livestate", "how much free space is on my c drive",
         verify=ck.all_of(ran_a_command(), free_disk_close())),
    Case("live-os", "livestate", "what version of windows am i running",
         verify=ck.all_of(ran_a_command(),
                          answer_matches([r"windows\s*11|\b11\b|26[12]00"], []))),
    Case("live-hostname", "livestate", "what is my computer's name",
         verify=ck.all_of(ran_a_command(), ck.message_contains(HOSTNAME))),
    Case("live-user", "livestate", "what's my username on this pc",
         verify=ck.all_of(ran_a_command(), ck.message_contains(USERNAME))),
    Case("live-python", "livestate", "what version of python is installed here",
         verify=ck.all_of(ran_a_command(), ck.message_contains(PY_VERSION))),
    Case("live-time", "livestate", "what time is it right now",
         verify=ck.all_of(ran_a_command(), answer_time_is_now())),
    Case("live-count", "livestate", "how many files are in this directory",
         setup={"a.txt": "x", "b.txt": "y", "c.py": "z", "d.md": "w"},
         verify=ck.all_of(ran_a_command("list_directory", "find_files"),
                          ck.message_has_int(4))),
    Case("live-biggest", "livestate", "which file in this folder is the biggest",
         setup={"small.txt": "x" * 10, "medium.log": "y" * 4000, "big.dat": "z" * 90000},
         verify=ck.message_contains("big.dat")),

    # ---- numeric: graded on the answer alone; any route is fine ----
    Case("num-salary", "numeric",
         "what is 104k annual salary in hourly working 37.5 hr/week",
         verify=answer_number_close(104_000 / (52 * 37.5))),           # 53.33
    Case("num-tip", "numeric", "what's a 15% tip on $84.50",
         verify=answer_number_close(84.50 * 0.15)),                    # 12.675
    Case("num-percent", "numeric", "what is 18% of 2450",
         verify=answer_number_close(441.0)),
    Case("num-miles", "numeric", "convert 5 miles to kilometers",
         verify=answer_number_close(8.0467)),
    Case("num-save", "numeric",
         "if i save $350 a month, how much will i have saved after 3 years",
         verify=answer_number_close(350 * 36)),                        # 12,600
    Case("num-mpg", "numeric", "i drove 340 miles on 12 gallons of gas, what was my mpg",
         verify=answer_number_close(340 / 12)),                        # 28.33
    Case("num-split", "numeric", "split a $187 dinner bill evenly between 7 people",
         verify=answer_number_close(187 / 7)),                         # 26.71
    Case("num-seconds", "numeric", "how many seconds are in a week",
         verify=answer_number_close(604_800, rel_tol=0.0)),
    Case("num-fraction", "numeric", "what is 7/8 as a percentage",
         verify=answer_number_close(87.5, rel_tol=0.0)),
    Case("num-discount", "numeric",
         "a $60 item is 30% off and then i pay 13% tax on it — what's the final price",
         verify=answer_number_close(60 * 0.70 * 1.13)),                # 47.46

    # ---- datemath: the prompt carries today's date; the work is calendar ----
    Case("date-christmas", "datemath", "how many days until christmas",
         verify=answer_number_close(_days_to_christmas, abs_tol=1.1)),
    Case("date-weekday", "datemath", "what day of the week is january 1 2027",
         verify=ck.message_contains("friday")),
    Case("date-weeks-left", "datemath", "how many weeks are left in 2026",
         verify=answer_number_close(_weeks_left, abs_tol=1.1)),
    Case("date-age", "datemath", "if i was born on march 12 2005, how old am i",
         verify=answer_number_close(_age, abs_tol=0.1)),

    # ---- knowledge controls: any fix above must not cause over-tooling ----
    Case("know-cmdlet", "knowledge", "what cmdlet lists running processes",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains("get-process")),
         max_steps=2),
    Case("know-capital", "knowledge", "what is the capital of japan",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_contains("tokyo")),
         max_steps=2),
    Case("know-flag", "knowledge", "what does the -Recurse flag do in powershell",
         verify=ck.all_of(ck.no_tool_calls(),
                          ck.message_contains_any("subdirector", "recursiv", "child", "nested")),
         max_steps=2),
    Case("know-simple-math", "knowledge", "what is 20% of 50",
         verify=ck.all_of(ck.no_tool_calls(), ck.message_has_int(10)),
         max_steps=2),
]


def main() -> int:
    return run_suite_cli("everyday_v1", cases=EVERYDAY_CASES, default_runs=3)


if __name__ == "__main__":
    raise SystemExit(main())
