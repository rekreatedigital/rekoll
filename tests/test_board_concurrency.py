"""Board reads under REAL concurrency (ADR-0035, A-lane §A6).

Two actual SQLite connections (two ``SQLiteAdapter`` instances) on ONE
temp-file WAL store — the exact multi-session shape the board exists for. Three
properties, each of which has a plausible wrong implementation:

 * FOREIGN-COMMIT VISIBILITY — a second connection's committed write appears in
   the FIRST connection's next ``board_snapshot``. The wrong implementation
   serves board rows from the per-connection vector scan cache (which is only
   invalidated by ``PRAGMA data_version`` on the vector path); the board must
   read the database, so the test warms connection 1's scan cache first.
 * UNTORN SNAPSHOT — all three snapshot legs come from ONE read transaction. A
   foreign commit landing BETWEEN the legs must not produce tiers that
   contradict each other (entries the count doesn't know, or vice versa).
 * RACING RESOLVES — two threads, two connections, one id: EXACTLY ONE
   ``set_status`` reports the transition, both return safely (the
   ``bump_proof_count`` in-SQL-gate pattern; no read-modify-write window).
 * CROSS-CONNECTION BYTE-DETERMINISM — the payload is a pure function of
   STORED rows, so two different sessions (connections) over one store must
   ``json.dumps`` byte-identically; that equality is what makes "byte-compare
   payloads to detect change" (ADR-0035 §9) work ACROSS sessions, not just
   within one.
"""

from __future__ import annotations

import json
import threading

from rekoll.adapters.base import BOARD_METADATA_KEY, BOARD_TAG_MAJOR, BOARD_TAG_PENDING
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.board import build_board_payload
from rekoll.embedding import StubEmbedder
from rekoll.model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier

SCOPE = Scope(tenant="acme", project="board", agent="bot")
_EMB = StubEmbedder()


def _rec(text, *, source, tag=None, trust=TrustTier.OWNER):
    record = MemoryRecord.create(
        scope=SCOPE,
        kind=Kind.RAW_FACT,
        content=text,
        provenance=Provenance(source_uri=source, adapter_name="test"),
        trust_tier=trust,
        metadata={BOARD_METADATA_KEY: tag} if tag else {},
    )
    vector = _EMB.embed([text])[0]
    record.with_embedding(vector, name=_EMB.identity().name, dim=_EMB.dim)
    return record


def test_board_snapshot_sees_a_foreign_commit(tmp_path):
    path = str(tmp_path / "shared.db")
    first = SQLiteAdapter(path)
    second = SQLiteAdapter(path)

    seed = _rec("seeded before anyone reads", source="t://seed")
    first.add(records=[seed])
    # Warm connection 1's vector scan cache so a served-from-cache board
    # implementation would have something stale to serve.
    qvec = _EMB.embed(["seeded before anyone reads"])[0]
    first.vector_query(scope=SCOPE, embedding=qvec, k=3)
    assert [r.id for r in first.board_snapshot(scope=SCOPE).recent] == [seed.id]

    foreign = _rec("committed by the second session", source="t://foreign",
                   tag=BOARD_TAG_PENDING)
    second.add(records=[foreign])

    snap = first.board_snapshot(scope=SCOPE)
    ids = [r.id for r in snap.recent]
    assert foreign.id in ids, (
        "a foreign connection's committed write is invisible to the next "
        "board_snapshot — board reads must observe the database, never a "
        "per-connection cache"
    )
    assert snap.pending_open == 1
    first.close()
    second.close()


def test_board_snapshot_is_untorn_when_a_foreign_commit_lands_mid_read(tmp_path):
    """Deterministically drive a foreign commit INTO the middle of a snapshot:
    the majors/recent legs are read before ``_pending_open_count`` runs, so a
    hooked adapter commits a new pending row (via the second connection) right
    before the count. One read transaction ⇒ the count must NOT include a row
    the legs never saw; the NEXT snapshot then sees it everywhere."""
    path = str(tmp_path / "shared.db")
    second = SQLiteAdapter(path)
    landed: list[MemoryRecord] = []

    class MidSnapshotWriter(SQLiteAdapter):
        def _pending_open_count(self, skey, min_trust):
            if not landed:  # fire exactly once, mid-snapshot
                row = _rec("landed mid snapshot", source="t://mid", tag=BOARD_TAG_PENDING)
                second.add(records=[row])
                landed.append(row)
            return super()._pending_open_count(skey, min_trust)

    first = MidSnapshotWriter(path)
    base = _rec("already open pending", source="t://base", tag=BOARD_TAG_PENDING)
    first.add(records=[base])

    torn_candidate = first.board_snapshot(scope=SCOPE)
    assert landed, "the hook must have fired mid-snapshot"
    assert torn_candidate.pending_open == 1, (
        "torn snapshot: pending_open counted a row the majors/recent legs "
        "never saw — the three reads must share one transaction"
    )
    assert landed[0].id not in {r.id for r in torn_candidate.majors}

    settled = first.board_snapshot(scope=SCOPE)
    assert settled.pending_open == 2
    assert landed[0].id in {r.id for r in settled.majors}
    assert landed[0].id in {r.id for r in settled.recent}
    first.close()
    second.close()


def test_racing_set_status_transitions_exactly_once(tmp_path):
    """The bump_proof_count atomicity proof, applied to resolve: the
    effective-status gate lives IN the UPDATE, so of two racing sessions
    exactly one observes ACTIVE→SUPERSEDED and the other reports False —
    never two winners, never an exception, never a resurrected row."""
    path = str(tmp_path / "shared.db")
    setup = SQLiteAdapter(path)
    item = _rec("resolve me exactly once", source="t://race", tag=BOARD_TAG_PENDING)
    setup.add(records=[item])
    setup.close()

    barrier = threading.Barrier(2)
    results: list[object] = [None, None]

    def resolve(slot: int) -> None:
        # Each racer owns its OWN connection, created in its own thread (a
        # sqlite3 connection is thread-bound) — the real two-sessions shape.
        adapter = SQLiteAdapter(path)
        try:
            barrier.wait()
            results[slot] = adapter.set_status(
                scope=SCOPE, record_id=item.id, status=Status.SUPERSEDED.value
            )
        except Exception as exc:  # both calls must return SAFELY
            results[slot] = exc
        finally:
            adapter.close()

    threads = [threading.Thread(target=resolve, args=(slot,)) for slot in (0, 1)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads), "a racing set_status hung"

    assert not any(isinstance(r, Exception) for r in results), (
        f"a racing set_status raised instead of returning: {results!r}"
    )
    assert sorted(results) == [False, True], (
        f"exactly one racer must observe the transition, got {results!r}"
    )

    check = SQLiteAdapter(path)
    stored = check.get(scope=SCOPE, ids=[item.id]).records[0]
    assert stored.status is Status.SUPERSEDED
    assert check.board_snapshot(scope=SCOPE).pending_open == 0
    check.close()


def test_payload_is_byte_identical_across_connections(tmp_path):
    """Two SESSIONS (separate connections) over one store must render the SAME
    bytes: the payload is a pure function of stored rows, never of connection
    state (caches, cursors, open transactions). This is what lets any session
    byte-compare payloads to detect change regardless of WHICH session produced
    the baseline (ADR-0035 §9's authoritative change check)."""
    path = str(tmp_path / "shared.db")
    writer = SQLiteAdapter(path)
    rule = MemoryRecord.create(
        scope=SCOPE, kind=Kind.DIRECTIVE, content="always explain simply",
        provenance=Provenance(source_uri="t://rule", adapter_name="test"),
        trust_tier=TrustTier.OWNER,
    )
    writer.add(records=[
        rule,
        _rec("major milestone", source="t://major", tag=BOARD_TAG_MAJOR),
        _rec("open question", source="t://pending", tag=BOARD_TAG_PENDING),
        _rec("untrusted note", source="t://low", trust=TrustTier.UNVERIFIED),
    ])
    first = SQLiteAdapter(path)
    second = SQLiteAdapter(path)
    # Skew the two connections' incidental state: warm ONLY first's scan cache.
    first.vector_query(scope=SCOPE, embedding=_EMB.embed(["major milestone"])[0], k=2)
    payload_one = json.dumps(build_board_payload(first, SCOPE))
    payload_two = json.dumps(build_board_payload(second, SCOPE))
    assert payload_one == payload_two, (
        "two connections rendered different board bytes for identical stored "
        "rows — cross-session byte-comparison would false-positive"
    )
    for adapter in (writer, first, second):
        adapter.close()
