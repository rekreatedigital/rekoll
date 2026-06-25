"""P1: RRF fusion + hybrid (vector + lexical) search."""

from __future__ import annotations

from dataclasses import dataclass

from rekoll import Kind, MemoryRecord, Provenance, Scope, StubEmbedder, TrustTier
from rekoll.adapters.base import QueryHit
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.retrieval import hybrid_search, rrf_fuse


@dataclass
class _FakeRecord:
    id: str


def _hit(rid: str) -> QueryHit:
    return QueryHit(record=_FakeRecord(rid), score=0.0)


def test_rrf_fuse_rewards_appearing_high_in_multiple_lists():
    list_a = [_hit("a"), _hit("b"), _hit("c")]
    list_b = [_hit("b"), _hit("a")]
    fused = rrf_fuse([list_a, list_b], top=3)
    ids = [h.record.id for h in fused]
    assert set(ids) == {"a", "b", "c"}
    # a and b appear in both lists, so they outrank c (only in one list)
    assert ids.index("c") == 2


def test_rrf_fuse_top_limit():
    big = [_hit(str(i)) for i in range(20)]
    assert len(rrf_fuse([big], top=5)) == 5


def _rec(scope, text, emb):
    record = MemoryRecord.create(
        scope=scope, kind=Kind.RAW_FACT, content=text,
        provenance=Provenance(source_uri="t://" + text[:10]), trust_tier=TrustTier.OWNER,
    )
    record.with_embedding(emb.embed([text])[0], name=emb.identity().name, dim=emb.dim)
    return record


def test_hybrid_search_finds_relevant_record():
    emb = StubEmbedder()
    db = SQLiteAdapter(":memory:")
    scope = Scope(tenant="t", project="p", agent="a")
    db.add(records=[
        _rec(scope, "database migration rollback procedure for production", emb),
        _rec(scope, "favorite pizza toppings and dough recipe", emb),
    ])
    hits = hybrid_search(db, scope=scope, query="database migration rollback", embedder=emb, k=2)
    assert hits.hits, "hybrid search returned nothing"
    assert hits.hits[0].record.content.startswith("database migration")
    db.close()


def test_hybrid_search_is_scoped():
    emb = StubEmbedder()
    db = SQLiteAdapter(":memory:")
    a = Scope(tenant="t", project="a", agent="x")
    b = Scope(tenant="t", project="b", agent="x")
    db.add(records=[_rec(a, "secret alpha document", emb), _rec(b, "secret beta document", emb)])
    hits = hybrid_search(db, scope=a, query="secret document", embedder=emb, k=5)
    assert all(h.record.scope == a for h in hits.hits)
    db.close()
