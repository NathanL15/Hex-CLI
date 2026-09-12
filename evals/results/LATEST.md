# Current eval number

Source: `persona_guard2_r5_20260912.json` — suite `extended_v2`, 2026-09-12 16:50, 5 runs/case.
Gate set from: persona_guard2_r5_20260912.json.

| | |
|---|---|
| pass^k (3/3) | **32/44** |
| run-level | 181/223 (81.2%) |
| gate set | 32 cases — a candidate must keep every one (recheck rule: 6 runs, one miss allowed) |
| ceiling panel | 12 cases — tracked, not gated |
| build | git eb41fc4+, npurun 0.2.3 |
| server | budget 3696, window 4096 |
| order | seed 20260916 |
| canary | 2.385s → 2.375s |
| model calls | 444 |

## Ceiling panel

- `ambiguous-2` 1/5
- `ambiguous-3` 2/5
- `bigfile-2` 0/5
- `error-recovery-1` 0/5
- `factual-5` 0/5
- `livestate-1` 3/5
- `numeric-1` 1/5
- `regression-anchor-1` 5/6
- `tests-claim-1` 3/5
- `trap-1` 0/5
- `trap-3` 0/5
- `trap-4` 4/5

## How to gate a change

    python evals/gate.py --baseline evals/results/persona_guard2_r5_20260912.json  evals/results/<candidate>.json

Run the candidate on a fresh server with a recorded seed; a p-value above 0.05 at 3 runs per case is absence of evidence, not parity (evals/stats.py).
