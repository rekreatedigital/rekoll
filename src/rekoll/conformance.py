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

from datetime import datetime, timezone
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
    """Trust-aware upsert (ADR-0023): identical content re-ingested from a
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


def assert_proof_count_monotonic_on_upsert(make: AdapterFactory, embedder: Embedder) -> None:
    """The was-it-used signal must survive idempotent re-ingest, mirroring the
    trust-monotonic rule (ADR-0023): a same-content upsert keeps
    MAX(stored, incoming) proof_count. A freshly-built incoming record carries
    proof_count=0, and writing that over a promoted value silently erased the
    usage evidence ``mark_used`` had accumulated."""
    adapter = make()
    first = _rec(_SCOPE_A, "credited then re-ingested", embedder=embedder)
    adapter.upsert(records=[first])
    adapter.bump_proof_count(scope=_SCOPE_A, ids=[first.id])
    again = _rec(_SCOPE_A, "credited then re-ingested", embedder=embedder)
    assert again.proof_count == 0  # a fresh record starts uncredited
    adapter.upsert(records=[again])
    back = adapter.get(scope=_SCOPE_A, ids=[first.id]).records[0]
    assert back.proof_count == 1, "idempotent re-ingest zeroed the was-it-used signal"
    # An incoming HIGHER count (an import/restore carrying usage) may raise it.
    richer = _rec(_SCOPE_A, "credited then re-ingested", embedder=embedder, proof_count=5)
    adapter.upsert(records=[richer])
    back = adapter.get(scope=_SCOPE_A, ids=[first.id]).records[0]
    assert back.proof_count == 5, "a higher incoming proof_count must win (MAX)"
    adapter.close()


def assert_k_nonpositive_returns_empty(make: AdapterFactory, embedder: Embedder) -> None:
    """``k <= 0`` asks for nothing and must return nothing — from BOTH query
    legs. The reference lexical implementation returned ONE hit for k<=0 (its
    bound was checked after an append) while the vector leg returned zero; a
    caller iterating "top k" would see phantom results."""
    adapter = make()
    record = _rec(_SCOPE_A, "phantom results for k zero", embedder=embedder)
    adapter.add(records=[record])
    qvec = embedder.embed(["phantom results"])[0]
    for k in (0, -1):
        assert len(adapter.vector_query(scope=_SCOPE_A, embedding=qvec, k=k)) == 0, \
            f"vector_query(k={k}) must return no hits"
        if adapter.supports(CAP_LEXICAL):
            assert len(adapter.lexical_query(scope=_SCOPE_A, text="phantom results", k=k)) == 0, \
                f"lexical_query(k={k}) must return no hits"
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


def assert_distance_metric_honest(make: AdapterFactory, embedder: Embedder) -> None:
    """``distance_metric`` must describe what ``vector_query`` actually returns.

    This attribute stopped being decorative in ADR-0028. ``recall(min_score=...)``
    thresholds the vector leg's top-1 score **as a cosine similarity**, and it
    does so exactly when the adapter declares ``distance_metric == "cosine"``.
    ``StorageAdapter`` DEFAULTS that attribute to ``"cosine"``, so a backend that
    ranks by an unnormalized dot product (a vector's self-score is then
    ``||v||^2``) or by any similarity on another scale, and simply never
    overrides the default, would have a plausible-looking number silently
    compared against a cosine-calibrated threshold — precisely the bluff
    ADR-0028 exists to stop.

    So the claim is verified, not trusted. A cosine has one property the common
    alternatives do not share: a vector's similarity to ITSELF is exactly 1.0,
    and every score lies in [-1, 1].

    (A backend returning a raw *distance*, where smaller means closer, already
    fails ``assert_vector_query_ranks``, which pins "higher score = more
    similar". This check closes the remaining gap: the score's SCALE.)
    """
    adapter = make()
    metric = getattr(adapter, "distance_metric", None)
    assert isinstance(metric, str) and metric, (
        "adapter must declare a non-empty distance_metric naming how vector_query scores"
    )
    text = "database migration rollback plan"
    target = _rec(_SCOPE_A, text, embedder=embedder)
    other = _rec(_SCOPE_A, "lunch menu tacos and salad", embedder=embedder)
    adapter.add(records=[target, other])

    # Query with the target's OWN embedding: a cosine must score it exactly 1.0.
    hits = adapter.vector_query(scope=_SCOPE_A, embedding=embedder.embed([text])[0], k=2).hits
    assert hits, "vector_query returned nothing for a stored record's own embedding"
    top = hits[0]
    assert top.record.id == target.id, "a record's own embedding must rank it first"

    if metric == "cosine":
        assert abs(top.score - 1.0) < 1e-6, (
            f"adapter declares distance_metric='cosine' but scored a record "
            f"against its OWN embedding as {top.score!r}, not 1.0. Either that "
            f"score is not a cosine similarity, or distance_metric is wrong. "
            f"recall(min_score=...) thresholds this number as a cosine (ADR-0028)."
        )
        for hit in hits:
            assert -1.0 - 1e-9 <= hit.score <= 1.0 + 1e-9, (
                f"distance_metric='cosine' but a score fell outside [-1, 1]: {hit.score!r}"
            )
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


def assert_effective_status_gate_on_forged_row(make: AdapterFactory, embedder: Embedder) -> None:
    """A stored row whose raw ``status`` column says ``active`` at QUARANTINED
    trust is the pair ``MemoryRecord`` makes unrepresentable at construction
    (``__post_init__`` rewrites ACTIVE→QUARANTINED at trust 0). It is still
    reachable in a store — an older Rekoll, a caller that mutated ``.status``
    after ``create()``, or a hand-edit — and EVERY read gate must classify it by
    its EFFECTIVE status (quarantined), never the raw column.

    An adapter that filters the raw column lets a quarantined memory surface
    through a ``status='active'`` query while hiding it from a
    ``status='quarantined'`` audit, and diverges its vector and lexical legs
    (#45). The forged state is built through the PUBLIC record API only (mutate
    ``.status`` post-construction), so this holds every backend to the rule
    without reaching into its storage."""
    adapter = make()
    clean = _rec(_SCOPE_A, "a genuinely active fact", embedder=embedder, source="test://clean")
    forged = _rec(
        _SCOPE_A, "forged active at quarantine trust", trust=TrustTier.QUARANTINED,
        embedder=embedder, source="test://forge",
    )
    assert forged.status is Status.QUARANTINED, "the model must force quarantine at trust 0"
    forged.status = Status.ACTIVE  # forge the divergent stored state on purpose
    adapter.add(records=[clean, forged])

    qvec = embedder.embed(["forged active at quarantine trust"])[0]
    active = adapter.vector_query(scope=_SCOPE_A, embedding=qvec, k=10, where={"status": "active"})
    assert forged.id not in {h.record.id for h in active}, \
        "a forged quarantine-level row surfaced through the vector status='active' filter"
    quarantined = adapter.vector_query(
        scope=_SCOPE_A, embedding=qvec, k=10, where={"status": "quarantined"}
    )
    assert forged.id in {h.record.id for h in quarantined}, \
        "the forged row is invisible to a vector status='quarantined' audit"

    # count() classifies by EFFECTIVE status: the forged row is quarantined, not active.
    assert adapter.count(scope=_SCOPE_A, status="active") == 1, "forged row miscounted as active"
    assert adapter.count(scope=_SCOPE_A, status="quarantined") == 1, \
        "forged row missing from the quarantined count"

    # The was-it-used credit skips the effectively-quarantined row (base-class rule).
    credited = adapter.bump_proof_count(scope=_SCOPE_A, ids=[forged.id, clean.id])
    assert credited == 1, "bump_proof_count credited a forged quarantine-level row"
    assert adapter.get(scope=_SCOPE_A, ids=[forged.id]).records[0].proof_count == 0

    # If the backend advertises lexical, the two legs must AGREE on the forged row.
    if adapter.supports(CAP_LEXICAL):
        text = "forged active quarantine trust"
        lex_active = adapter.lexical_query(scope=_SCOPE_A, text=text, k=10, where={"status": "active"})
        assert forged.id not in {h.record.id for h in lex_active}, \
            "lexical status='active' surfaced the forged row (vector/lexical divergence)"
        lex_quar = adapter.lexical_query(scope=_SCOPE_A, text=text, k=10, where={"status": "quarantined"})
        assert forged.id in {h.record.id for h in lex_quar}
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


def assert_active_directives_if_supported(make: AdapterFactory, embedder: Embedder) -> None:
    """The standing-directive channel read (ADR-0034), IF the adapter serves it.

    Optional exactly like ``lexical_query``: a backend that raises
    ``UnsupportedCapabilityError`` is skipped (``Memory`` degrades to rank-only).
    An adapter that DOES implement it must honor the contract ``Memory``'s
    always-on instruction channel depends on — get any of these wrong and a saved
    rule silently drops, duplicates, ranks the wrong five, or (worse) a
    non-active / below-floor / cross-scope directive leaks in as a *rule*:

      * only ACTIVE ``Kind.DIRECTIVE`` records at ``trust_tier >= min_trust``;
      * oldest-first, deterministic order (``created_at`` ASC, ``id`` ASC), so the
        rendered envelope stays byte-stable and the cap keeps the foundational
        rules;
      * capped at ``limit`` (and ``limit <= 0`` returns nothing);
      * scope-isolated.

    The forged/quarantine leak is separately caught by
    ``build_envelope``/``Memory._pinned_directives`` (defense in depth), but a
    wrong ORDER or a dropped/duplicated rule is a correctness bug those cannot
    see, so it is pinned here at the contract."""
    adapter = make()
    floor = int(TrustTier.TRUSTED_SOURCE)
    try:
        adapter.active_directives(scope=_SCOPE_A, limit=1, min_trust=floor)
    except UnsupportedCapabilityError:
        adapter.close()
        return

    def _dir(scope, text, *, sec, source, trust=TrustTier.OWNER, status=Status.ACTIVE):
        return _rec(
            scope, text, kind=Kind.DIRECTIVE, trust=trust, embedder=embedder,
            source=source, status=status,
            created_at=datetime(2026, 1, 1, 0, 0, sec, tzinfo=timezone.utc),
        )

    d0 = _dir(_SCOPE_A, "rule zero oldest", sec=0, source="t://0")
    d1 = _dir(_SCOPE_A, "rule one middle", sec=1, source="t://1")
    d2 = _dir(_SCOPE_A, "rule two newest", sec=2, source="t://2")
    below = _dir(_SCOPE_A, "below floor rule", sec=3, source="t://b", trust=TrustTier.UNVERIFIED)
    superseded = _dir(_SCOPE_A, "superseded rule", sec=4, source="t://s", status=Status.SUPERSEDED)
    fact = _rec(_SCOPE_A, "a plain raw fact not a rule", embedder=embedder, source="t://f")
    other = _dir(_SCOPE_B, "scope b rule", sec=0, source="t://ob")
    adapter.add(records=[d0, d1, d2, below, superseded, fact, other])

    got = adapter.active_directives(scope=_SCOPE_A, limit=10, min_trust=floor).records
    ids = [r.id for r in got]
    assert ids == [d0.id, d1.id, d2.id], (
        f"active_directives must return ACTIVE at-floor directives oldest-first; "
        f"got {[r.content for r in got]}"
    )
    assert below.id not in ids, "a below-floor directive surfaced in the standing channel"
    assert superseded.id not in ids, "a non-active directive surfaced in the standing channel"
    assert fact.id not in ids, "a raw fact surfaced in the directive-only channel"
    assert other.id not in ids, "a scope-B directive leaked into scope A"
    assert all(r.kind is Kind.DIRECTIVE for r in got), "non-directive kind in the channel"

    capped = adapter.active_directives(scope=_SCOPE_A, limit=2, min_trust=floor).records
    assert [r.id for r in capped] == [d0.id, d1.id], "limit/oldest-first cap not honored"
    assert adapter.active_directives(scope=_SCOPE_A, limit=0, min_trust=floor).records == (), \
        "limit<=0 must return no directives"

    # A higher floor (OWNER) filters out a TRUSTED_SOURCE directive.
    ts = _dir(_SCOPE_B, "trusted-source only rule", sec=1, source="t://ts",
              trust=TrustTier.TRUSTED_SOURCE)
    adapter.add(records=[ts])
    owner_only = adapter.active_directives(
        scope=_SCOPE_B, limit=10, min_trust=int(TrustTier.OWNER)
    ).records
    assert ts.id not in {r.id for r in owner_only}, "min_trust floor was not applied"
    adapter.close()


ALL_CHECKS = (
    assert_capabilities_honest,
    assert_kwargs_only,
    assert_add_and_get_roundtrip,
    assert_content_addressed_idempotent,
    assert_upsert_is_trust_monotonic,
    assert_proof_count_monotonic_on_upsert,
    assert_k_nonpositive_returns_empty,
    assert_add_strict_on_duplicate,
    assert_scope_isolation,
    assert_trust_roundtrip,
    assert_metadata_scalar_roundtrip,
    assert_embedder_identity,
    assert_vector_query_ranks,
    assert_distance_metric_honest,
    assert_where_honesty,
    assert_effective_status_gate_on_forged_row,
    assert_delete,
    assert_delete_scope_isolation,
    assert_lexical_if_supported,
    assert_active_directives_if_supported,
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
