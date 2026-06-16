#!/usr/bin/env python3
"""shellai_memory — lightweight on-device semantic memory for shellai.

Pure NumPy vector index over a local ONNX sentence-embedding model
(sentence-transformers/all-MiniLM-L6-v2, ARM64-quantized). No FAISS/
ChromaDB/LangChain. One-way dependency, mirroring shellai_ui.py and
shellai_telemetry.py: shellai.py imports this module, never the reverse.

Every public method swallows its own exceptions — an embedding/model
load failure (e.g. offline on first use) must degrade to a silent
no-op, never crash the agent loop or block a turn that doesn't touch
memory.
"""
from __future__ import annotations

import io
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

_STORE_DIR_NAME = ".shellai/vector_store"
_MODEL_CACHE_DIR_NAME = ".shellai/models"
_HF_REPO = "sentence-transformers/all-MiniLM-L6-v2"
_HF_ONNX_FILE = "onnx/model_qint8_arm64.onnx"
_HF_TOKENIZER_FILE = "tokenizer.json"
_EMBED_DIM = 384
_MAX_ENTRIES = 500
_MIN_SIMILARITY = 0.15
_MAX_SEQ_LEN = 256


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

    def __init__(self, config: dict[str, Any] | None = None, cwd: str | None = None) -> None:
        self.enabled = bool((config or {}).get("memory_enabled", True))
        base = Path(cwd) if cwd else Path.cwd()
        self._dir = base / _STORE_DIR_NAME
        self._vectors_path = self._dir / "vectors.npz"
        self._meta_path = self._dir / "metadata.json"
        self._vectors: np.ndarray = np.zeros((0, _EMBED_DIM), dtype=np.float32)
        self._meta: list[dict[str, Any]] = []
        self._loaded = False

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
            if len(self._meta) > _MAX_ENTRIES:
                overflow = len(self._meta) - _MAX_ENTRIES
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
        store = VectorStore(config)
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
    store = VectorStore(config)
    results = store.search(query, top_k=top_k)
    if not results:
        return "No relevant memory found."
    lines = []
    for r in results:
        tools = ", ".join(r.get("tool_sequence", []) or [])
        paths = ", ".join(r.get("key_paths", []) or [])
        lines.append(
            f"- [{r.get('created_at', '?')}] (score {r.get('score')}) {r.get('text', '')} "
            f"| tools used: {tools or '(none)'} | files: {paths or '(none)'}"
        )
    return "\n".join(lines)
