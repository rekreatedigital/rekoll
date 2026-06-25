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
