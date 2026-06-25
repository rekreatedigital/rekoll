"""Hybrid retrieval: fuse vector + lexical results with Reciprocal Rank Fusion.

Zero LLM on this path (ADR-0007). The verbatim store is the floor; lexical is an
additive arm when the adapter advertises it. RRF (k=60, the widely-used constant)
combines the ranked lists without needing comparable raw scores.
"""

from __future__ import annotations

from typing import Iterable, Optional

from .adapters.base import CAP_LEXICAL, QueryHit, QueryResult, StorageAdapter
from .embedding import Embedder
from .model import Kind, Scope, Status
from .reranking import Reranker

__all__ = ["rrf_fuse", "hybrid_search", "DEFAULT_RRF_K"]

DEFAULT_RRF_K = 60


def rrf_fuse(
    result_lists: Iterable[Iterable[QueryHit]],
    *,
    k: int = DEFAULT_RRF_K,
    top: int = 10,
) -> list[QueryHit]:
    """Reciprocal Rank Fusion over several ranked hit lists, keyed by record id."""
    scores: dict[str, float] = {}
    records = {}
    for hits in result_lists:
        for rank, hit in enumerate(hits):
            rid = hit.record.id
            scores[rid] = scores.get(rid, 0.0) + 1.0 / (k + rank + 1)
            records[rid] = hit.record
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    return [QueryHit(record=records[rid], score=score) for rid, score in ranked[:top]]


def hybrid_search(
    adapter: StorageAdapter,
    *,
    scope: Scope,
    query: str,
    embedder: Embedder,
    k: int = 10,
    kind: Optional[Kind] = None,
    where: Optional[dict] = None,
    rrf_k: int = DEFAULT_RRF_K,
    reranker: Optional[Reranker] = None,
    candidates: Optional[int] = None,
    include_quarantined: bool = False,
) -> QueryResult:
    """Vector + (optional) lexical search fused by RRF, then optionally reranked.

    Reads call no LLM — the cross-encoder reranker is a small local model, not a
    generative LLM (ADR-0010). Without a reranker, RRF order is returned.
    Quarantined memory (firewall, ADR-0013) is excluded by default.
    """
    pool = candidates or max(k * 6, k)
    query_vec = embedder.embed([query])[0]
    lists: list[Iterable[QueryHit]] = [
        adapter.vector_query(scope=scope, embedding=query_vec, k=pool, kind=kind, where=where).hits
    ]
    if adapter.supports(CAP_LEXICAL):
        lists.append(
            adapter.lexical_query(scope=scope, text=query, k=pool, kind=kind, where=where).hits
        )
    fused = rrf_fuse(lists, k=rrf_k, top=pool)
    if not include_quarantined:
        fused = [h for h in fused if h.record.status is not Status.QUARANTINED]
    if reranker is not None:
        fused = reranker.rerank(query, fused, top=k)
    else:
        fused = fused[:k]
    return QueryResult(hits=tuple(fused))
