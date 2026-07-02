"""P0 definition-of-done: the reference SQLite adapter passes the full
conformance suite, advertises capabilities honestly, and resolves via the registry.
"""

from __future__ import annotations

import pytest

from rekoll import conformance
from rekoll.adapters.base import CAP_LEXICAL, CAP_VECTOR, UnsupportedCapabilityError
from rekoll.adapters.registry import available_adapters, get_adapter
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.embedding import StubEmbedder
from rekoll.model import Scope


def _make():
    return SQLiteAdapter(":memory:")


def test_full_conformance_suite_passes():
    passed = conformance.run_all(_make, StubEmbedder())
    assert len(passed) == len(conformance.ALL_CHECKS)


@pytest.mark.parametrize("check", conformance.ALL_CHECKS, ids=lambda c: c.__name__)
def test_each_conformance_check(check):
    # Run each contract check individually so a failure names the exact contract.
    if check is conformance.assert_capabilities_honest:
        check(_make)
    else:
        check(_make, StubEmbedder())


def test_capabilities_are_honest():
    adapter = _make()
    assert CAP_VECTOR in adapter.capabilities
    assert adapter.supports(CAP_LEXICAL)  # FTS5 lexical added in P1
    # an advertised capability must actually work, not raise
    result = adapter.lexical_query(scope=Scope(), text="anything")
    assert hasattr(result, "hits")
    adapter.close()


def test_lexical_search_ranks_keyword_match():
    from rekoll import Kind, MemoryRecord, Provenance, TrustTier

    adapter = _make()
    scope = Scope(tenant="t", project="p", agent="a")

    def rec(text):
        return MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content=text,
            provenance=Provenance(source_uri="t://" + text[:8]), trust_tier=TrustTier.OWNER,
        )

    adapter.add(records=[rec("postgres connection pooling tips"), rec("how to bake bread")])
    hits = adapter.lexical_query(scope=scope, text="postgres pooling", k=5)
    assert hits.hits and "postgres" in hits.hits[0].record.content
    adapter.close()


def test_registry_resolves_builtin_sqlite():
    assert "sqlite" in available_adapters()
    adapter = get_adapter("sqlite", path=":memory:")
    assert isinstance(adapter, SQLiteAdapter)
    adapter.close()


def test_lexical_filter_does_not_starve_valid_matches():
    """A min_trust filter must surface valid matches even when many low-trust rows
    rank ahead of them lexically — the old fixed over-fetch window dropped them."""
    from rekoll import Kind, MemoryRecord, Provenance, TrustTier

    adapter = _make()
    scope = Scope(tenant="t", project="p", agent="a")

    def rec(text, trust, src):
        return MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content=text,
            provenance=Provenance(source_uri=src), trust_tier=trust,
        )

    k = 3  # old window was k*4+10 = 22; we add more matches than that
    records = []
    # Term-dense, short, UNVERIFIED rows rank ABOVE the good rows in BM25.
    for i in range(25):
        records.append(rec(f"match match match match noise{i}", TrustTier.UNVERIFIED, f"t://n{i}"))
    # Sparse, long, CURATED rows — one keyword in a sea of filler => low BM25 rank.
    for i in range(k):
        filler = " ".join(f"filler{i}word{j}" for j in range(40))
        records.append(rec(f"{filler} match {filler}", TrustTier.CURATED, f"t://good{i}"))
    adapter.add(records=records)

    hits = adapter.lexical_query(
        scope=scope, text="match", k=k, where={"min_trust": int(TrustTier.CURATED)}
    )
    assert len(hits) == k, f"min_trust filter starved valid matches: got {len(hits)} of {k}"
    assert all(h.record.trust_tier >= TrustTier.CURATED for h in hits), "filter leaked low-trust rows"
    adapter.close()


def test_upsert_higher_trust_replaces_without_orphans():
    """A STRICTLY higher-trust re-ingest of identical content from a different
    source takes over (trust-aware upsert, ADR-0023) and must not orphan the
    displaced id's fts/metadata rows. (Equal/lower trust is a no-op — covered in
    test_trust_upsert.py.)"""
    from rekoll import Kind, MemoryRecord, Provenance, TrustTier

    adapter = _make()
    scope = Scope(tenant="t", project="p", agent="a")

    def rec(src, trust):
        return MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content="identical body text",
            provenance=Provenance(source_uri=src), trust_tier=trust,
            metadata={"k": "v"},
        )

    r1 = rec("src://one", TrustTier.UNVERIFIED)
    r2 = rec("src://two", TrustTier.OWNER)  # strict upgrade takes over
    assert r1.id != r2.id, "id is derived from source_uri, so these differ"
    adapter.upsert(records=[r1])
    adapter.upsert(records=[r2])

    assert adapter.count(scope=scope) == 1, "UNIQUE(scope, content_hash) must collapse to one row"
    fts_rids = [row["rid"] for row in adapter._conn.execute("SELECT rid FROM fts").fetchall()]
    assert fts_rids == [r2.id], f"orphaned fts rows remain: {fts_rids}"
    meta_ids = [
        row["record_id"]
        for row in adapter._conn.execute("SELECT DISTINCT record_id FROM record_metadata").fetchall()
    ]
    assert meta_ids == [r2.id], f"orphaned metadata rows remain: {meta_ids}"
    adapter.close()


def test_persists_to_disk(tmp_path):
    from rekoll import Kind, MemoryRecord, Provenance, TrustTier

    db = str(tmp_path / "mem.db")
    scope = Scope(tenant="t", project="p", agent="a")
    record = MemoryRecord.create(
        scope=scope,
        kind=Kind.RAW_FACT,
        content="persisted across connections",
        provenance=Provenance(source_uri="src://x"),
        trust_tier=TrustTier.OWNER,
    )
    a1 = SQLiteAdapter(db)
    a1.add(records=[record])
    a1.close()

    a2 = SQLiteAdapter(db)
    got = a2.get(scope=scope, ids=[record.id])
    assert len(got) == 1
    assert got.records[0].content == "persisted across connections"
    a2.close()


def test_bump_proof_count_is_atomic_and_scope_and_status_aware():
    """The SQLite bump is an in-DB proof_count += 1 (was-it-used signal): it
    credits only in-scope, non-quarantined ids, is repeatable, and touches no
    other column."""
    from rekoll import Kind, MemoryRecord, Provenance, Status, TrustTier

    adapter = _make()
    scope = Scope(tenant="t", project="p", agent="a")
    other = Scope(tenant="t", project="other", agent="a")

    def rec(text, scp=scope, status=Status.ACTIVE):
        r = MemoryRecord.create(
            scope=scp, kind=Kind.RAW_FACT, content=text,
            provenance=Provenance(source_uri="t://" + text[:12]), trust_tier=TrustTier.OWNER,
        )
        r.status = status
        return r

    live = rec("live fact to credit")
    quar = rec("quarantined fact", status=Status.QUARANTINED)
    elsewhere = rec("cross scope fact", scp=other)
    adapter.upsert(records=[live, quar, elsewhere])

    # Only the live in-scope id is credited; quarantined + cross-scope + unknown ignored.
    credited = adapter.bump_proof_count(
        scope=scope, ids=[live.id, quar.id, elsewhere.id, "no-such-id"]
    )
    assert credited == 1
    assert adapter.get(scope=scope, ids=[live.id]).records[0].proof_count == 1
    assert adapter.get(scope=scope, ids=[quar.id]).records[0].proof_count == 0
    assert adapter.get(scope=other, ids=[elsewhere.id]).records[0].proof_count == 0

    # Repeatable: a second bump reads the CURRENT on-disk value, reaching 2.
    assert adapter.bump_proof_count(scope=scope, ids=[live.id]) == 1
    got = adapter.get(scope=scope, ids=[live.id]).records[0]
    assert got.proof_count == 2
    assert got.content == "live fact to credit"  # nothing else changed
    assert adapter.bump_proof_count(scope=scope, ids=[]) == 0  # empty is a no-op
    adapter.close()


def test_base_bump_proof_count_fallback_read_modify_write():
    """An adapter that does NOT override bump_proof_count still works via the
    base read-modify-write fallback (correct under a single writer)."""
    from rekoll.adapters.base import StorageAdapter

    class _NoBumpAdapter(SQLiteAdapter):
        # Drop the specialized override to exercise the base fallback.
        bump_proof_count = StorageAdapter.bump_proof_count

    from rekoll import Kind, MemoryRecord, Provenance, TrustTier

    adapter = _NoBumpAdapter(":memory:")
    scope = Scope(tenant="t", project="p", agent="a")
    r = MemoryRecord.create(
        scope=scope, kind=Kind.RAW_FACT, content="credited via the base fallback",
        provenance=Provenance(source_uri="t://fallback"), trust_tier=TrustTier.OWNER,
    )
    adapter.upsert(records=[r])
    assert adapter.bump_proof_count(scope=scope, ids=[r.id]) == 1
    assert adapter.get(scope=scope, ids=[r.id]).records[0].proof_count == 1
    adapter.close()
