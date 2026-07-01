"""Hybrid retrieval: fuse vector + lexical results with Reciprocal Rank Fusion.

Zero LLM on this path (ADR-0007). The verbatim store is the floor; lexical is an
additive arm when the adapter advertises it. RRF (k=60, the widely-used constant)
combines the ranked lists without needing comparable raw scores.
"""

from __future__ import annotations

import warnings
from typing import Iterable, Optional

from .adapters.base import CAP_LEXICAL, QueryHit, QueryResult, StorageAdapter
from .embedding import Embedder
from .firewall import sanitize_unicode
from .model import Kind, Scope, Status
from .reranking import Reranker

__all__ = ["rrf_fuse", "hybrid_search", "DEFAULT_RRF_K", "MAX_QUERY_CHARS"]

DEFAULT_RRF_K = 60

# Read path must degrade, never DoS: queries are truncated (not rejected) to a
# bound far past any real question before embedding/lexical search (ADR-0018).
MAX_QUERY_CHARS = 8_192


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


def _verify_hits(hits: list[QueryHit]) -> list[QueryHit]:
    """Tamper check (ADR-0019): demote any hit whose content fails its hash.

    An attacker with direct write access to the backing store bypasses ingest
    screening entirely; the content-address is the detection layer SECURITY.md
    promises. Mismatched records are demoted to QUARANTINED **in memory** (the
    store is never written on the read path) so the normal quarantine filtering
    below decides surfacing, and one warning names the withheld ids.
    """
    bad: list[str] = []
    for hit in hits:
        if not hit.record.verify():
            hit.record.status = Status.QUARANTINED
            bad.append(hit.record.id)
    if bad:
        warnings.warn(
            f"[rekoll] {len(bad)} recalled record(s) failed content-hash "
            f"verification and were withheld (possible direct-DB tampering; "
            f"re-ingest or delete them): {', '.join(sorted(bad))}",
            stacklevel=3,
        )
    return hits


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

    The query is sanitized by the firewall (NFKC + invisible-char strip — the
    same normalization stored content got, so hidden characters can't split
    terms or skew matching) and truncated to ``MAX_QUERY_CHARS`` before any
    embedding or lexical work (DESIGN §7 "query sanitized before embedding").

    Every candidate is content-hash verified before surfacing; a mismatch
    (direct-DB tampering) is demoted to QUARANTINED in memory and warned about
    (ADR-0019).
    """
    query = sanitize_unicode(query)[:MAX_QUERY_CHARS]
    pool = candidates or max(k * 6, k)
    query_vec = embedder.embed([query])[0]
    lists: list[Iterable[QueryHit]] = [
        adapter.vector_query(scope=scope, embedding=query_vec, k=pool, kind=kind, where=where).hits
    ]
    if adapter.supports(CAP_LEXICAL):
        lists.append(
            adapter.lexical_query(scope=scope, text=query, k=pool, kind=kind, where=where).hits
        )
    fused = _verify_hits(rrf_fuse(lists, k=rrf_k, top=pool))
    if not include_quarantined:
        fused = [h for h in fused if h.record.status is not Status.QUARANTINED]
    if reranker is not None:
        fused = reranker.rerank(query, fused, top=k)
    else:
        fused = fused[:k]
    return QueryResult(hits=tuple(fused))
