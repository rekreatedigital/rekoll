"""P0-0 / ADR-0022: trust-aware upsert — a lower-trust re-ingest of byte-identical
content must never downgrade or hijack a trusted record.

Confirmed-by-repro finding: the UNIQUE(scope_key, content_hash) upsert let an
UNVERIFIED source that re-submitted a trusted record's exact bytes silently
REPLACE it — the survivor kept the content but took the attacker's provenance and
the LOWER trust tier, and stayed recallable. That directly contradicts the
"trusted provenance" promise (DESIGN §1/§6).
"""

from __future__ import annotations

from rekoll import Kind, Memory, MemoryRecord, Provenance, Scope, Status, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.embedding import StubEmbedder

CONTENT = "the prod database is db.internal:5432 and rotates backups nightly"


def _adapter() -> SQLiteAdapter:
    return SQLiteAdapter(":memory:")


def _rec(scope, source, trust, *, emb=None):
    r = MemoryRecord.create(
        scope=scope, kind=Kind.RAW_FACT, content=CONTENT,
        provenance=Provenance(source_uri=source, adapter_name="t"), trust_tier=trust,
    )
    if emb is not None:
        r.with_embedding(emb.embed([CONTENT])[0], name=emb.identity().name, dim=emb.dim)
    return r


# ---- adapter level --------------------------------------------------------

def test_lower_trust_reingest_does_not_downgrade_or_hijack():
    a, scope = _adapter(), Scope()
    owner = _rec(scope, "user://owner", TrustTier.OWNER)
    a.upsert(records=[owner])
    attacker = _rec(scope, "web://evil", TrustTier.UNVERIFIED)
    a.upsert(records=[attacker])
    assert a.count(scope=scope) == 1
    assert not a.get(scope=scope, ids=[attacker.id]).records, "attacker id must not exist"
    survivor = a.get(scope=scope, ids=[owner.id]).records
    assert survivor and survivor[0].trust_tier is TrustTier.OWNER
    assert survivor[0].provenance.source_uri == "user://owner"
    a.close()


def test_equal_trust_different_source_keeps_first_no_hijack():
    a, scope = _adapter(), Scope()
    first = _rec(scope, "src://one", TrustTier.TRUSTED_SOURCE)
    second = _rec(scope, "src://two", TrustTier.TRUSTED_SOURCE)
    a.upsert(records=[first])
    a.upsert(records=[second])
    assert a.count(scope=scope) == 1
    assert a.get(scope=scope, ids=[first.id]).records, "first (incumbent) must be kept"
    assert not a.get(scope=scope, ids=[second.id]).records, "equal-trust source must not hijack"
    a.close()


def test_strictly_higher_trust_takes_over():
    a, scope = _adapter(), Scope()
    low = _rec(scope, "web://x", TrustTier.UNVERIFIED)
    high = _rec(scope, "user://y", TrustTier.OWNER)
    a.upsert(records=[low])
    a.upsert(records=[high])
    assert a.count(scope=scope) == 1
    survivor = a.get(scope=scope, ids=[high.id]).records
    assert survivor and survivor[0].trust_tier is TrustTier.OWNER
    assert not a.get(scope=scope, ids=[low.id]).records
    a.close()


def test_same_source_reingest_is_idempotent_and_can_re_embed():
    a, scope, emb = _adapter(), Scope(), StubEmbedder()
    r1 = _rec(scope, "user://me", TrustTier.OWNER)  # no embedding
    a.upsert(records=[r1])
    r2 = _rec(scope, "user://me", TrustTier.OWNER, emb=emb)  # same id, now embedded
    assert r1.id == r2.id
    a.upsert(records=[r2])
    assert a.count(scope=scope) == 1
    back = a.get(scope=scope, ids=[r1.id]).records[0]
    assert back.trust_tier is TrustTier.OWNER
    assert back.embedding is not None, "same-source re-ingest must still update (re-embed)"
    a.close()


def test_downgrade_rejected_leaves_no_orphan_rows():
    a, scope = _adapter(), Scope()
    owner = _rec(scope, "user://owner", TrustTier.OWNER)
    a.upsert(records=[owner])
    a.upsert(records=[_rec(scope, "web://evil", TrustTier.UNVERIFIED)])
    fts = [row["rid"] for row in a._conn.execute("SELECT rid FROM fts").fetchall()]
    assert fts == [owner.id], f"unexpected fts rows after rejected downgrade: {fts}"
    a.close()


# ---- facade level (the realistic attack: remember OWNER, then ingest UNVERIFIED) ----

def test_facade_ingest_cannot_downgrade_a_remembered_owner_record():
    mem = Memory(path=":memory:", embedder=StubEmbedder(), reranker=None)
    mem.remember(CONTENT, trust=TrustTier.OWNER)
    # After P0-1, ingest defaults to UNVERIFIED — the realistic poisoning path.
    mem.ingest_text(CONTENT, name="evil.txt")
    hits = mem.recall("prod database backups nightly", k=5)
    assert hits.records(), "the trusted memory must still be recallable"
    top = hits.records()[0]
    assert top.trust_tier is TrustTier.OWNER, "recall surfaced a downgraded record"
    assert top.provenance.source_uri == "user", "recall surfaced hijacked provenance"
    assert top.status is Status.ACTIVE
    mem.close()


def test_conformance_suite_includes_trust_monotonic_check():
    # Guard: the contract must ship in the importable suite all adapters inherit.
    from rekoll import conformance

    names = {c.__name__ for c in conformance.ALL_CHECKS}
    assert "assert_upsert_is_trust_monotonic" in names
    passed = conformance.run_all(lambda: SQLiteAdapter(":memory:"), StubEmbedder())
    assert "assert_upsert_is_trust_monotonic" in passed
