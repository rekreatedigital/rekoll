"""Cross-encoder reranking — the precision lever on top of RRF.

A cross-encoder scores (query, passage) *jointly*, so it ranks far more precisely
than the bi-encoder cosine used for first-pass retrieval. It is a small local
transformer (via fastembed, the same optional ``embeddings`` extra) — NOT a
generative LLM, so the reads-never-call-an-LLM invariant (ADR-0007) holds.

Reranking is optional: if the extra isn't installed, ``hybrid_search`` keeps RRF
order (a documented passthrough), so the core path always works (ADR-0010).
"""

from __future__ import annotations

from typing import Optional, Protocol, Sequence, runtime_checkable

from .adapters.base import QueryHit

__all__ = ["Reranker", "CrossEncoderReranker"]


@runtime_checkable
class Reranker(Protocol):
    def rerank(
        self, query: str, hits: Sequence[QueryHit], *, top: Optional[int] = None
    ) -> list[QueryHit]: ...


class CrossEncoderReranker:
    """Local ONNX cross-encoder reranker via fastembed (optional ``embeddings`` extra)."""

    DEFAULT_MODEL = "Xenova/ms-marco-MiniLM-L-6-v2"

    def __init__(self, model_name: str = DEFAULT_MODEL, *, cache_dir: str | None = None) -> None:
        try:
            from fastembed.rerank.cross_encoder import TextCrossEncoder
        except ImportError as exc:  # pragma: no cover - only without the extra
            raise ImportError(
                "CrossEncoderReranker needs the optional 'embeddings' extra: "
                "pip install 'rekoll[embeddings]'"
            ) from exc
        self.model_name = model_name
        self._encoder = TextCrossEncoder(model_name=model_name, cache_dir=cache_dir)

    def rerank(
        self, query: str, hits: Sequence[QueryHit], *, top: Optional[int] = None
    ) -> list[QueryHit]:
        hits = list(hits)
        if not hits:
            return []
        scores = list(self._encoder.rerank(query, [h.record.content for h in hits]))
        order = sorted(range(len(hits)), key=lambda i: scores[i], reverse=True)
        reranked = [QueryHit(record=hits[i].record, score=float(scores[i])) for i in order]
        return reranked[:top] if top is not None else reranked
