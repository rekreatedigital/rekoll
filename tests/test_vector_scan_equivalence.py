"""ADR-0030: the cached/vectorized vector scan must be OBSERVATIONALLY IDENTICAL
to the brute-force scan it replaced.

``_legacy_vector_query`` below is a verbatim transcription of the pre-ADR-0030
``SQLiteAdapter.vector_query`` (main @ fd14e63): ``SELECT *`` per kind table,
``json.loads`` every embedding, ``cosine()`` in pure Python, stable sort, slice
to k. Every test here runs BOTH paths against the same store and diffs them.

The bar is: identical ids in identical order, and scores equal within float
tolerance. The pure-Python path is in fact bit-exact (it accumulates the dot and
the norms in the same order ``embedding.cosine`` does); the numpy path may differ
in the last ulp because BLAS sums pairwise. Both are asserted.
"""

from __future__ import annotations

import json
import sqlite3
import sys

import pytest

from rekoll.adapters.sqlite import _KIND_TABLE, SQLiteAdapter
from rekoll.embedding import cosine
from rekoll.model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier

BACKENDS = ["python", "numpy"]
SCOPE = Scope(tenant="t", project="p", agent="a")
OTHER_SCOPE = Scope(tenant="t", project="p", agent="other")
KINDS = [Kind.RAW_FACT, Kind.OBSERVATION, Kind.DIRECTIVE, Kind.EPISODE]


# --- the pre-change implementation, kept verbatim as the oracle ---------------
def _legacy_vector_query(adapter, *, scope, embedding, k=10, kind=None, where=None):
    allowed = {"status", "min_trust"}
    if where:
        bad = set(where) - allowed
        if bad:
            raise ValueError(f"unsupported where keys {sorted(bad)}; allowed: {sorted(allowed)}")
    query_vec = [float(x) for x in embedding]
    qdim = len(query_vec)
    skey = scope.key()
    tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
    status_filter = where.get("status") if where else None
    min_trust = where.get("min_trust") if where else None
    scored: list[tuple[float, sqlite3.Row]] = []
    for table in tables:
        sql = f"SELECT * FROM {table} WHERE scope_key=? AND embedding IS NOT NULL"
        params: list[object] = [skey]
        if status_filter is not None:
            sql += " AND status=?"
            params.append(status_filter)
        if min_trust is not None:
            sql += " AND trust_tier>=?"
            params.append(int(min_trust))
        for row in adapter._conn.execute(sql, params).fetchall():
            stored = json.loads(row["embedding"])
            if len(stored) != qdim:
                continue
            scored.append((cosine(query_vec, stored), row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(score, row["id"]) for score, row in scored[: max(0, k)]]


def _assert_same(adapter, *, exact: bool, **kwargs):
    """Diff new vs legacy for one query. Returns the new-path hits."""
    expected = _legacy_vector_query(adapter, **kwargs)
    actual = adapter.vector_query(**kwargs)
    got = [(h.score, h.record.id) for h in actual.hits]
    assert [i for _, i in got] == [i for _, i in expected], "top-k ids or their ORDER diverged"
    for (gs, gid), (es, eid) in zip(got, expected):
        assert gid == eid
        if exact:
            assert gs == es, f"pure-Python path must be bit-exact: {gs!r} != {es!r}"
        else:
            assert gs == pytest.approx(es, abs=1e-9, rel=1e-9)
    return actual.hits


# --- fixtures ----------------------------------------------------------------
_M64 = (1 << 64) - 1


def _vec(seed: int, dim: int = 16) -> tuple[float, ...]:
    """Deterministic pseudo-random vector: non-normalized, mixed signs, no numpy.

    Uses a splitmix64 mixer rather than an arithmetic ramp — a ramp makes every
    vector nearly collinear and lets distinct seeds collide exactly, which turns
    unrelated tests into accidental tie tests.
    """
    x = ((seed + 1) * 0x9E3779B97F4A7C15) & _M64
    out = []
    for _ in range(dim):
        x = ((x ^ (x >> 30)) * 0xBF58476D1CE4E5B9) & _M64
        x = ((x ^ (x >> 27)) * 0x94D049BB133111EB) & _M64
        x ^= x >> 31
        out.append((x % 2_000_003) / 1_000_001.5 - 1.0)
    return tuple(out)


def _rec(i: int, *, dim=16, kind=None, trust=TrustTier.UNVERIFIED, status=None, vec=None, content=None):
    r = MemoryRecord.create(
        scope=SCOPE,
        kind=kind or KINDS[i % 4],
        content=content or f"record {i} about topic {i % 7}",
        provenance=Provenance(source_uri=f"s://x/{i}", adapter_name="b", adapter_version="1"),
        trust_tier=trust,
        embedding=_vec(i, dim) if vec is None else vec,
        embedder_name="stub",
        embedder_dim=dim,
    )
    if status is not None:
        r.status = status
    return r


def _adapter(backend, records=()):
    a = SQLiteAdapter(":memory:", vector_backend=backend)
    if records:
        a.add(records=list(records))
    return a


@pytest.fixture(params=BACKENDS)
def backend(request):
    if request.param == "numpy":
        pytest.importorskip("numpy")
    return request.param


@pytest.fixture
def exact(backend):
    return backend == "python"


# --- the corpus of stores ----------------------------------------------------
def test_empty_store(backend, exact):
    a = _adapter(backend)
    assert _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1), k=5) == ()
    a.close()


def test_single_row(backend, exact):
    a = _adapter(backend, [_rec(0)])
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(0), k=5)
    a.close()


@pytest.mark.parametrize("k", [1, 3, 10, 50, 500])
def test_many_rows_all_k(backend, exact, k):
    a = _adapter(backend, [_rec(i) for i in range(120)])
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(7), k=k)
    a.close()


def test_k_zero_and_negative_return_nothing(backend, exact):
    a = _adapter(backend, [_rec(i) for i in range(10)])
    for k in (0, -1, -100):
        assert a.vector_query(scope=SCOPE, embedding=_vec(1), k=k).hits == ()
        assert _legacy_vector_query(a, scope=SCOPE, embedding=_vec(1), k=k) == []
    a.close()


def test_exact_ties_preserve_scan_order(backend, exact):
    """Duplicate vectors across kinds produce EXACTLY equal scores. The tie must
    break the same way it did before (table order, then rowid) — this is the one
    place a vectorized rewrite silently reorders results."""
    shared = _vec(42)
    records = [
        _rec(i, kind=KINDS[i % 4], vec=shared, content=f"tied record {i}") for i in range(12)
    ]
    a = _adapter(backend, records)
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(99), k=12)
    scores = [h.score for h in hits]
    assert len(set(scores)) == 1, "fixture is not actually producing ties"
    a.close()


def test_mixed_dims_after_model_swap_are_skipped(backend, exact):
    """A scope holding vectors of two widths (embedder swapped mid-flight) must
    score only the comparable ones — not crash, not truncate, not zip-pad."""
    a = _adapter(backend, [_rec(i, dim=16) for i in range(20)])
    a.add(records=[_rec(100 + i, dim=32) for i in range(15)])
    for dim in (16, 32):
        hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(3, dim), k=10)
        assert all(len(h.record.embedding) == dim for h in hits)
    # a width nobody stored matches nothing
    assert a.vector_query(scope=SCOPE, embedding=_vec(3, 7), k=10).hits == ()
    a.close()


def test_only_foreign_dim_rows_present(backend, exact):
    a = _adapter(backend, [_rec(i, dim=32) for i in range(5)])
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1, 16), k=5)
    a.close()


def test_zero_vectors_score_zero_not_nan(backend, exact):
    zero = tuple(0.0 for _ in range(16))
    a = _adapter(backend, [_rec(0, vec=zero), _rec(1), _rec(2)])
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1), k=5)
    assert all(h.score == h.score for h in hits)  # no NaN
    # a zero QUERY vector: every score is 0.0, nothing blows up
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=zero, k=5)
    assert [h.score for h in hits] == [0.0, 0.0, 0.0]
    a.close()


def test_deleted_rows_disappear(backend, exact):
    records = [_rec(i) for i in range(30)]
    a = _adapter(backend, records)
    a.vector_query(scope=SCOPE, embedding=_vec(1), k=5)  # warm the cache
    removed = a.delete(scope=SCOPE, ids=[r.id for r in records[:10]])
    assert removed == 10
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1), k=30)
    gone = {r.id for r in records[:10]}
    assert not (gone & {h.record.id for h in hits})
    a.close()


def test_records_with_no_embedding_are_ignored(backend, exact):
    a = _adapter(backend, [_rec(i) for i in range(5)])
    naked = MemoryRecord.create(
        scope=SCOPE, kind=Kind.RAW_FACT, content="no vector here",
        provenance=Provenance(source_uri="s://n", adapter_name="b", adapter_version="1"),
        trust_tier=TrustTier.UNVERIFIED,
    )
    a.add(records=[naked])
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1), k=10)
    assert naked.id not in {h.record.id for h in hits}
    a.close()


# --- filters -----------------------------------------------------------------
@pytest.mark.parametrize(
    "where",
    [
        None,
        {},
        {"status": "active"},
        {"status": "quarantined"},
        {"status": "superseded"},
        {"min_trust": 0},
        {"min_trust": 2},
        {"min_trust": 4},
        {"status": "active", "min_trust": 2},
    ],
)
def test_where_filters_match_legacy(backend, exact, where):
    records = [_rec(i, trust=TrustTier(i % 5)) for i in range(60)]
    records[3].status = Status.SUPERSEDED
    records[9].status = Status.SUPERSEDED
    a = _adapter(backend, records)
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(5), k=20, where=where)
    a.close()


@pytest.mark.parametrize("kind", KINDS)
def test_kind_table_restriction_matches_legacy(backend, exact, kind):
    a = _adapter(backend, [_rec(i) for i in range(40)])
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(2), k=10, kind=kind)
    assert all(h.record.kind is kind for h in hits)
    a.close()


def test_kind_and_where_together(backend, exact):
    records = [_rec(i, trust=TrustTier(1 + i % 4)) for i in range(40)]
    a = _adapter(backend, records)
    _assert_same(
        a, exact=exact, scope=SCOPE, embedding=_vec(2), k=10,
        kind=Kind.OBSERVATION, where={"min_trust": 3, "status": "active"},
    )
    a.close()


def test_unsupported_where_key_still_raises(backend):
    a = _adapter(backend, [_rec(0)])
    with pytest.raises(ValueError, match="unsupported where keys"):
        a.vector_query(scope=SCOPE, embedding=_vec(0), where={"bogus": 1})
    # ...even when k<=0 short-circuits the scan
    with pytest.raises(ValueError, match="unsupported where keys"):
        a.vector_query(scope=SCOPE, embedding=_vec(0), k=0, where={"bogus": 1})
    a.close()


def test_quarantined_rows_never_leak_across_the_filter(backend, exact):
    """Trust 0 forces status=QUARANTINED at construction. A status='active'
    query must not surface them — the property the firewall relies on."""
    records = [_rec(i, trust=TrustTier.UNVERIFIED) for i in range(10)]
    poisoned = [_rec(100 + i, trust=TrustTier.QUARANTINED) for i in range(10)]
    assert all(r.status is Status.QUARANTINED for r in poisoned)
    a = _adapter(backend, records + poisoned)
    hits = _assert_same(
        a, exact=exact, scope=SCOPE, embedding=_vec(101), k=20, where={"status": "active"}
    )
    assert {h.record.id for h in hits}.isdisjoint({r.id for r in poisoned})
    assert len(hits) == 10
    a.close()


def test_scope_isolation_holds(backend, exact):
    a = _adapter(backend, [_rec(i) for i in range(10)])
    foreign = [
        MemoryRecord.create(
            scope=OTHER_SCOPE, kind=Kind.RAW_FACT, content=f"foreign {i}",
            provenance=Provenance(source_uri=f"s://f/{i}", adapter_name="b", adapter_version="1"),
            trust_tier=TrustTier.OWNER, embedding=_vec(i), embedder_name="stub", embedder_dim=16,
        )
        for i in range(10)
    ]
    a.add(records=foreign)
    a.vector_query(scope=SCOPE, embedding=_vec(1), k=5)  # warm both caches
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1), k=20)
    assert {h.record.id for h in hits}.isdisjoint({r.id for r in foreign})
    _assert_same(a, exact=exact, scope=OTHER_SCOPE, embedding=_vec(1), k=20)
    a.close()


# --- cache coherence under mutation ------------------------------------------
def test_write_query_write_query(backend, exact):
    """The cache must never serve a vector the store no longer has."""
    a = _adapter(backend, [_rec(i) for i in range(5)])
    first = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(50), k=10)
    assert len(first) == 5

    a.add(records=[_rec(50, content="the newly added target")])
    second = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(50), k=10)
    assert len(second) == 6
    assert second[0].record.content == "the newly added target"

    a.delete(scope=SCOPE, ids=[second[0].record.id])
    third = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(50), k=10)
    assert len(third) == 5
    a.close()


def test_upsert_re_embed_invalidates_cache(backend, exact):
    """Same content, new vector: the cached matrix must be rebuilt, not reused."""
    a = _adapter(backend, [_rec(i) for i in range(5)])
    target = _rec(0)
    a.vector_query(scope=SCOPE, embedding=_vec(0), k=5)  # warm

    reembedded = _rec(0, vec=_vec(999))
    assert reembedded.id == target.id and reembedded.content == target.content
    a.upsert(records=[reembedded])

    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(999), k=5)
    assert hits[0].record.id == target.id
    assert hits[0].score > 0.999, "stale cached vector was scored instead of the new one"
    a.close()


def test_ties_after_an_upsert_reorder_exactly_as_the_legacy_scan_did(backend, exact):
    """``INSERT OR REPLACE`` re-inserts the row, moving its rowid to the END of
    the table. Among EXACTLY tied scores that visibly reorders the results. It
    did so before this change too, and it must keep doing so identically —
    caching the scan must not freeze the old rowid order."""
    shared = _vec(42)
    records = [_rec(i, kind=Kind.RAW_FACT, vec=shared, content=f"tied {i}") for i in range(5)]
    a = _adapter(backend, records)
    before = [h.record.content for h in a.vector_query(scope=SCOPE, embedding=_vec(7), k=5)]
    assert before == [f"tied {i}" for i in range(5)]

    a.upsert(records=[_rec(0, kind=Kind.RAW_FACT, vec=shared, content="tied 0")])
    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(7), k=5)
    assert [h.record.content for h in hits] == ["tied 1", "tied 2", "tied 3", "tied 4", "tied 0"]
    a.close()


def test_upsert_status_change_invalidates_the_filter_columns(backend, exact):
    a = _adapter(backend, [_rec(i) for i in range(6)])
    a.vector_query(scope=SCOPE, embedding=_vec(1), k=6, where={"status": "active"})  # warm

    r = _rec(1)
    r.status = Status.SUPERSEDED
    a.upsert(records=[r])
    hits = _assert_same(
        a, exact=exact, scope=SCOPE, embedding=_vec(1), k=6, where={"status": "active"}
    )
    assert r.id not in {h.record.id for h in hits}
    a.close()


def test_bump_proof_count_keeps_cache_but_serves_fresh_counts(backend, exact):
    """proof_count is not a cached column, so mark_used() must NOT nuke the
    cache — but the records handed back must still carry the fresh count."""
    records = [_rec(i) for i in range(5)]
    a = _adapter(backend, records)
    a.vector_query(scope=SCOPE, embedding=_vec(0), k=5)
    cached = a._scan_cache[(_KIND_TABLE[Kind.RAW_FACT], SCOPE.key())]
    a.bump_proof_count(scope=SCOPE, ids=[records[0].id])
    assert a._scan_cache[(_KIND_TABLE[Kind.RAW_FACT], SCOPE.key())] is cached, (
        "bump_proof_count should not disturb the scan cache"
    )

    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(0), k=5)
    counts = {h.record.id: h.record.proof_count for h in hits}
    assert counts[records[0].id] == 1, "served a stale proof_count"
    assert counts[records[1].id] == 0
    a.close()


def test_cache_survives_repeated_identical_queries(backend, exact):
    a = _adapter(backend, [_rec(i) for i in range(20)])
    baseline = [(h.score, h.record.id) for h in a.vector_query(scope=SCOPE, embedding=_vec(4), k=5)]
    for _ in range(5):
        again = [(h.score, h.record.id) for h in a.vector_query(scope=SCOPE, embedding=_vec(4), k=5)]
        assert again == baseline
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(4), k=5)
    a.close()


def test_second_connection_write_invalidates_cache(tmp_path, backend):
    """A cache keyed only on this adapter's own write counter would serve stale
    vectors forever once another process wrote the same .db. PRAGMA data_version
    is the other half of the token."""
    db = str(tmp_path / "shared.db")
    writer = SQLiteAdapter(db, vector_backend=backend)
    reader = SQLiteAdapter(db, vector_backend=backend)

    writer.add(records=[_rec(i) for i in range(5)])
    assert len(reader.vector_query(scope=SCOPE, embedding=_vec(1), k=10).hits) == 5  # warms reader

    writer.add(records=[_rec(50, content="written by the other connection")])
    hits = reader.vector_query(scope=SCOPE, embedding=_vec(50), k=10).hits
    assert len(hits) == 6, "reader served a cache stale w.r.t. another connection"
    assert hits[0].record.content == "written by the other connection"

    writer.delete(scope=SCOPE, ids=[hits[0].record.id])
    assert len(reader.vector_query(scope=SCOPE, embedding=_vec(50), k=10).hits) == 5
    writer.close()
    reader.close()


def test_rollback_leaves_the_cache_matching_the_database(backend, exact):
    """A failed batch must not fold its half-applied rows into the cache."""
    records = [_rec(i) for i in range(5)]
    a = _adapter(backend, records)
    a.vector_query(scope=SCOPE, embedding=_vec(1), k=10)  # warm

    # add() of an already-present record raises on the UNIQUE content-address;
    # the fresh record ahead of it in the batch is rolled back with it.
    with pytest.raises(sqlite3.IntegrityError):
        a.add(records=[_rec(77), records[2]])

    hits = _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(77), k=10)
    assert len(hits) == 5, "a rolled-back row was folded into the cache"
    a.close()


def test_write_then_read_updates_the_cache_in_place(backend):
    """The whole point of the surgical update: remember()-then-recall() must not
    re-decode the entire store. The cache OBJECT survives the write and simply
    grows — if this regresses to a rebuild, the write→read cycle goes quadratic."""
    a = _adapter(backend, [_rec(i) for i in range(20)])
    a.vector_query(scope=SCOPE, embedding=_vec(1), k=5)
    key = (_KIND_TABLE[Kind.RAW_FACT], SCOPE.key())
    cached = a._scan_cache[key]
    before = len(cached.rows)

    a.add(records=[_rec(500, kind=Kind.RAW_FACT)])
    assert a._scan_cache[key] is cached, "the write dropped the cache instead of updating it"
    assert len(cached.rows) == before + 1

    a.delete(scope=SCOPE, ids=[_rec(500, kind=Kind.RAW_FACT).id])
    assert a._scan_cache[key] is cached
    assert len(cached.rows) == before
    a.close()


def test_cached_row_order_tracks_rowid_order_under_random_mutation(backend):
    """The cache claims its dict insertion order IS SQLite's rowid order, and
    the pure-Python/numpy scans lean on that for tie-breaking. Hammer it with a
    random add/upsert/delete sequence and diff against the real rowid order."""
    import random

    rng = random.Random(20250710)
    a = _adapter(backend)
    table = _KIND_TABLE[Kind.RAW_FACT]
    live: list[int] = []

    def rowid_order() -> list[str]:
        return [
            r["id"]
            for r in a._conn.execute(
                f"SELECT id FROM {table} WHERE scope_key=? AND embedding IS NOT NULL "
                f"ORDER BY rowid",
                (SCOPE.key(),),
            )
        ]

    for step in range(120):
        op = rng.choice(["add", "add", "upsert", "delete"])
        if op == "add":
            i = rng.randrange(1000)
            if i in live:
                continue
            a.add(records=[_rec(i, kind=Kind.RAW_FACT)])
            live.append(i)
        elif op == "upsert" and live:
            i = rng.choice(live)
            a.upsert(records=[_rec(i, kind=Kind.RAW_FACT, vec=_vec(i + 5000))])
        elif op == "delete" and live:
            i = live.pop(rng.randrange(len(live)))
            a.delete(scope=SCOPE, ids=[_rec(i, kind=Kind.RAW_FACT).id])

        a.vector_query(scope=SCOPE, embedding=_vec(step), k=3)  # keep the cache live
        cached = a._scan_cache.get((table, SCOPE.key()))
        if cached is not None:
            assert list(cached.rows) == rowid_order(), f"cache order drifted at step {step}"
    a.close()


# --- bounded memory ----------------------------------------------------------
def test_cache_disabled_by_budget_zero_is_still_correct(backend, exact):
    a = SQLiteAdapter(":memory:", vector_backend=backend, vector_cache_max_vectors=0)
    a.add(records=[_rec(i) for i in range(30)])
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(3), k=10)
    assert a._scan_cache == {}, "nothing should be retained at budget 0"
    a.close()


def test_scope_larger_than_the_whole_budget_is_never_cached(backend, exact):
    a = SQLiteAdapter(":memory:", vector_backend=backend, vector_cache_max_vectors=4)
    a.add(records=[_rec(i, kind=Kind.RAW_FACT) for i in range(10)])
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(3), k=5)
    assert a._cached_vector_count() <= 4
    a.close()


def test_cache_never_exceeds_its_budget_across_scopes_or_ingest(backend):
    budget = 12
    a = SQLiteAdapter(":memory:", vector_backend=backend, vector_cache_max_vectors=budget)
    scopes = [Scope(tenant="t", project="p", agent=f"a{i}") for i in range(6)]
    for si, sc in enumerate(scopes):
        recs = [
            MemoryRecord.create(
                scope=sc, kind=Kind.RAW_FACT, content=f"s{si} record {i}",
                provenance=Provenance(
                    source_uri=f"s://{si}/{i}", adapter_name="b", adapter_version="1"
                ),
                trust_tier=TrustTier.UNVERIFIED, embedding=_vec(si * 100 + i),
                embedder_name="stub", embedder_dim=16,
            )
            for i in range(5)
        ]
        a.add(records=recs)
        assert len(a.vector_query(scope=sc, embedding=_vec(si * 100), k=5).hits) == 5
        assert a._cached_vector_count() <= budget, "scan cache blew its budget"

    # ...and growing one live entry row-by-row past the budget also stays bounded
    hot = scopes[0]
    for i in range(40):
        a.add(
            records=[
                MemoryRecord.create(
                    scope=hot, kind=Kind.RAW_FACT, content=f"hot growth {i}",
                    provenance=Provenance(
                        source_uri=f"s://hot/{i}", adapter_name="b", adapter_version="1"
                    ),
                    trust_tier=TrustTier.UNVERIFIED, embedding=_vec(9000 + i),
                    embedder_name="stub", embedder_dim=16,
                )
            ]
        )
        assert a._cached_vector_count() <= budget
    a.close()


def test_evicted_entry_still_answers_correctly(backend, exact):
    """Eviction is a cost decision, never a correctness one."""
    a = SQLiteAdapter(":memory:", vector_backend=backend, vector_cache_max_vectors=8)
    a.add(records=[_rec(i, kind=Kind.RAW_FACT) for i in range(6)])
    a.vector_query(scope=SCOPE, embedding=_vec(1), k=6)
    other = Scope(tenant="t", project="p", agent="cold")
    a.add(
        records=[
            MemoryRecord.create(
                scope=other, kind=Kind.RAW_FACT, content=f"other {i}",
                provenance=Provenance(source_uri=f"o://{i}", adapter_name="b", adapter_version="1"),
                trust_tier=TrustTier.UNVERIFIED, embedding=_vec(700 + i),
                embedder_name="stub", embedder_dim=16,
            )
            for i in range(6)
        ]
    )
    a.vector_query(scope=other, embedding=_vec(700), k=6)  # forces eviction of SCOPE
    _assert_same(a, exact=exact, scope=SCOPE, embedding=_vec(1), k=6)
    a.close()


def test_cached_vectors_are_compact_arrays_not_boxed_float_lists():
    """dim=384 x 8 bytes, not dim x 32. At 10k records that is 32 MB, not 125 MB."""
    from array import array as _array

    a = _adapter("python", [_rec(0)])
    a.vector_query(scope=SCOPE, embedding=_vec(0), k=1)
    entry = a._scan_cache[(_KIND_TABLE[Kind.RAW_FACT], SCOPE.key())]
    (cached,) = entry.rows.values()
    assert isinstance(cached.vec, _array) and cached.vec.typecode == "d"
    a.close()


def test_negative_cache_budget_is_rejected():
    with pytest.raises(ValueError, match="must be >= 0"):
        SQLiteAdapter(":memory:", vector_cache_max_vectors=-1)


# --- backend selection contract ----------------------------------------------
def test_default_backend_is_auto_and_never_imports_numpy():
    """`auto` must not IMPORT numpy — it may only ride one that is already
    resident. tests/test_invariants.py enforces the same rule end-to-end in a
    clean subprocess; this pins the adapter-level contract."""
    a = SQLiteAdapter(":memory:")
    assert a.vector_backend == "auto"
    a.close()


def test_python_backend_never_uses_numpy_even_when_resident():
    pytest.importorskip("numpy")
    a = SQLiteAdapter(":memory:", vector_backend="python")
    assert a._numpy() is None
    a.close()


def test_auto_backend_rides_a_resident_numpy():
    numpy = pytest.importorskip("numpy")
    a = SQLiteAdapter(":memory:")
    assert a._numpy() is numpy
    a.close()


def test_unknown_backend_is_rejected_loudly():
    with pytest.raises(ValueError, match="unknown vector_backend"):
        SQLiteAdapter(":memory:", vector_backend="hnsw")


def test_numpy_backend_fails_fast_when_numpy_is_absent(monkeypatch):
    """A store that only reveals a missing numpy under query load reveals it in
    production. `sys.modules[name] = None` is the stdlib way to make `import
    name` raise, so this is what a box without the [embeddings] extra sees."""
    monkeypatch.setitem(sys.modules, "numpy", None)
    with pytest.raises(ImportError):
        SQLiteAdapter(":memory:", vector_backend="numpy")


def test_auto_falls_back_to_pure_python_when_numpy_is_absent(monkeypatch):
    monkeypatch.setitem(sys.modules, "numpy", None)
    a = SQLiteAdapter(":memory:")
    assert a._numpy() is None
    a.add(records=[_rec(i) for i in range(5)])
    _assert_same(a, exact=True, scope=SCOPE, embedding=_vec(1), k=3)
    a.close()


def test_full_conformance_suite_passes_on_both_backends(backend):
    """`auto` resolves to numpy or pure Python depending on what else the process
    has imported. Neither is allowed to be the only one the contract holds for."""
    from rekoll import conformance
    from rekoll.embedding import StubEmbedder

    passed = conformance.run_all(
        lambda: SQLiteAdapter(":memory:", vector_backend=backend), StubEmbedder()
    )
    assert len(passed) == len(conformance.ALL_CHECKS)


def test_sqlite_adapter_does_not_advertise_a_vector_index():
    """It is an exact full scan. Advertising CAP_VECTOR_INDEX would tell callers
    to expect sublinear reads and approximate recall; both would be lies."""
    from rekoll.adapters.base import CAP_VECTOR_INDEX

    a = SQLiteAdapter(":memory:")
    assert not a.supports(CAP_VECTOR_INDEX)
    a.close()
