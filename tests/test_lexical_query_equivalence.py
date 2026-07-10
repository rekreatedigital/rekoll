"""ADR-0030: ``lexical_query`` reconstructs only the k winners, not every FTS
match. Prove that changed nothing a caller can observe.

``_legacy_lexical_tail`` reproduces the pre-ADR-0030 tail verbatim (build a full
MemoryRecord for EVERY match row, filter on the reconstructed record, trim to k).
Every test diffs the live implementation against it.

The subtle one is ``status``. ``MemoryRecord.__post_init__`` rewrites an ACTIVE
status to QUARANTINED at quarantine-level trust, so the legacy tail filtered on
the REWRITTEN value while the raw ``status`` column may say something else. The
fast path reads the column, so it must reapply the rewrite — and a row that
disagrees is planted here by hand to prove it does.
"""

from __future__ import annotations

import pytest

from rekoll.adapters.base import QueryHit, QueryResult
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier

SCOPE = Scope(tenant="t", project="p", agent="a")
KINDS = [Kind.RAW_FACT, Kind.OBSERVATION, Kind.DIRECTIVE, Kind.EPISODE]


def _legacy_lexical_tail(adapter, rows, *, scope, k, status_filter, min_trust):
    records = {
        r.id: r for r in adapter.get(scope=scope, ids=[row["rid"] for row in rows]).records
    }
    hits = []
    for row in rows:
        record = records.get(row["rid"])
        if record is None:
            continue
        if status_filter is not None and record.status.value != status_filter:
            continue
        if min_trust is not None and int(record.trust_tier) < int(min_trust):
            continue
        hits.append(QueryHit(record=record, score=-float(row["s"])))
        if len(hits) >= k:
            break
    return QueryResult(hits=tuple(hits))


def _legacy_lexical_query(adapter, *, scope, text, k=10, kind=None, where=None):
    from rekoll.adapters.sqlite import _ALLOWED_WHERE_KEYS, _fts_query

    if where:
        bad = set(where) - _ALLOWED_WHERE_KEYS
        if bad:
            raise ValueError(f"unsupported where keys {sorted(bad)}")
    if k <= 0:
        return QueryResult(hits=())
    match = _fts_query(text)
    if match is None:
        return QueryResult(hits=())
    status_filter = where.get("status") if where else None
    min_trust = where.get("min_trust") if where else None
    has_filter = status_filter is not None or min_trust is not None
    sql = "SELECT rid, bm25(fts) AS s FROM fts WHERE fts MATCH ? AND scope_key=?"
    params = [match, scope.key()]
    if kind is not None:
        sql += " AND kind=?"
        params.append(kind.value)
    sql += " ORDER BY s"
    if not has_filter:
        sql += " LIMIT ?"
        params.append(max(0, k) * 4 + 10)
    rows = adapter._conn.execute(sql, params).fetchall()
    if not rows:
        return QueryResult(hits=())
    return _legacy_lexical_tail(
        adapter, rows, scope=scope, k=k, status_filter=status_filter, min_trust=min_trust
    )


def _assert_same(adapter, **kwargs):
    expected = _legacy_lexical_query(adapter, **kwargs)
    actual = adapter.lexical_query(**kwargs)
    assert [(h.score, h.record.id) for h in actual.hits] == [
        (h.score, h.record.id) for h in expected.hits
    ], "lexical hits or their order diverged from the pre-ADR-0030 tail"
    return actual.hits


WORDS = "alpha beta gamma delta epsilon zeta eta theta iota kappa".split()


def _rec(i, *, trust=TrustTier.UNVERIFIED, kind=None, status=None):
    r = MemoryRecord.create(
        scope=SCOPE,
        kind=kind or KINDS[i % 4],
        content=f"{WORDS[i % 10]} {WORDS[(i * 3) % 10]} memo {i} {WORDS[(i * 7) % 10]}",
        provenance=Provenance(source_uri=f"s://x/{i}", adapter_name="b", adapter_version="1"),
        trust_tier=trust,
        embedding=tuple(float((i + j) % 5) for j in range(8)),
        embedder_name="stub",
        embedder_dim=8,
    )
    if status is not None:
        r.status = status
    return r


def _adapter(records=()):
    a = SQLiteAdapter(":memory:")
    if records:
        a.add(records=list(records))
    return a


@pytest.mark.parametrize("k", [1, 3, 10, 40])
def test_matches_legacy_without_filter(k):
    a = _adapter([_rec(i) for i in range(60)])
    _assert_same(a, scope=SCOPE, text="alpha beta gamma", k=k)
    a.close()


@pytest.mark.parametrize(
    "where",
    [
        None,
        {"status": "active"},
        {"status": "superseded"},
        {"min_trust": 0},
        {"min_trust": 3},
        {"status": "active", "min_trust": 2},
    ],
)
def test_matches_legacy_with_filters(where):
    records = [_rec(i, trust=TrustTier(i % 5)) for i in range(60)]
    records[2].status = Status.SUPERSEDED
    records[11].status = Status.SUPERSEDED
    a = _adapter(records)
    _assert_same(a, scope=SCOPE, text="alpha beta gamma delta", k=12, where=where)
    a.close()


@pytest.mark.parametrize("kind", KINDS)
def test_matches_legacy_per_kind(kind):
    a = _adapter([_rec(i) for i in range(40)])
    hits = _assert_same(a, scope=SCOPE, text="alpha beta gamma delta", k=10, kind=kind)
    assert all(h.record.kind is kind for h in hits)
    a.close()


def test_filter_returns_k_even_when_high_ranking_rows_are_filtered_out():
    """The reason the filtered path must not pre-LIMIT: rows that rank ahead but
    fail the filter must not crowd valid matches out of a fixed window."""
    records = [_rec(i, trust=TrustTier.QUARANTINED) for i in range(50)]
    records += [_rec(100 + i, trust=TrustTier.OWNER) for i in range(6)]
    a = _adapter(records)
    hits = _assert_same(
        a, scope=SCOPE, text=" ".join(WORDS), k=5, where={"status": "active", "min_trust": 4}
    )
    assert len(hits) == 5
    assert all(h.record.trust_tier is TrustTier.OWNER for h in hits)
    a.close()


def test_quarantined_never_surfaces_through_the_active_filter():
    clean = [_rec(i, trust=TrustTier.UNVERIFIED) for i in range(10)]
    poison = [_rec(100 + i, trust=TrustTier.QUARANTINED) for i in range(10)]
    assert all(r.status is Status.QUARANTINED for r in poison)
    a = _adapter(clean + poison)
    hits = _assert_same(a, scope=SCOPE, text=" ".join(WORDS), k=20, where={"status": "active"})
    assert {h.record.id for h in hits}.isdisjoint({r.id for r in poison})
    a.close()


def test_row_whose_stored_status_disagrees_with_its_trust_is_gated_as_quarantined():
    """A hand-written / legacy row with status='active' at trust 0. The record
    model rewrites it to quarantined on reconstruction, so the filter must too.
    Reading the raw column here would surface a quarantined memory."""
    a = _adapter([_rec(i) for i in range(5)])
    smuggled = _rec(999, trust=TrustTier.UNVERIFIED, kind=Kind.RAW_FACT)
    a.add(records=[smuggled])
    # forge the divergent state the model makes unrepresentable
    a._conn.execute(
        "UPDATE verbatim_records SET trust_tier=0, status='active' WHERE id=?", (smuggled.id,)
    )
    a._conn.commit()

    raw = a._conn.execute(
        "SELECT status, trust_tier FROM verbatim_records WHERE id=?", (smuggled.id,)
    ).fetchone()
    assert (raw["status"], raw["trust_tier"]) == ("active", 0), "fixture failed to forge the row"
    assert a.get(scope=SCOPE, ids=[smuggled.id]).records[0].status is Status.QUARANTINED

    hits = _assert_same(a, scope=SCOPE, text=" ".join(WORDS), k=20, where={"status": "active"})
    assert smuggled.id not in {h.record.id for h in hits}
    # ...and it IS reachable when you ask for quarantined rows explicitly
    hits = _assert_same(a, scope=SCOPE, text=" ".join(WORDS), k=20, where={"status": "quarantined"})
    assert smuggled.id in {h.record.id for h in hits}
    a.close()


def test_orphaned_fts_row_is_skipped():
    a = _adapter([_rec(i) for i in range(5)])
    a._conn.execute(
        "INSERT INTO fts (content, rid, scope_key, kind) VALUES (?,?,?,?)",
        ("alpha beta ghost row", "rk_does_not_exist", SCOPE.key(), Kind.RAW_FACT.value),
    )
    a._conn.commit()
    hits = _assert_same(a, scope=SCOPE, text="alpha beta ghost", k=10)
    assert "rk_does_not_exist" not in {h.record.id for h in hits}
    a.close()


def test_cross_scope_fts_rows_never_leak():
    a = _adapter([_rec(i) for i in range(5)])
    other = Scope(tenant="t", project="p", agent="other")
    a.add(
        records=[
            MemoryRecord.create(
                scope=other, kind=Kind.RAW_FACT, content="alpha beta foreign memo",
                provenance=Provenance(source_uri="o://1", adapter_name="b", adapter_version="1"),
                trust_tier=TrustTier.OWNER, embedding=(1.0,) * 8,
                embedder_name="stub", embedder_dim=8,
            )
        ]
    )
    hits = _assert_same(a, scope=SCOPE, text="alpha beta foreign", k=10)
    assert all(h.record.scope == SCOPE for h in hits)
    a.close()


def test_empty_and_degenerate_queries():
    a = _adapter([_rec(i) for i in range(5)])
    assert a.lexical_query(scope=SCOPE, text="", k=5).hits == ()
    assert a.lexical_query(scope=SCOPE, text="   !!! ", k=5).hits == ()
    assert a.lexical_query(scope=SCOPE, text="alpha", k=0).hits == ()
    assert a.lexical_query(scope=SCOPE, text="zzzznomatch", k=5).hits == ()
    a.close()


def test_reconstructs_only_the_winners(monkeypatch):
    """The actual optimization: k records built, not one per FTS match."""
    a = _adapter([_rec(i) for i in range(60)])
    built = []
    original = SQLiteAdapter._row_to_record
    monkeypatch.setattr(
        SQLiteAdapter,
        "_row_to_record",
        lambda self, row: (built.append(row["id"]), original(self, row))[1],
    )
    hits = a.lexical_query(scope=SCOPE, text=" ".join(WORDS), k=5)
    assert len(hits.hits) == 5
    assert len(built) == 5, f"reconstructed {len(built)} records to return 5"
    a.close()
