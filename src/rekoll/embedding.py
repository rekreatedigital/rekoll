"""Embedding interface, the embedder-identity guard, and a dependency-free
stub embedder used by the foundation + conformance suite.

Two invariants from the design doc live here:
 - Embeddings default to a LOCAL embedder (no API key, nothing leaves the box).
 - An *embedder-identity guard* prevents silently mixing vectors from different
   models/configs in one scope (the bug MemPalace's name-only check leaves open):
   identity is (name, dim, config_hash), three-state compared.

P1 replaces ``StubEmbedder`` with a real local ONNX model. The stub is
deterministic and fast so the storage contract can be tested without ML deps.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from typing import Literal, Protocol, Sequence, runtime_checkable

__all__ = [
    "EmbedderIdentity",
    "Embedder",
    "StubEmbedder",
    "FastEmbedEmbedder",
    "EmbedderIdentityMismatch",
    "compare_identity",
    "guard_identity",
    "cosine",
]


@dataclass(frozen=True)
class EmbedderIdentity:
    """What produced a vector. Stored per-scope and checked on every write."""

    name: str
    dim: int
    config_hash: str


@runtime_checkable
class Embedder(Protocol):
    @property
    def dim(self) -> int: ...

    def identity(self) -> EmbedderIdentity: ...

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class EmbedderIdentityMismatch(Exception):
    """Raised when a scope's stored embedder identity differs from the current one."""


def compare_identity(
    stored: EmbedderIdentity | None, current: EmbedderIdentity
) -> Literal["unknown", "match", "mismatch"]:
    if stored is None:
        return "unknown"
    return "match" if stored == current else "mismatch"


def guard_identity(stored: EmbedderIdentity | None, current: EmbedderIdentity) -> None:
    """Hard-fail on a silent model/config swap within a scope."""
    if compare_identity(stored, current) == "mismatch":
        raise EmbedderIdentityMismatch(
            f"scope was embedded with {stored!r}, but the current embedder is "
            f"{current!r}. Re-embed the scope or use a separate scope."
        )


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """Cosine similarity. Returns 0.0 for a zero vector.

    Raises ``ValueError`` on a dimension mismatch rather than silently truncating
    to the shorter vector (``zip`` would otherwise return a meaningless score) —
    a real risk once vectors from different embedders coexist.
    """
    if len(a) != len(b):
        raise ValueError(f"cosine: vector dimension mismatch ({len(a)} vs {len(b)})")
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    denom = math.sqrt(na) * math.sqrt(nb)
    return dot / denom if denom else 0.0


class StubEmbedder:
    """Deterministic, dependency-free embedder for the foundation + tests.

    Hashes whitespace tokens into a fixed-dim signed bag-of-hashed-tokens vector,
    L2-normalized. NOT production quality (no semantics) — it exists only so the
    storage/retrieval *contract* can be exercised without shipping an ML model.
    """

    def __init__(self, dim: int = 64) -> None:
        if dim <= 0:
            raise ValueError("dim must be positive")
        self._dim = dim

    @property
    def dim(self) -> int:
        return self._dim

    def identity(self) -> EmbedderIdentity:
        config_hash = hashlib.sha256(f"stub-hash:dim={self._dim}".encode()).hexdigest()[:16]
        return EmbedderIdentity(name="stub-hash", dim=self._dim, config_hash=config_hash)

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self._dim
        for tok in text.lower().split():
            digest = hashlib.md5(tok.encode("utf-8")).digest()
            h = int.from_bytes(digest[:4], "big")
            idx = h % self._dim
            sign = 1.0 if (h >> 8) & 1 else -1.0
            vec[idx] += sign
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FastEmbedEmbedder:
    """Local ONNX embeddings via ``fastembed`` (optional extra, ADR-0009).

    No PyTorch, no API key; runs on CPU. The model downloads once to a local
    cache. Default is a small English model; pass ``model_name`` to change it.
    Install with: ``pip install 'rekoll[embeddings]'``.
    """

    DEFAULT_MODEL = "BAAI/bge-small-en-v1.5"

    def __init__(self, model_name: str = DEFAULT_MODEL, *, cache_dir: str | None = None) -> None:
        try:
            from fastembed import TextEmbedding
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise ImportError(
                "FastEmbedEmbedder needs the optional 'embeddings' extra: "
                "pip install 'rekoll[embeddings]'"
            ) from exc
        self.model_name = model_name
        self._model = TextEmbedding(model_name=model_name, cache_dir=cache_dir)
        self._dim: int | None = None

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def identity(self) -> EmbedderIdentity:
        return EmbedderIdentity(
            name=f"fastembed:{self.model_name}",
            dim=self.dim,
            config_hash=hashlib.sha256(f"fastembed:{self.model_name}".encode()).hexdigest()[:16],
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [list(map(float, vec)) for vec in self._model.embed(list(texts))]
