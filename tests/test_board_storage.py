"""SQLite specifics of the live-project-board storage layer (ADR-0035).

The cross-adapter CONTRACT (gates, ordering, bounds, scope isolation,
set_status semantics) lives in rekoll/conformance.py and runs via
tests/test_sqlite_adapter.py. This file pins what is specific to the reference
implementation and to THIS repo's history:

 * the discriminating fail-before/pass-after proof that ``newest()`` — the
   pre-existing recency read — DOES return a forged (raw-active, trust-0) row,
   which is exactly WHY the gated ``recent_records`` exists;
 * the schema bootstrap being additive + idempotent (an existing store gains
   the two new indexes on reopen, no migration machinery);
 * the policy pins that keep restated numbers from drifting: the adapter
   contract's trust-floor defaults == ``firewall.BOARD_FLOOR`` (base.py cannot
   import firewall — firewall imports base), that every storage-side Tier-2
   floor READS ``BOARD_TRUST_FLOOR`` rather than restating it (proven
   behaviorally, since a signature pin cannot see an internal restatement),
   and the board's rules cap == ``DEFAULT_MAX_PINNED_DIRECTIVES`` (board.py
   must not import memory);
 * ``set_status`` rolling back a failed multi-table sweep, so a resolve that
   reported failure can never be committed later by an unrelated write;
 * scan-cache coherence after ``set_status`` (status IS cached for the vector
   gate, unlike ``proof_count``);
 * the INTENDED reopen semantics: a same-id re-upsert rewrites ``status``
   unconditionally, so re-remembering a resolved item reopens it (ADR-0035's
   honesty caveat, pinned so a future change is a conscious one).
"""

from __future__ import annotations

import inspect
from datetime import datetime, timezone

import pytest

from rekoll.adapters.base import (
    BOARD_METADATA_KEY,
    BOARD_TAG_MAJOR,
    BOARD_TAG_PENDING,
    BOARD_TRUST_FLOOR,
    StorageAdapter,
)
from rekoll.adapters.sqlite import _KIND_TABLE, SQLiteAdapter
from rekoll.board import DEFAULT_BOARD_RULES_LIMIT
from rekoll.embedding import StubEmbedder
from rekoll.firewall import BOARD_FLOOR, DIRECTIVE_FLOOR
from rekoll.memory import DEFAULT_MAX_PINNED_DIRECTIVES
from rekoll.model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier

SCOPE = Scope(tenant="acme", project="board", agent="bot")
_EMB = StubEmbedder()


def _rec(text, *, trust=TrustTier.OWNER, kind=Kind.RAW_FACT, source="test://doc", **kwargs):
    record = MemoryRecord.create(
        scope=SCOPE,
        kind=kind,
        content=text,
        provenance=Provenance(source_uri=source, adapter_name="test"),
        trust_tier=trust,
        **kwargs,
    )
    vector = _EMB.embed([text])[0]
    record.with_embedding(vector, name=_EMB.identity().name, dim=_EMB.dim)
    return record


def _forged(text, *, source):
    """The divergent stored state: raw ``status='active'`` at QUARANTINED trust,
    built through the public record API only (mutate after create)."""
    record = _rec(text, trust=TrustTier.QUARANTINED, source=source)
    assert record.status is Status.QUARANTINED  # the model normalized it
    record.status = Status.ACTIVE  # forge on purpose
    return record


# --- the discriminating proof: why recent_records exists --------------------

def test_newest_returns_the_forged_row_and_recent_records_gates_it():
    """FAIL-BEFORE / PASS-AFTER, in one test: the pre-existing ``newest()``
    (unchanged by this PR — ``health()``/``reindex()`` depend on it seeing every
    row) DOES return a forged active-at-trust-0 row. That is the danger a
    trust-labeled, every-session activity feed cannot inherit, and why the board
    got its own gated read instead of reusing ``newest()``."""
    adapter = SQLiteAdapter(":memory:")
    clean = _rec("a genuinely active fact", source="t://clean")
    forged = _forged("forged active at quarantine trust", source="t://forge")
    adapter.add(records=[clean, forged])

    newest_ids = {r.id for r in adapter.newest(scope=SCOPE, n=10)}
    assert forged.id in newest_ids, (
        "PRECONDITION CHANGED: newest() no longer returns the forged row — "
        "if newest() grew a gate, re-examine whether recent_records still "
        "needs to exist and update ADR-0035"
    )

    # The board's feed excludes it even with the trust floor dropped to 0,
    # so the exclusion is the EFFECTIVE-STATUS gate, not the trust floor.
    recent_ids = {
        r.id
        for r in adapter.recent_records(
            scope=SCOPE, limit=10, min_trust=int(TrustTier.QUARANTINED)
        )
    }
    assert clean.id in recent_ids
    assert forged.id not in recent_ids
    adapter.close()


# --- schema bootstrap: additive + idempotent --------------------------------

def test_board_indexes_created_and_reopen_is_idempotent(tmp_path):
    """The two new indexes exist after bootstrap, and an EXISTING store gains
    them on reopen with no migration machinery (CREATE INDEX IF NOT EXISTS)."""
    path = str(tmp_path / "store.db")
    first = SQLiteAdapter(path)
    first.add(records=[_rec("survives a reopen", source="t://reopen")])
    names = {
        row["name"]
        for row in first._conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
        ).fetchall()
    }
    for table in _KIND_TABLE.values():
        assert f"idx_{table}_scope_created" in names
    assert "idx_record_metadata_key" in names
    first.close()

    # Reopen: bootstrap runs again over the existing schema — must not raise,
    # and the data written before is intact.
    second = SQLiteAdapter(path)
    assert second.count(scope=SCOPE) == 1
    second.close()


# --- policy pins: restated numbers may never drift --------------------------

def test_adapter_trust_floor_defaults_equal_board_floor():
    """base.py spells the Tier-2 default as ``int(TrustTier.TRUSTED_SOURCE)``
    because it cannot import ``firewall.BOARD_FLOOR`` (firewall imports
    adapters.base — a cycle). This is the pin that makes that restatement safe:
    if either side moves, this fails and the mover must reconcile both."""
    for cls in (StorageAdapter, SQLiteAdapter):
        entries_default = inspect.signature(cls.board_entries).parameters["min_trust"].default
        assert entries_default == int(BOARD_FLOOR)
        recent_default = inspect.signature(cls.recent_records).parameters["min_trust"].default
        assert recent_default == int(TrustTier.UNVERIFIED)
        snap = inspect.signature(cls.board_snapshot).parameters
        assert snap["min_trust"].default == int(TrustTier.UNVERIFIED)
    # The board floor IS the directive floor (deliberately — see firewall.py);
    # if that policy ever diverges, the ADR-0035 reasoning must be revisited.
    assert int(BOARD_FLOOR) == int(DIRECTIVE_FLOOR)


def test_every_tier2_floor_reads_the_one_shared_constant():
    """The Tier-2 floor was restated in THREE places (the contract defaults,
    the reference adapter's ``board_snapshot`` internals, and
    ``firewall.BOARD_FLOOR``). A signature-only pin cannot see the INTERNAL
    one, so a sideways edit there would drift silently in both directions.

    Now every storage-side floor reads ``BOARD_TRUST_FLOOR``. This proves it
    BEHAVIORALLY for the internal restatement: move the shared constant and the
    curated leg must move with it. A hard-coded internal floor fails here.
    """
    assert BOARD_TRUST_FLOOR == int(BOARD_FLOOR)  # the one remaining restatement

    adapter = SQLiteAdapter(":memory:")
    low_major = _rec(
        "a below-floor tagged major", trust=TrustTier.UNVERIFIED, source="t://lm",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_MAJOR},
    )
    adapter.add(records=[low_major])
    # At the real floor the tag alone never curates it (the landed policy)...
    assert adapter.board_snapshot(scope=SCOPE, recent_limit=10, major_limit=10).majors == ()

    # ...and with the SHARED constant lowered, the same call must curate it —
    # which can only happen if board_snapshot reads that constant, not a literal.
    import rekoll.adapters.sqlite as sqlite_module

    original = sqlite_module.BOARD_TRUST_FLOOR
    try:
        sqlite_module.BOARD_TRUST_FLOOR = int(TrustTier.UNVERIFIED)
        snap = adapter.board_snapshot(scope=SCOPE, recent_limit=10, major_limit=10)
        assert [r.id for r in snap.majors] == [low_major.id], (
            "board_snapshot's Tier-2 floor is restated internally instead of "
            "reading BOARD_TRUST_FLOOR — a sideways drift this pin must catch"
        )
        assert snap.pending_open == 0  # still no pending item; only the floor moved
    finally:
        sqlite_module.BOARD_TRUST_FLOOR = original
    adapter.close()


def test_set_status_rolls_back_a_failed_sweep_and_leaves_no_open_transaction():
    """``set_status`` sweeps several kind tables then commits. Without a
    rollback, a failure AFTER a matching UPDATE leaves that update sitting in an
    open transaction: the call reports failure (it raised), but the NEXT
    unrelated write's commit silently lands the resolve anyway — and the
    scan-cache patch that write depends on never ran.

    Injects a failure on the SECOND table after the FIRST one matched, then
    proves the row is unchanged, STAYS unchanged across a later committed
    write, and that no transaction was left open.
    """
    import sqlite3

    adapter = SQLiteAdapter(":memory:")
    target = _rec("an active board item", source="t://tgt")
    adapter.add(records=[target])
    table = _KIND_TABLE[Kind.RAW_FACT]  # the FIRST table the sweep visits

    def status_of(record_id):
        row = real.execute(
            f"SELECT status FROM {table} WHERE id=?", (record_id,)
        ).fetchone()
        return row[0]

    class _BoomAfterFirstMatch:
        """Delegates to the real connection but detonates on ``observations`` —
        the table the sweep reaches AFTER updating ``verbatim_records``."""

        def __init__(self, conn):
            self._conn = conn

        def __getattr__(self, name):
            return getattr(self._conn, name)

        def execute(self, sql, *args):
            if sql.lstrip().startswith("UPDATE ") and " observations " in sql:
                raise sqlite3.OperationalError("injected mid-sweep failure")
            return self._conn.execute(sql, *args)

    real = adapter._conn
    assert status_of(target.id) == Status.ACTIVE.value

    adapter._conn = _BoomAfterFirstMatch(real)
    try:
        with pytest.raises(sqlite3.OperationalError):
            adapter.set_status(
                scope=SCOPE, record_id=target.id, status=Status.SUPERSEDED.value
            )
    finally:
        adapter._conn = real

    assert not real.in_transaction, "a failed set_status left a transaction dangling"
    assert status_of(target.id) == Status.ACTIVE.value, "the failed resolve took effect"

    # The real hazard: a LATER unrelated write must not commit the abandoned
    # UPDATE on its way past.
    adapter.add(records=[_rec("an unrelated later write", source="t://other")])
    assert status_of(target.id) == Status.ACTIVE.value, (
        "a failed resolve was silently committed by the next unrelated write"
    )
    adapter.close()


def test_board_rules_cap_equals_pinned_directives_cap():
    """board.py restates 5 because it must never import memory; the board's
    rules and recall's pinned directives are the same records by construction,
    so the two caps must be the same number."""
    assert DEFAULT_BOARD_RULES_LIMIT == DEFAULT_MAX_PINNED_DIRECTIVES


def test_snapshot_tier2_floor_is_pinned_at_board_floor():
    """``board_snapshot``'s ``min_trust`` gates the Tier-1 feed ONLY; the
    curated leg + pending count always apply the BOARD_FLOOR policy. Passing
    min_trust=0 must therefore open the FEED to low-trust rows while the
    curated leg stays floored."""
    adapter = SQLiteAdapter(":memory:")
    low_major = _rec(
        "low-trust tagged major", trust=TrustTier.UNVERIFIED, source="t://lm",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_MAJOR},
    )
    low_pending = _rec(
        "low-trust tagged pending", trust=TrustTier.UNVERIFIED, source="t://lp",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_PENDING},
    )
    adapter.add(records=[low_major, low_pending])
    snap = adapter.board_snapshot(
        scope=SCOPE, recent_limit=10, major_limit=10,
        min_trust=int(TrustTier.QUARANTINED),
    )
    assert {r.id for r in snap.recent} == {low_major.id, low_pending.id}
    assert snap.majors == ()  # tag alone never curates a below-floor row
    assert snap.pending_open == 0
    adapter.close()


# --- scan-cache coherence after set_status ----------------------------------

def test_set_status_keeps_the_vector_scan_cache_coherent():
    """``status`` IS held by the vector scan cache (its gate reads it), unlike
    ``proof_count``. After a resolve, a cached ``where={'status':'active'}``
    vector query must not surface the row, and a ``'superseded'`` audit must —
    WITHOUT an intervening write to invalidate the cache."""
    adapter = SQLiteAdapter(":memory:")
    record = _rec("resolve me and stay coherent", source="t://cache")
    adapter.add(records=[record])
    qvec = _EMB.embed(["resolve me and stay coherent"])[0]

    # Warm the scan cache, then transition WITHOUT any other write.
    hits = adapter.vector_query(scope=SCOPE, embedding=qvec, k=5, where={"status": "active"})
    assert record.id in {h.record.id for h in hits}
    assert adapter.set_status(
        scope=SCOPE, record_id=record.id, status=Status.SUPERSEDED.value
    ) is True

    active = adapter.vector_query(scope=SCOPE, embedding=qvec, k=5, where={"status": "active"})
    assert record.id not in {h.record.id for h in active}, (
        "stale scan cache: a resolved row still surfaces as status='active'"
    )
    superseded = adapter.vector_query(
        scope=SCOPE, embedding=qvec, k=5, where={"status": "superseded"}
    )
    assert record.id in {h.record.id for h in superseded}, (
        "stale scan cache: a resolved row is invisible to a status='superseded' audit"
    )
    adapter.close()


# --- honesty caveat pinned: same-id re-upsert reopens ------------------------

def test_same_id_reupsert_reopens_a_resolved_item():
    """INTENDED semantics (ADR-0035 honesty caveat): the content-addressed
    upsert rewrites ``status`` unconditionally, so re-remembering the SAME
    content from the SAME source reopens a resolved item (and rewrites
    created_at, which can reorder the board). Pinned so a future change to
    upsert is made knowing it changes the board's reopen story."""
    adapter = SQLiteAdapter(":memory:")
    item = _rec(
        "ship the docs pass", source="t://same",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_PENDING},
        created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
    )
    adapter.upsert(records=[item])
    assert adapter.set_status(
        scope=SCOPE, record_id=item.id, status=Status.SUPERSEDED.value
    ) is True
    assert adapter.board_snapshot(scope=SCOPE).pending_open == 0

    again = _rec(
        "ship the docs pass", source="t://same",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_PENDING},
        created_at=datetime(2026, 2, 2, tzinfo=timezone.utc),
    )
    assert again.id == item.id  # same scope+source+kind+content ⇒ same address
    adapter.upsert(records=[again])
    snap = adapter.board_snapshot(scope=SCOPE)
    assert snap.pending_open == 1, "same-id re-upsert must REOPEN the resolved item"
    assert [r.id for r in snap.recent] == [item.id]
    adapter.close()


# --- limit=0 disables one leg without touching the others --------------------

def test_snapshot_zero_limits_disable_legs_independently():
    adapter = SQLiteAdapter(":memory:")
    major = _rec(
        "a tagged major", source="t://m",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_MAJOR},
    )
    pending = _rec(
        "a tagged pending", source="t://p",
        metadata={BOARD_METADATA_KEY: BOARD_TAG_PENDING},
    )
    adapter.add(records=[major, pending])
    no_recent = adapter.board_snapshot(scope=SCOPE, recent_limit=0, major_limit=10)
    assert no_recent.recent == ()
    assert {r.id for r in no_recent.majors} == {major.id, pending.id}
    assert no_recent.pending_open == 1  # the COUNT is not a leg limit's hostage
    no_major = adapter.board_snapshot(scope=SCOPE, recent_limit=10, major_limit=0)
    assert no_major.majors == ()
    assert {r.id for r in no_major.recent} == {major.id, pending.id}
    assert no_major.pending_open == 1
    adapter.close()
