"""Embedder protocol and offline HashingEmbedder (no network, no model files)."""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol, runtime_checkable

import numpy as np

DEFAULT_DIM = 384
DEFAULT_MODEL_ID = "hashing-unicode-trigram-v2"

_WORD_RE = re.compile(r"[^\W\d_]+|\d+", re.UNICODE)


@runtime_checkable
class Embedder(Protocol):
    @property
    def model_id(self) -> str: ...

    @property
    def dim(self) -> int: ...

    def embed_documents(self, texts: list[str]) -> np.ndarray: ...

    def embed_query(self, text: str) -> np.ndarray: ...


def _l2_normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm <= 1e-12:
        return vec
    return vec / norm


class HashingEmbedder:
    """Deterministic bag-of-character-trigrams projected into ``dim`` dims.

    Fully offline. Unicode-aware word/trigram features (model id v2).
    Optional ``synonym_groups`` collapse alternate surface forms onto a shared
    token so the eval harness can measure hybrid > lexical on paraphrases.
    """

    def __init__(
        self,
        *,
        dim: int = DEFAULT_DIM,
        model_id: str = DEFAULT_MODEL_ID,
        synonym_groups: list[set[str]] | None = None,
    ) -> None:
        self._dim = dim
        self._model_id = model_id
        self._canon: dict[str, str] = {}
        if synonym_groups:
            for group in synonym_groups:
                forms = {g.lower().strip() for g in group if g.strip()}
                if not forms:
                    continue
                rep = sorted(forms, key=lambda s: (len(s), s))[0]
                for f in forms:
                    self._canon[f] = rep

    @property
    def model_id(self) -> str:
        return self._model_id

    @property
    def dim(self) -> int:
        return self._dim

    def _normalize_text(self, text: str) -> str:
        lowered = text.casefold()
        if not self._canon:
            return lowered
        keys = sorted(self._canon.keys(), key=len, reverse=True)
        out = lowered
        for key in keys:
            if key in out:
                out = out.replace(key, self._canon[key])
        return out

    def _embed_one(self, text: str) -> np.ndarray:
        text = self._normalize_text(text)
        vec = np.zeros(self._dim, dtype=np.float32)
        for tok in _WORD_RE.findall(text):
            self._add_feature(vec, f"w:{tok}", 1.0)
        # Character trigrams over letters/digits (any script), spaces as separators.
        compact = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
        padded = f" {compact} "
        for i in range(max(0, len(padded) - 2)):
            tri = padded[i : i + 3]
            if tri.strip():
                self._add_feature(vec, f"t:{tri}", 0.5)
        return _l2_normalize(vec)

    def _add_feature(self, vec: np.ndarray, feature: str, weight: float) -> None:
        digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
        idx = int.from_bytes(digest[:4], "little") % self._dim
        sign = 1.0 if digest[4] & 1 == 0 else -1.0
        vec[idx] += weight * sign

    def embed_documents(self, texts: list[str]) -> np.ndarray:
        if not texts:
            return np.zeros((0, self._dim), dtype=np.float32)
        return np.vstack([self._embed_one(t) for t in texts])

    def embed_query(self, text: str) -> np.ndarray:
        return self._embed_one(text)


def embedding_to_bytes(vec: np.ndarray) -> bytes:
    arr = np.asarray(vec, dtype=np.float32).reshape(-1)
    return arr.tobytes()


def bytes_to_embedding(blob: bytes, dim: int) -> np.ndarray:
    return np.frombuffer(blob, dtype=np.float32).reshape(dim)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(_l2_normalize(a), _l2_normalize(b)))


def rrf_score(rank: int, *, k: int = 60, weight: float = 1.0) -> float:
    if rank < 1:
        return 0.0
    return weight / (k + rank)


def mmr_select(
    candidates: list[dict[str, object]],
    *,
    lambda_: float = 0.7,
    limit: int,
    diversity_key: str = "channel_id",
) -> list[dict[str, object]]:
    """Greedy MMR; scores are min-max normalized so penalties stay comparable."""
    if limit <= 0 or not candidates:
        return []
    raw_scores = [float(c.get("score") or 0.0) for c in candidates]  # type: ignore[arg-type]
    lo, hi = min(raw_scores), max(raw_scores)
    span = hi - lo
    norms = [0.5 if span <= 1e-12 else (s - lo) / span for s in raw_scores]

    remaining = list(range(len(candidates)))
    selected: list[dict[str, object]] = []
    seen_keys: set[str] = set()
    while remaining and len(selected) < limit:
        best_i = remaining[0]
        best_val = -math.inf
        for i in remaining:
            rel = norms[i]
            key = str(candidates[i].get(diversity_key) or "")
            penalty = 1.0 if key and key in seen_keys else 0.0
            val = lambda_ * rel - (1.0 - lambda_) * penalty
            if val > best_val:
                best_val = val
                best_i = i
        remaining.remove(best_i)
        chosen = candidates[best_i]
        selected.append(chosen)
        key = str(chosen.get(diversity_key) or "")
        if key:
            seen_keys.add(key)
    return selected
