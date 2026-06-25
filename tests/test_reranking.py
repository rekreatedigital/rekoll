"""P1: cross-encoder reranking + its integration into hybrid_search."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from rekoll import Kind, MemoryRecord, Provenance, Scope, StubEmbedder, TrustTier
from rekoll.adapters.base import QueryHit
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.retrieval import hybrid_search


@dataclass
class _Rec:
    id: str
    content: str


class _ForceWinner:
    """Deterministic test reranker: pushes a chosen id to the top."""

    def __init__(self, winner_id: str) -> None:
        self.winner = winner_id

    def rerank(self, query, hits, *, top=None):
        hits = list(hits)
        ordered = sorted(hits, key=lambda h: 0 if h.record.id == self.winner else 1)
        out = [
            QueryHit(record=h.record, score=(1.0 if h.record.id == self.winner else 0.0))
            for h in ordered
        ]
        return out[:top] if top is not None else out


def _rec(scope, text, emb):
    record = MemoryRecord.create(
        scope=scope, kind=Kind.RAW_FACT, content=text,
        provenance=Provenance(source_uri="t://" + text[:10]), trust_tier=TrustTier.OWNER,
    )
    record.with_embedding(emb.embed([text])[0], name=emb.identity().name, dim=emb.dim)
    return record


def test_hybrid_search_applies_reranker():
    emb = StubEmbedder()
    db = SQLiteAdapter(":memory:")
    scope = Scope(tenant="t", project="p", agent="a")
    a = _rec(scope, "alpha apples and oranges", emb)
    b = _rec(scope, "beta bananas grapes fruit basket", emb)
    c = _rec(scope, "gamma carrots celery and potatoes", emb)
    db.add(records=[a, b, c])
    # Force c to the top even though the query is about fruit — proves the reranker is applied.
    result = hybrid_search(db, scope=scope, query="fruit", embedder=emb, k=3, reranker=_ForceWinner(c.id))
    assert result.hits[0].record.id == c.id
    db.close()


def test_no_reranker_keeps_rrf_order():
    emb = StubEmbedder()
    db = SQLiteAdapter(":memory:")
    scope = Scope(tenant="t", project="p", agent="a")
    db.add(records=[
        _rec(scope, "database migration rollback procedure", emb),
        _rec(scope, "pizza recipe dough and cheese", emb),
    ])
    result = hybrid_search(db, scope=scope, query="database migration", embedder=emb, k=2)
    assert result.hits[0].record.content.startswith("database migration")
    db.close()


def test_cross_encoder_reranker_real():
    pytest.importorskip("fastembed")
    from rekoll.reranking import CrossEncoderReranker

    reranker = CrossEncoderReranker()
    hits = [
        QueryHit(record=_Rec("cat", "a fluffy cat naps on the warm windowsill"), score=0.0),
        QueryHit(record=_Rec("phys", "the lagrangian formulation of classical mechanics"), score=0.0),
    ]
    out = reranker.rerank("what is the lagrangian in physics", hits)
    assert out[0].record.id == "phys", "cross-encoder should rank the physics passage first"
