"""Synthetic CPU work for the CPU-vs-NPU study.

  python load.py workers N SECONDS   -- N busy processes (sha256 loop) for SECONDS
  python load.py job                 -- one fixed foreground job, prints JSON timings

The foreground job is deterministic: a pure-Python integer loop (single core),
sha256 over 256 MB (single core, memory-streaming), and the same Python loop
split over 12 processes (all cores). Wall time alone vs. wall time while an
inference backend is running is the "how does it feel to use the laptop" number.
"""
import hashlib
import json
import multiprocessing as mp
import os
import sys
import time


def _spin(seconds: float) -> None:
    h = hashlib.sha256()
    blk = os.urandom(1 << 16)
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        h.update(blk)


def _pyloop(n: int) -> int:
    acc = 0
    for i in range(n):
        acc = (acc * 31 + i) & 0xFFFFFFFF
    return acc


def job() -> dict:
    out = {}
    t = time.perf_counter()
    _pyloop(20_000_000)
    out["pyloop_1core_s"] = round(time.perf_counter() - t, 3)
    buf = os.urandom(1 << 20) * 256
    t = time.perf_counter()
    hashlib.sha256(buf).hexdigest()
    out["sha256_256mb_s"] = round(time.perf_counter() - t, 3)
    del buf
    t = time.perf_counter()
    with mp.Pool(12) as pool:
        pool.map(_pyloop, [5_000_000] * 12)
    out["pyloop_12core_s"] = round(time.perf_counter() - t, 3)
    return out


if __name__ == "__main__":
    mode = sys.argv[1]
    if mode == "workers":
        n, secs = int(sys.argv[2]), float(sys.argv[3])
        procs = [mp.Process(target=_spin, args=(secs,)) for _ in range(n)]
        for p in procs:
            p.start()
        for p in procs:
            p.join()
    elif mode == "spin":
        # one busy process; the harness spawns N of these and kills them by pid
        # (multiprocessing children outlive a killed parent on Windows)
        _spin(float(sys.argv[2]))
    elif mode == "job":
        print(json.dumps(job()))
