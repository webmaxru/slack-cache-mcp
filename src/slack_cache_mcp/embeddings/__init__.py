"""Embedding backends that may pull optional ONNX deps.

This package is intentionally outside the offline import-linter contract
sources (like ``sync/``): only CLI/daemon construct Fastembed backends.
``search/`` and ``mcp_server/`` depend solely on the Embedder protocol /
HashingEmbedder.
"""

from slack_cache_mcp.embeddings.factory import load_embedder

__all__ = ["load_embedder"]
