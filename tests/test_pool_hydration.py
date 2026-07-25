"""The RRF fusion pool must not materialize vectors nobody reads (issue #43).

``hybrid_search`` fetches ``candidates`` (default ``6*k``) records from BOTH
legs, so a ``k=8`` recall reconstructs ~96 records to return 8 — and fusion
never touches ``.embedding``. Reconstructing each of those records used to
json-decode and box a 384-float vector that was then ranked away and discarded.

These tests pin the MECHANISM, not a wall-clock number: this repo has been
burned by runner-noise flakes, so timings live in the PR body and
``benchmarks/pool_hydration_bench.py``, never in an assert. The mechanism is
counted at ``_decode_embedding``, the one module-level helper every stored
vector passes through — both callers (``_scan`` and the record reconstruction
path) resolve it from module globals at call time, so a monkeypatch here cannot
be bypassed by a stale local binding.

The other half of the contract is that laziness stays INVISIBLE: reading
``.embedding`` on any record still yields exactly the same value it did before,
and a corrupt cell still raises exactly the same ``ValueError``.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile

import pytest

import rekoll.adapters.sqlite as sqlite_mod
from rekoll.memory import Memory
from rekoll.model import MemoryRecord, Provenance, Scope

DIM = 96  # wide enough to be a real vector, small enough to keep the suite fast
DOCS = [
    f"note {i}: postgres pooling latency widget alpha beta gamma topic {i % 7} shard {i}"
    for i in range(60)
]
QUERY = "postgres pooling widget topic 3"


@pytest.fixture()
def mem():
    m = Memory(path=os.path.join(tempfile.mkdtemp(), "m.db"), embedder="stub", reranker=None)
    for doc in DOCS:
        m.remember(doc)
    # Warm the ADR-0030 scan cache. A COLD scan legitimately decodes every
    # vector in the scope (that is where the cosine comes from); the waste this
    # lane is about is the per-hit reconstruction on top of it, so the counts
    # below are only meaningful once the scan is warm.
    m.recall(QUERY, k=8)
    yield m
    m.close()


class _Spy:
    """Count real ``_decode_embedding`` calls, attributed to the caller."""

    def __init__(self, monkeypatch):
        self.calls: list[str] = []
        real = sqlite_mod._decode_embedding

        def spy(raw):
            import traceback

            self.calls.append(traceback.extract_stack()[-2].name)
            return real(raw)

        monkeypatch.setattr(sqlite_mod, "_decode_embedding", spy)


def test_a_warm_recall_decodes_no_vector_for_the_fusion_pool(mem, monkeypatch):
    """RED before issue #43: 96 decodes to return 8 hits, none of them read."""
    spy = _Spy(monkeypatch)
    res = mem.recall(QUERY, k=8)
    assert res.ids(), "fixture produced no hits — the test would pass vacuously"
    assert spy.calls == [], (
        f"pool hydration decoded {len(spy.calls)} stored vector(s) that RRF "
        f"fusion never reads (callers: {sorted(set(spy.calls))})"
    )


def test_reading_one_hit_vector_decodes_exactly_that_one(mem, monkeypatch):
    """Laziness is per-record: touching one hit must not hydrate the pool."""
    spy = _Spy(monkeypatch)
    hits = mem.recall(QUERY, k=8).records()
    assert len(hits) > 1
    vec = hits[0].embedding
    assert len(spy.calls) == 1, f"reading one vector decoded {len(spy.calls)}"
    assert hits[0].embedding is vec, "a materialized vector must be cached, not re-decoded"
    assert len(spy.calls) == 1


def test_a_recalled_record_vector_is_exactly_what_the_store_holds(mem):
    """The value a caller sees through ``.embedding`` is unchanged (contract)."""
    hit = mem.recall(QUERY, k=1).records()[0]
    stored = mem.adapter.get(scope=mem.scope, ids=[hit.id]).records[0]
    assert hit.embedding == stored.embedding
    assert isinstance(hit.embedding, tuple)
    assert all(type(x) is float for x in hit.embedding)
    assert len(hit.embedding) == mem.embedder.dim


def test_a_recalled_record_writes_its_vector_verbatim(mem):
    """A recalled record whose vector was NEVER read must still write verbatim.

    The write path reads ``.embedding`` to build the stored cell, so a deferred
    vector has to materialize there — otherwise re-storing a record read back
    from the store would silently persist ``embedding=NULL``.
    """
    hit = mem.recall(QUERY, k=1).records()[0]  # never materialized in this process
    expected = mem.adapter.get(scope=mem.scope, ids=[hit.id]).records[0].embedding

    other = Memory(path=os.path.join(tempfile.mkdtemp(), "copy.db"), embedder="stub")
    try:
        other.adapter.add(records=[hit])
        round_tripped = other.adapter.get(scope=hit.scope, ids=[hit.id]).records[0]
        assert round_tripped.embedding == expected
    finally:
        other.close()


def test_a_recalled_record_still_pickles_with_its_vector(mem):
    """A deferred vector is a closure, and closures do not pickle. The wire
    format must stay exactly what it was: the vector materializes on the way
    out and travels under its public field name."""
    import pickle

    hit = mem.recall(QUERY, k=1).records()[0]  # never materialized
    expected = mem.adapter.get(scope=mem.scope, ids=[hit.id]).records[0].embedding

    blob = pickle.dumps(hit)
    assert pickle.loads(blob).embedding == expected
    # Pickled under the FIELD name, so pickles cross this change in both
    # directions (a pre-change reader sees the key it expects).
    assert b"embedding" in blob and b"_embedding" not in blob


@pytest.mark.parametrize("value", ['"garbage"', "[[1,2],[3,4]]", "not json", "[NaN,NaN]"])
def test_a_corrupt_cell_still_raises_the_same_valueerror_when_read(value):
    """Deferring the decode must not lose the error, only move where it lands.

    ``Memory.recall`` still fails on a corrupt cell at the scan (see
    tests/test_battle_robustness.py) because the cosine needs every vector in
    the scope. This pins the OTHER door — a read with no vector leg. Deferring
    the decode moves WHERE the error lands (from inside ``get()`` to the first
    ``.embedding`` access), so the assertion spans both points: what may never
    change is that the corrupt cell is reported as a clean ``ValueError`` and
    never silently yields garbage.
    """
    dbp = os.path.join(tempfile.mkdtemp(), "m.db")
    m = Memory(path=dbp, embedder="stub")
    rid = m.remember("the capital of france is paris").id
    m.close()

    con = sqlite3.connect(dbp)
    con.execute("UPDATE verbatim_records SET embedding=? WHERE id=?", (value, rid))
    con.commit()
    con.close()

    m2 = Memory(path=dbp, embedder="stub")
    with pytest.raises(ValueError, match="corrupt embedding cell"):
        record = m2.adapter.get(scope=m2.scope, ids=[rid]).records[0]
        record.embedding
    m2.close()


def test_an_eagerly_constructed_record_still_validates_at_construction():
    """Laziness is an ADAPTER-side optimisation; the public constructor must
    keep coercing and rejecting exactly when it did before."""
    kwargs = dict(
        scope=Scope(),
        kind="raw_fact",
        content="x",
        provenance=Provenance(source_uri="test://x"),
        trust_tier=4,
    )
    rec = MemoryRecord.create(embedding=[1, 2, 3], **kwargs)
    assert rec.embedding == (1.0, 2.0, 3.0)
    assert all(type(x) is float for x in rec.embedding)
    with pytest.raises(ValueError):
        MemoryRecord.create(embedding=["not a number"], **kwargs)
