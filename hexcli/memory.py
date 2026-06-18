#!/usr/bin/env python3
"""hexcli.memory — lightweight on-device semantic memory for Hex CLI.

Pure NumPy vector index over a local ONNX sentence-embedding model
(sentence-transformers/all-MiniLM-L6-v2, ARM64-quantized). No FAISS/
ChromaDB/LangChain. One-way dependency, mirroring hexcli.ui and
hexcli.telemetry: hexcli.agent imports this module, never the reverse.

Every public method swallows its own exceptions — an embedding/model
load failure (e.g. offline on first use) must degrade to a silent
no-op, never crash the agent loop or block a turn that doesn't touch
memory.
"""
from __future__ import annotations

import io
import json
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import numpy as np

_STORE_DIR_NAME = ".shellai/vector_store"
_MODEL_CACHE_DIR_NAME = ".shellai/models"
_HF_REPO = "sentence-transformers/all-MiniLM-L6-v2"
_HF_ONNX_FILE = "onnx/model_qint8_arm64.onnx"
_HF_TOKENIZER_FILE = "tokenizer.json"
_EMBED_DIM = 384
_MAX_ENTRIES = 500
_GLOBAL_MAX_ENTRIES = 1_000
_MIN_SIMILARITY = 0.15
_MAX_SEQ_LEN = 256
_MAX_RULES = 50

# Global store — cross-project, lives in the user's home dir.
_GLOBAL_STORE_DIR = Path.home() / ".shellai" / "global_vector_store"
_RULES_PATH = Path.home() / ".shellai" / "memory_rules.md"

# NPU inference lock — prevents the dreaming daemon from calling the LLM
# concurrently with the main agent loop. Acquired by call_llm in agent.py
# and by _consolidate here with a 5-second timeout.
_NPU_INFERENCE_LOCK: threading.Lock = threading.Lock()

# Idle timer for the dreaming daemon. touch_last_turn() resets it on each
# user input. _consolidate fires after _IDLE_TIMEOUT seconds of silence.
_last_turn_time: float = 0.0
_IDLE_TIMEOUT: float = 300.0  # 5 minutes

# Injected by start_dreaming() from agent.py — avoids a circular import.
_dream_config_fn: Callable[[], dict[str, Any]] | None = None
_dream_llm_fn: Callable[[dict[str, Any], str, str], str] | None = None

_DREAM_SYSTEM = (
    "Extract 3–5 concise factual rules from these session notes. "
    "Return only a Markdown bullet list (one rule per line, starting with '- '). "
    "No preamble, no commentary, no numbering."
)


class _Embedder:
    """Process-wide lazy singleton — the ONNX session/tokenizer load once
    and are reused across every add()/search() call for the life of the
    process, regardless of how many VectorStore instances are created."""

    _instance: "_Embedder | None" = None

    def __init__(self) -> None:
        self._session: Any = None
        self._tokenizer: Any = None
        self._unavailable = False

    @classmethod
    def instance(cls) -> "_Embedder":
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance

    def _ensure_loaded(self) -> None:
        if self._session is not None or self._unavailable:
            return
        try:
            from huggingface_hub import hf_hub_download
            from tokenizers import Tokenizer
            import onnxruntime as ort

            cache_dir = Path.cwd() / _MODEL_CACHE_DIR_NAME
            model_path = hf_hub_download(_HF_REPO, _HF_ONNX_FILE, cache_dir=str(cache_dir))
            tok_path = hf_hub_download(_HF_REPO, _HF_TOKENIZER_FILE, cache_dir=str(cache_dir))

            tokenizer = Tokenizer.from_file(tok_path)
            tokenizer.enable_padding()
            tokenizer.enable_truncation(max_length=_MAX_SEQ_LEN)

            self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
            self._tokenizer = tokenizer
        except Exception:
            self._unavailable = True

    def embed(self, texts: list[str]) -> np.ndarray | None:
        self._ensure_loaded()
        if self._unavailable or not texts:
            return None
        try:
            encodings = self._tokenizer.encode_batch(texts)
            input_ids = np.array([e.ids for e in encodings], dtype=np.int64)
            attention_mask = np.array([e.attention_mask for e in encodings], dtype=np.int64)
            token_type_ids = np.zeros_like(input_ids)

            feed = {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            }
            token_embeddings = self._session.run(None, feed)[0]

            mask = attention_mask[..., None].astype(np.float32)
            summed = (token_embeddings * mask).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            pooled = summed / counts

            norms = np.clip(np.linalg.norm(pooled, axis=1, keepdims=True), 1e-9, None)
            return (pooled / norms).astype(np.float32)
        except Exception:
            return None


class VectorStore:
    """One store per (config, cwd) — cheap to construct; only the shared
    _Embedder singleton carries real load cost, so creating a fresh
    VectorStore per tool call is fine."""

    def __init__(
        self,
        config: dict[str, Any] | None = None,
        cwd: str | None = None,
        *,
        store_dir: Path | None = None,
        max_entries: int = _MAX_ENTRIES,
    ) -> None:
        self.enabled = bool((config or {}).get("memory_enabled", True))
        if store_dir is not None:
            self._dir = store_dir
        else:
            base = Path(cwd) if cwd else Path.cwd()
            self._dir = base / _STORE_DIR_NAME
        self._vectors_path = self._dir / "vectors.npz"
        self._meta_path = self._dir / "metadata.json"
        self._vectors: np.ndarray = np.zeros((0, _EMBED_DIM), dtype=np.float32)
        self._meta: list[dict[str, Any]] = []
        self._loaded = False
        self._max_entries = max_entries

    def _load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            if self._meta_path.exists():
                self._meta = json.loads(self._meta_path.read_text(encoding="utf-8"))
            if self._vectors_path.exists():
                with np.load(self._vectors_path) as data:
                    self._vectors = data["vectors"].astype(np.float32)
        except Exception:
            self._vectors = np.zeros((0, _EMBED_DIM), dtype=np.float32)
            self._meta = []

    def add(self, text: str, metadata: dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            self._load()
            vec = _Embedder.instance().embed([text])
            if vec is None:
                return
            self._vectors = np.vstack([self._vectors, vec])
            entry = {
                "id": str(uuid.uuid4()),
                "created_at": datetime.now(timezone.utc).isoformat(),
                "text": text,
                **metadata,
            }
            self._meta.append(entry)
            if len(self._meta) > self._max_entries:
                overflow = len(self._meta) - self._max_entries
                self._meta = self._meta[overflow:]
                self._vectors = self._vectors[overflow:]
            self._save()
        except Exception:
            pass

    def search(self, query: str, top_k: int = 3) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        try:
            self._load()
            if self._vectors.shape[0] == 0:
                return []
            qvec = _Embedder.instance().embed([query])
            if qvec is None:
                return []
            sims = self._vectors @ qvec[0]
            order = np.argsort(-sims)[: max(top_k, 1)]
            results: list[dict[str, Any]] = []
            for idx in order:
                score = float(sims[idx])
                if score < _MIN_SIMILARITY:
                    continue
                entry = dict(self._meta[idx])
                entry["score"] = round(score, 3)
                results.append(entry)
            return results
        except Exception:
            return []

    def _save(self) -> None:
        self._dir.mkdir(parents=True, exist_ok=True)

        buf = io.BytesIO()
        np.savez(buf, vectors=self._vectors)
        tmp_vectors = self._vectors_path.with_suffix(".tmp")
        tmp_vectors.write_bytes(buf.getvalue())
        tmp_vectors.replace(self._vectors_path)

        tmp_meta = self._meta_path.with_suffix(".tmp")
        tmp_meta.write_text(json.dumps(self._meta, indent=2), encoding="utf-8")
        tmp_meta.replace(self._meta_path)


def maybe_index_turn(
    config: dict[str, Any],
    prompt: str,
    tools_used: list[str],
    key_paths: list[str],
    outcome: str = "completed",
) -> None:
    """Auto-index a finished agentic turn. Silent no-op on any failure,
    including memory_enabled=False, an empty tool sequence, or an
    unreachable/unavailable embedding model."""
    if not tools_used:
        return
    try:
        # File-touching turns → project store (cwd-scoped).
        # Non-file-touching turns (preferences, patterns) → global store.
        if key_paths:
            store = VectorStore(config)
        else:
            store = VectorStore(config, store_dir=_GLOBAL_STORE_DIR, max_entries=_GLOBAL_MAX_ENTRIES)
        summary = prompt.strip()
        if len(summary) > 200:
            summary = summary[:200] + "..."
        store.add(summary, {
            "tool_sequence": tools_used,
            "key_paths": sorted(set(key_paths)),
            "outcome": outcome,
        })
    except Exception:
        pass


def search_memory_tool(config: dict[str, Any], query: str, top_k: int = 3) -> str:
    if not bool(config.get("memory_enabled", True)):
        return "Memory search is disabled."

    project_store = VectorStore(config)
    global_store = VectorStore(config, store_dir=_GLOBAL_STORE_DIR, max_entries=_GLOBAL_MAX_ENTRIES)
    combined = project_store.search(query, top_k=top_k) + global_store.search(query, top_k=top_k)

    # Merge: deduplicate by content hash, rank by similarity score.
    seen: set[int] = set()
    merged: list[dict[str, Any]] = []
    for r in sorted(combined, key=lambda x: x.get("score", 0.0), reverse=True):
        h = hash(r.get("text", ""))
        if h not in seen:
            seen.add(h)
            merged.append(r)

    if not merged:
        return "No relevant memory found."
    lines = []
    for r in merged[:top_k]:
        tools = ", ".join(r.get("tool_sequence", []) or [])
        paths = ", ".join(r.get("key_paths", []) or [])
        lines.append(
            f"- [{r.get('created_at', '?')}] (score {r.get('score')}) {r.get('text', '')} "
            f"| tools used: {tools or '(none)'} | files: {paths or '(none)'}"
        )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Idle timer
# ---------------------------------------------------------------------------

def touch_last_turn() -> None:
    """Reset the idle timer. Called from the REPL on every user input."""
    global _last_turn_time
    _last_turn_time = time.monotonic()


# ---------------------------------------------------------------------------
# Memory rules (Feature 15 — rules injection)
# ---------------------------------------------------------------------------

def read_memory_rules(max_rules: int = 5) -> list[str]:
    """Return the last max_rules bullet lines from ~/.shellai/memory_rules.md."""
    try:
        if not _RULES_PATH.exists():
            return []
        lines = _RULES_PATH.read_text(encoding="utf-8").splitlines()
        rules = [ln.strip() for ln in lines if ln.strip().startswith("- ")]
        return rules[-max_rules:]
    except Exception:
        return []


def _append_rules(new_rules: list[str]) -> None:
    """Append rules to memory_rules.md, evicting oldest if count exceeds _MAX_RULES."""
    try:
        _RULES_PATH.parent.mkdir(parents=True, exist_ok=True)
        existing: list[str] = []
        if _RULES_PATH.exists():
            existing = [
                ln.strip()
                for ln in _RULES_PATH.read_text(encoding="utf-8").splitlines()
                if ln.strip().startswith("- ")
            ]
        ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M")
        stamped = [f"- [{ts}] {r.lstrip('- ').strip()}" for r in new_rules if r.strip()]
        combined = existing + stamped
        if len(combined) > _MAX_RULES:
            combined = combined[-_MAX_RULES:]
        _RULES_PATH.write_text("\n".join(combined) + "\n", encoding="utf-8")
    except Exception:
        pass


def prune_memory_rules() -> int:
    """Keep only the newest _MAX_RULES rules. Returns the number removed."""
    try:
        if not _RULES_PATH.exists():
            return 0
        lines = [
            ln.strip()
            for ln in _RULES_PATH.read_text(encoding="utf-8").splitlines()
            if ln.strip().startswith("- ")
        ]
        if len(lines) <= _MAX_RULES:
            return 0
        removed = len(lines) - _MAX_RULES
        _RULES_PATH.write_text("\n".join(lines[-_MAX_RULES:]) + "\n", encoding="utf-8")
        return removed
    except Exception:
        return 0


# ---------------------------------------------------------------------------
# Dreaming daemon (Feature 14 — async consolidation)
# ---------------------------------------------------------------------------

def _consolidate() -> None:
    """Pull recent global entries, generate rules via LLM, append to rules file."""
    global _dream_config_fn, _dream_llm_fn
    if _dream_config_fn is None or _dream_llm_fn is None:
        return
    try:
        store = VectorStore(None, store_dir=_GLOBAL_STORE_DIR, max_entries=_GLOBAL_MAX_ENTRIES)
        store._load()
        if not store._meta:
            return
        notes = "\n".join(e.get("text", "") for e in store._meta[-20:])
        if not notes.strip():
            return

        if not _NPU_INFERENCE_LOCK.acquire(timeout=5):
            return  # main loop is busy; skip this dreaming cycle
        try:
            config = _dream_config_fn()
            raw = _dream_llm_fn(config, _DREAM_SYSTEM, notes)
        finally:
            _NPU_INFERENCE_LOCK.release()

        new_rules = [ln.strip() for ln in raw.splitlines() if ln.strip().startswith("- ")]
        if new_rules:
            _append_rules(new_rules)
    except Exception:
        pass


def _dream_loop() -> None:
    """Background daemon: check every 30 s; fire consolidation after idle timeout."""
    while True:
        time.sleep(30)
        if _last_turn_time > 0 and (time.monotonic() - _last_turn_time) >= _IDLE_TIMEOUT:
            _consolidate()
            touch_last_turn()  # reset so it doesn't re-fire immediately


def start_dreaming(config_fn: Callable[[], dict[str, Any]], llm_fn: Callable) -> None:
    """Start the background consolidation daemon. Called once from run_repl."""
    global _dream_config_fn, _dream_llm_fn
    _dream_config_fn = config_fn
    _dream_llm_fn = llm_fn
    t = threading.Thread(target=_dream_loop, daemon=True)
    t.start()
