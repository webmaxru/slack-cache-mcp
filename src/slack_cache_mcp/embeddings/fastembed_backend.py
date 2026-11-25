"""Optional fastembed ONNX backend (local model files only)."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from slack_cache_mcp.search.embedder import DEFAULT_DIM

DEFAULT_FASTEMBED_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


class FastembedEmbedder:
    """Wraps fastembed TextEmbedding. Assumes weights already on disk."""

    def __init__(self, model: Any, *, model_id: str, dim: int) -> None:
        self._model = model
        self._model_id = model_id
        self._dim = dim

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        vectors = list(self._model.embed(texts))
        return np.asarray(vectors, dtype=np.float32)

    def embed_query(self, text: str) -> np.ndarray:
        vectors = list(self._model.query_embed(text))
        if not vectors:
            vectors = list(self._model.embed([text]))
        return np.asarray(vectors[0], dtype=np.float32)


def try_load_fastembed(
    *,
    models_dir: Path | None = None,
    model_id: str = DEFAULT_FASTEMBED_MODEL,
) -> FastembedEmbedder | None:
    """Load fastembed with offline env. Returns None if model unavailable."""
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    cache = Path(models_dir) if models_dir else None
    try:
        from fastembed import TextEmbedding
    except Exception:
        return None

    kwargs: dict[str, Any] = {
        "model_name": model_id,
        "local_files_only": True,
    }
    if cache is not None:
        cache.mkdir(parents=True, exist_ok=True)
        kwargs["cache_dir"] = str(cache)
    try:
        model = TextEmbedding(**kwargs)
    except Exception:
        return None
    # Probe dimension
    try:
        sample = list(model.embed(["ping"]))[0]
        dim = int(len(sample))
    except Exception:
        dim = DEFAULT_DIM
    return FastembedEmbedder(model, model_id=model_id, dim=dim)


def fetch_fastembed_model(
    *,
    models_dir: Path,
    model_id: str = DEFAULT_FASTEMBED_MODEL,
) -> dict[str, Any]:
    """Download model weights into ``models_dir`` (network allowed; no netguard).

    This is the only intentional network path for embeddings (HuggingFace),
    never Slack. Caller must not install netguard before this runs.
    """
    models_dir.mkdir(parents=True, exist_ok=True)
    # Clear offline flags for the fetch process.
    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ.pop("TRANSFORMERS_OFFLINE", None)
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name=model_id, cache_dir=str(models_dir))
    sample = list(model.embed(["warmup"]))[0]
    return {
        "ok": True,
        "model_id": model_id,
        "dim": int(len(sample)),
        "cache_dir": str(models_dir),
    }
