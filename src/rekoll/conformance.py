"""Importable storage-adapter conformance suite.

This module ships *assertions*, not test functions, so the SAME contract is
verified identically by the reference SQLite adapter and by any third-party
backend (``from rekoll.conformance import run_all``). It is the executable
definition of "a correct Rekoll storage adapter."

Usage::

    from rekoll.conformance import run_all
    from rekoll.adapters.sqlite import SQLiteAdapter
    from rekoll.embedding import StubEmbedder

    run_all(lambda: SQLiteAdapter(":memory:"), StubEmbedder())
"""

from __future__ import annotations

from typing import Callable, Optional

from .adapters.base import CAP_LEXICAL, CAP_VECTOR, StorageAdapter, UnsupportedCapabilityError
from .embedding import Embedder, StubEmbedder
from .model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier

AdapterFactory = Callable[[], StorageAdapter]

_SCOPE_A = Scope(tenant="acme", project="app", agent="bot")
_SCOPE_B = Scope(tenant="acme", project="other", agent="bot")


def _rec(
    scope: Scope,
    text: str,
    *,
    kind: Kind = Kind.RAW_FACT,
    trust: TrustTier = TrustTier.TRUSTED_SOURCE,
    embedder: Optional[Embedder] = None,
    source: str = "test://doc",
    **kwargs: object,
) -> MemoryRecord:
    record = MemoryRecord.create(
        scope=scope,
        kind=kind,
        content=text,
        provenance=Provenance(source_uri=source, adapter_name="conformance"),
        trust_tier=trust,
        **kwargs,  # type: ignore[arg-type]
    )
    if embedder is not None:
        vector = embedder.embed([text])[0]
        record.with_embedding(vector, name=embedder.identity().name, dim=embedder.dim)
    return record


def assert_capabilities_honest(make: AdapterFactory) -> None:
    adapter = make()
    assert CAP_VECTOR in adapter.capabilities, "every adapter must support the vector core"
    if not adapter.supports(CAP_LEXICAL):
        try:
            adapter.lexical_query(scope=_SCOPE_A, text="x")
        except UnsupportedCapabilityError:
            pass
        else:
            raise AssertionError("lexical not advertised but did not raise UnsupportedCapabilityError")
    adapter.close()


def assert_kwargs_only(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    record = _rec(_SCOPE_A, "hello world", embedder=embedder)
    try:
        adapter.add([record])  # type: ignore[misc]  # positional must fail
    except TypeError:
        pass
    else:
        raise AssertionError("add() accepted a positional arg; methods must be keyword-only")
    adapter.add(records=[record])  # keyword works
    adapter.close()


def assert_add_and_get_roundtrip(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    record = _rec(_SCOPE_A, "the sky is blue", trust=TrustTier.CURATED, embedder=embedder)
    adapter.add(records=[record])
    got = adapter.get(scope=_SCOPE_A, ids=[record.id])
    assert len(got) == 1, "expected exactly one record back"
    back = got.records[0]
    assert back.id == record.id
    assert back.content == "the sky is blue"
    assert back.content_hash == record.content_hash
    assert back.trust_tier == TrustTier.CURATED
    assert back.kind == Kind.RAW_FACT
    assert back.provenance.source_uri == "test://doc"
    assert back.verify(), "content_hash must still match content after round-trip"
    adapter.close()


def assert_content_addressed_idempotent(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    first = _rec(_SCOPE_A, "same content", embedder=embedder)
    second = _rec(_SCOPE_A, "same content", embedder=embedder)
    assert first.id == second.id, "same content+scope+source must yield the same id"
    adapter.upsert(records=[first])
    adapter.upsert(records=[second])
    assert adapter.count(scope=_SCOPE_A) == 1, "re-upserting identical content must not duplicate"
    adapter.close()


def assert_upsert_is_trust_monotonic(make: AdapterFactory, embedder: Embedder) -> None:
    """Trust-aware upsert (ADR-0022): identical content re-ingested from a
    DIFFERENT source must never LOWER the stored trust tier or hijack the trusted
    record's provenance. Only a STRICTLY higher trust tier may take over. Every
    adapter inherits this contract, since the naive UNIQUE(scope, content_hash)
    upsert would otherwise let an attacker downgrade a trusted memory by
    re-submitting its exact bytes."""
    text = "the production database credentials rotate monthly"

    # 1) Lower-trust re-ingest from a different source must be a NO-OP.
    adapter = make()
    trusted = _rec(_SCOPE_A, text, trust=TrustTier.OWNER, source="user://owner", embedder=embedder)
    attacker = _rec(_SCOPE_A, text, trust=TrustTier.UNVERIFIED, source="web://evil", embedder=embedder)
    assert trusted.id != attacker.id, "different sources must yield different ids"
    adapter.upsert(records=[trusted])
    adapter.upsert(records=[attacker])
    assert adapter.count(scope=_SCOPE_A) == 1, "identical content must collapse to one row"
    survivors = adapter.get(scope=_SCOPE_A, ids=[trusted.id, attacker.id]).records
    assert len(survivors) == 1, "exactly one of the colliding ids should survive"
    survivor = survivors[0]
    assert survivor.id == trusted.id, "lower-trust re-ingest replaced the trusted record"
    assert survivor.trust_tier == TrustTier.OWNER, "lower-trust re-ingest DOWNGRADED a trusted record"
    assert survivor.provenance.source_uri == "user://owner", "attacker HIJACKED the record's provenance"
    adapter.close()

    # 2) A strictly HIGHER-trust source may legitimately take over (upgrade).
    adapter = make()
    low = _rec(_SCOPE_A, text, trust=TrustTier.UNVERIFIED, source="web://x", embedder=embedder)
    high = _rec(_SCOPE_A, text, trust=TrustTier.OWNER, source="user://y", embedder=embedder)
    adapter.upsert(records=[low])
    adapter.upsert(records=[high])
    assert adapter.count(scope=_SCOPE_A) == 1
    upgraded = adapter.get(scope=_SCOPE_A, ids=[high.id]).records
    assert upgraded and upgraded[0].trust_tier == TrustTier.OWNER, \
        "a strictly higher-trust source must be able to take over identical content"
    assert not adapter.get(scope=_SCOPE_A, ids=[low.id]).records, "the displaced low-trust id must be gone"
    adapter.close()


def assert_add_strict_on_duplicate(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    record = _rec(_SCOPE_A, "unique once", embedder=embedder)
    adapter.add(records=[record])
    try:
        adapter.add(records=[record])
    except Exception:
        pass
    else:
        raise AssertionError("add() must raise on a duplicate content-address (use upsert to be idempotent)")
    adapter.close()


def assert_scope_isolation(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    a = _rec(_SCOPE_A, "tenant a secret", embedder=embedder)
    b = _rec(_SCOPE_B, "tenant b secret", embedder=embedder)
    adapter.add(records=[a, b])
    # get is scoped
    assert len(adapter.get(scope=_SCOPE_A, ids=[a.id, b.id])) == 1
    assert adapter.get(scope=_SCOPE_A, ids=[b.id]).records == ()
    # count is scoped
    assert adapter.count(scope=_SCOPE_A) == 1
    assert adapter.count(scope=_SCOPE_B) == 1
    # vector search is scoped
    qvec = embedder.embed(["tenant b secret"])[0]
    hits = adapter.vector_query(scope=_SCOPE_A, embedding=qvec, k=10)
    assert all(h.record.scope == _SCOPE_A for h in hits), "vector_query leaked across scopes"
    adapter.close()


def assert_trust_roundtrip(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    for tier in TrustTier:
        record = _rec(_SCOPE_A, f"trusted at {tier.name}", trust=tier, embedder=embedder)
        adapter.upsert(records=[record])
        back = adapter.get(scope=_SCOPE_A, ids=[record.id]).records[0]
        assert back.trust_tier == tier, f"trust tier {tier!r} did not round-trip"
    adapter.close()


def assert_metadata_scalar_roundtrip(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    meta = {"tag": "alpha", "n": 7, "ratio": 1.5, "flag": True, "missing": None}
    record = _rec(_SCOPE_A, "with metadata", embedder=embedder, metadata=meta)
    adapter.add(records=[record])
    back = adapter.get(scope=_SCOPE_A, ids=[record.id]).records[0]
    assert back.metadata == meta, f"metadata did not round-trip: {back.metadata!r} != {meta!r}"
    assert isinstance(back.metadata["flag"], bool)
    assert isinstance(back.metadata["n"], int)
    assert isinstance(back.metadata["ratio"], float)
    adapter.close()


def assert_embedder_identity(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    assert adapter.get_embedder_identity(scope=_SCOPE_A) is None, "unset identity must be None"
    identity = embedder.identity()
    adapter.set_embedder_identity(scope=_SCOPE_A, identity=identity)
    assert adapter.get_embedder_identity(scope=_SCOPE_A) == identity
    assert adapter.get_embedder_identity(scope=_SCOPE_B) is None, "identity must be per-scope"
    adapter.close()


def assert_vector_query_ranks(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    target = _rec(_SCOPE_A, "database migration rollback plan", embedder=embedder)
    other = _rec(_SCOPE_A, "lunch menu tacos and salad", embedder=embedder)
    adapter.add(records=[target, other])
    qvec = embedder.embed(["database migration rollback plan"])[0]
    hits = adapter.vector_query(scope=_SCOPE_A, embedding=qvec, k=2)
    assert len(hits) == 2
    assert hits.hits[0].record.id == target.id, "exact match must rank first"
    assert hits.hits[0].score >= hits.hits[1].score, "hits must be sorted by score descending"
    # k limit honored
    assert len(adapter.vector_query(scope=_SCOPE_A, embedding=qvec, k=1)) == 1
    adapter.close()


def assert_where_honesty(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    active = _rec(_SCOPE_A, "active fact", embedder=embedder, status=Status.ACTIVE)
    proposed = _rec(_SCOPE_A, "proposed fact", embedder=embedder, status=Status.PROPOSED)
    adapter.add(records=[active, proposed])
    qvec = embedder.embed(["fact"])[0]
    only_active = adapter.vector_query(scope=_SCOPE_A, embedding=qvec, k=10, where={"status": "active"})
    assert {h.record.id for h in only_active} == {active.id}, "status filter not applied"
    try:
        adapter.vector_query(scope=_SCOPE_A, embedding=qvec, where={"made_up_key": 1})
    except (ValueError, UnsupportedCapabilityError):
        pass
    else:
        raise AssertionError("unknown where key must raise, never be silently ignored")
    adapter.close()


def assert_delete(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    record = _rec(_SCOPE_A, "to be deleted", embedder=embedder)
    adapter.add(records=[record])
    assert adapter.delete(scope=_SCOPE_A, ids=[record.id]) == 1
    assert adapter.count(scope=_SCOPE_A) == 0
    assert adapter.delete(scope=_SCOPE_A, ids=[record.id]) == 0, "deleting a missing id removes nothing"
    adapter.close()


def assert_delete_scope_isolation(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    a = _rec(
        _SCOPE_A, "scope a keeps its metadata and index", embedder=embedder,
        metadata={"owner": "a-secret"},
    )
    adapter.add(records=[a])
    # A caller in a DIFFERENT scope must not delete or corrupt A's rows by passing
    # A's id: delete() returns 0 and A's metadata + lexical index survive intact.
    assert adapter.delete(scope=_SCOPE_B, ids=[a.id]) == 0, "cross-scope delete must remove nothing"
    back = adapter.get(scope=_SCOPE_A, ids=[a.id])
    assert len(back) == 1, "cross-scope delete wrongly removed an in-scope record"
    assert back.records[0].metadata.get("owner") == "a-secret", \
        "cross-scope delete corrupted another scope's metadata"
    if adapter.supports(CAP_LEXICAL):
        hits = adapter.lexical_query(scope=_SCOPE_A, text="metadata index")
        assert any(h.record.id == a.id for h in hits), \
            "cross-scope delete corrupted another scope's lexical index"
    # In-scope delete still works and removes exactly one.
    assert adapter.delete(scope=_SCOPE_A, ids=[a.id]) == 1
    assert adapter.count(scope=_SCOPE_A) == 0
    adapter.close()


def assert_lexical_if_supported(make: AdapterFactory, embedder: Embedder) -> None:
    adapter = make()
    if not adapter.supports(CAP_LEXICAL):
        adapter.close()
        return
    target = _rec(_SCOPE_A, "the quarterly revenue report for fiscal year", embedder=embedder)
    other = _rec(_SCOPE_A, "kitchen recipe for sourdough bread loaves", embedder=embedder)
    cross = _rec(_SCOPE_B, "the quarterly revenue report for tenant b", embedder=embedder)
    adapter.add(records=[target, other, cross])
    hits = adapter.lexical_query(scope=_SCOPE_A, text="quarterly revenue report", k=5)
    ids = [h.record.id for h in hits]
    assert target.id in ids, "lexical search missed an obvious keyword match"
    assert all(h.record.scope == _SCOPE_A for h in hits), "lexical_query leaked across scopes"
    assert len(adapter.lexical_query(scope=_SCOPE_A, text="!!!")) == 0, \
        "punctuation-only query must return empty, not crash"
    adapter.close()


ALL_CHECKS = (
    assert_capabilities_honest,
    assert_kwargs_only,
    assert_add_and_get_roundtrip,
    assert_content_addressed_idempotent,
    assert_upsert_is_trust_monotonic,
    assert_add_strict_on_duplicate,
    assert_scope_isolation,
    assert_trust_roundtrip,
    assert_metadata_scalar_roundtrip,
    assert_embedder_identity,
    assert_vector_query_ranks,
    assert_where_honesty,
    assert_delete,
    assert_delete_scope_isolation,
    assert_lexical_if_supported,
)


def run_all(make: AdapterFactory, embedder: Optional[Embedder] = None) -> list[str]:
    """Run every conformance check against a fresh adapter; return passed names."""
    embedder = embedder or StubEmbedder()
    passed: list[str] = []
    for check in ALL_CHECKS:
        if check is assert_capabilities_honest:
            check(make)  # type: ignore[call-arg]
        else:
            check(make, embedder)  # type: ignore[call-arg]
        passed.append(check.__name__)
    return passed
