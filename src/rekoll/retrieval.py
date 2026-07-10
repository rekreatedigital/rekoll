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
from .model import RECALLABLE_STATUSES, Kind, Scope, Status, TrustTier
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
            # Safe to mutate: adapters reconstruct a FRESH MemoryRecord per query
            # (no shared/cached instance), so this demotion is local to this
            # result set and never persisted (reads stay side-effect-free).
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
    use_vector: bool = True,
    use_lexical: bool = True,
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

    ``candidates`` sizes the pool each leg retrieves and RRF fuses (default
    ``6*k``). It is a **reranker-feeding knob, NOT a recall knob**: a reranker
    rescores the whole pool, so a deeper pool gives it more to find. Without a
    reranker there is nothing to rescore — RRF simply fuses two longer ranked
    lists whose tails are noise, and that noise reaches the top-k. Raising
    ``candidates`` with no reranker therefore *lowers* quality (measured
    paraphrase recall@5 on a 1,000-doc corpus: 0.901 at pool 30, 0.870 at 50,
    0.823 at 100, 0.760 at 200) and warns. Note a reranker rescues depth but
    does not beat a shallow pool: pool 200 + reranker recovers only to 0.849.

    ``use_vector=False`` refuses the vector leg entirely — the query is never
    embedded and ``vector_query`` is never called. Callers use this for honest
    lexical-only degradation when the scope's stored vectors are not comparable
    to the current embedder (embedder-identity mismatch, ADR-0024).

    ``use_lexical=False`` is its mirror: the lexical leg is refused even on an
    adapter that advertises ``CAP_LEXICAL``, and ``lexical_query`` is never
    called. Callers use it for a vector-only run — an ablation arm, or a scope
    whose FTS index is corrupt/misbehaving — without dropping to
    ``adapter.vector_query`` directly, which would bypass this function's query
    sanitization, tamper verification and quarantine filtering (#33).

    With BOTH legs refused (or ``use_vector=False`` on an adapter with no
    lexical capability) the result is honestly empty rather than a garbage
    ranking.
    """
    query = sanitize_unicode(query)[:MAX_QUERY_CHARS]
    default_pool = max(k * 6, k)
    pool = candidates or default_pool
    if candidates is not None and candidates > default_pool and reranker is None:
        warnings.warn(
            f"[rekoll] candidates={candidates} exceeds the default pool of "
            f"{default_pool} (6*k, k={k}) and no reranker is attached. "
            f"`candidates` FEEDS A RERANKER; it is not a recall knob. Under "
            f"RRF-only ranking a deeper pool admits more of each leg's noisy "
            f"tail into the top-k and MEASURABLY DEGRADES quality (measured "
            f"paraphrase recall@5: 0.901 at pool 30 -> 0.760 at pool 200). "
            f"Attach a reranker, or leave `candidates` unset (issue #36).",
            stacklevel=2,
        )
    lists: list[Iterable[QueryHit]] = []
    if use_vector:
        query_vec = embedder.embed([query])[0]
        lists.append(
            adapter.vector_query(scope=scope, embedding=query_vec, k=pool, kind=kind, where=where).hits
        )
    if use_lexical and adapter.supports(CAP_LEXICAL):
        lists.append(
            adapter.lexical_query(scope=scope, text=query, k=pool, kind=kind, where=where).hits
        )
    if not lists:
        # No vector leg (refused) AND no lexical leg (refused, or the adapter
        # has no lexical capability): honestly empty rather than a garbage
        # ranking (ADR-0024).
        return QueryResult(hits=())
    fused = _verify_hits(rrf_fuse(lists, k=rrf_k, top=pool))
    if not include_quarantined:
        # The surfacing filter must AGREE with the envelope (build_envelope
        # drops status==QUARANTINED AND trust<=QUARANTINED): RecallResult's raw
        # accessors (.texts()/.ids()/.records()) expose exactly these hits, so
        # anything the envelope would withhold must be withheld here too.
        # Construction already forces status=QUARANTINED at quarantine-level
        # trust (model.MemoryRecord); the trust clause keeps the agreement
        # explicit for any adapter/legacy row that slips one through.
        # "Recallable" itself (status==ACTIVE) is defined ONCE in
        # model.RECALLABLE_STATUSES — the same predicate the MCP status count
        # uses — so a future supersede/propose loop can't surface here either.
        fused = [
            h for h in fused
            if h.record.status in RECALLABLE_STATUSES
            and h.record.trust_tier > TrustTier.QUARANTINED
        ]
    if reranker is not None:
        fused = reranker.rerank(query, fused, top=k)
    else:
        fused = fused[:k]
    return QueryResult(hits=tuple(fused))
