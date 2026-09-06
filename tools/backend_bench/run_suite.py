"""Run a Hex CLI eval suite against an alternate config file (e.g. Ollama on CPU
through its OpenAI-compatible /v1 endpoint), using the exact production agent
code path. Everything after the config path is passed to the suite's CLI.

  python tools/backend_bench/run_suite.py smoke tools/backend_bench/shellai_cpu_bench.json --runs 2 --seed 1
"""
import importlib
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO))

suite, cfg, *rest = sys.argv[1:]
from evals import runner  # noqa: E402,I001

runner.CONFIG_PATH = (REPO / cfg).resolve() if not Path(cfg).is_absolute() else Path(cfg)
mod = importlib.import_module(f"evals.cases_{suite}")
sys.argv = [f"cases_{suite}.py", *rest]
raise SystemExit(mod.main())
