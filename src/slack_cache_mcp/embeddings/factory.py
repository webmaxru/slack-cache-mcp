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
) -> Embedder:
    """Return the best available offline embedder.

    Never contacts the network. Fastembed is used only when model files are
    already present under ``models_dir`` (or the library's cache) and
    ``HF_HUB_OFFLINE`` / local files allow load. Otherwise HashingEmbedder.
    """
    if force_hashing or os.environ.get("SLACKCACHE_EMBEDDER", "").lower() == "hashing":
        return HashingEmbedder(dim=DEFAULT_DIM)

    if prefer_fastembed:
        try:
            from slack_cache_mcp.embeddings.fastembed_backend import try_load_fastembed

            emb = try_load_fastembed(models_dir=models_dir)
            if emb is not None:
                return emb
        except Exception:
            pass
    return HashingEmbedder(dim=DEFAULT_DIM)
