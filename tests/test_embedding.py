"""Embedder identity guard + cosine dimension safety."""

from __future__ import annotations

import pytest

from rekoll import Kind, MemoryRecord, Provenance, Scope, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.embedding import StubEmbedder, cosine


def test_cosine_basic():
    assert cosine([1.0, 0.0], [1.0, 0.0]) == 1.0
    assert cosine([0.0, 0.0], [1.0, 2.0]) == 0.0  # zero vector -> 0, no div-by-zero


def test_cosine_raises_on_dimension_mismatch():
    # Previously zip() silently truncated and returned a bogus 1.0 here.
    with pytest.raises(ValueError):
        cosine([1.0, 0.0], [1.0, 0.0, 0.0, 0.0, 0.0])


def test_vector_query_skips_incompatible_dim_rows():
    """A stored vector from a different embedder dim must not crash vector_query;
    it is skipped so same-dim hits are still returned (graceful degradation)."""
    adapter = SQLiteAdapter(":memory:")
    scope = Scope(tenant="t", project="p", agent="a")
    emb = StubEmbedder(dim=64)

    def rec(text):
        r = MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content=text,
            provenance=Provenance(source_uri="t://" + text[:8]), trust_tier=TrustTier.OWNER,
        )
        r.with_embedding(emb.embed([text])[0], name=emb.identity().name, dim=emb.dim)
        return r

    good = rec("a normal in-dimension record")
    adapter.add(records=[good])
    # Inject a stray 8-dim vector directly (simulating a prior embedder).
    stray = MemoryRecord.create(
        scope=scope, kind=Kind.RAW_FACT, content="stray wrong-dim vector",
        provenance=Provenance(source_uri="t://stray"), trust_tier=TrustTier.OWNER,
    )
    stray.with_embedding([0.1] * 8, name="other", dim=8)
    adapter.add(records=[stray])

    qvec = emb.embed(["a normal in-dimension record"])[0]
    hits = adapter.vector_query(scope=scope, embedding=qvec, k=10)  # must not raise
    ids = {h.record.id for h in hits}
    assert good.id in ids
    assert stray.id not in ids, "incompatible-dim row should be skipped, not scored"
    adapter.close()
