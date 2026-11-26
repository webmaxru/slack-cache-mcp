"""Choose an Embedder: local ONNX when model files exist, else hashing."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

from slack_cache_mcp.search.embedder import DEFAULT_DIM, HashingEmbedder

if TYPE_CHECKING:
    from slack_cache_mcp.search.embedder import Embedder


def load_embedder(
    *,
    models_dir: Path | None = None,
    prefer_fastembed: bool = True,
    force_hashing: bool = False,
    model_id: str | None = None,
) -> Embedder:
    """Return an offline embedder.

    When ``model_id`` is set, load that exact model (hashing or ONNX). Otherwise
    return the best available. Never contacts the network.
    """
    if model_id and model_id.startswith("hashing"):
        return HashingEmbedder(dim=DEFAULT_DIM, model_id=model_id)

    if force_hashing or os.environ.get("SLACKCACHE_EMBEDDER", "").lower() == "hashing":
        return HashingEmbedder(dim=DEFAULT_DIM)

    if model_id or prefer_fastembed:
        try:
            from slack_cache_mcp.embeddings.fastembed_backend import try_load_fastembed

            kwargs: dict[str, object] = {"models_dir": models_dir}
            if model_id:
                kwargs["model_id"] = model_id
            emb = try_load_fastembed(**kwargs)  # type: ignore[arg-type]
            if emb is not None:
                if model_id is None or emb.model_id == model_id:
                    return emb
        except Exception:
            pass
        if model_id and not model_id.startswith("hashing"):
            # Exact ONNX model requested but unavailable — do not silently
            # substitute a different model space.
            return HashingEmbedder(dim=DEFAULT_DIM)
    return HashingEmbedder(dim=DEFAULT_DIM)
