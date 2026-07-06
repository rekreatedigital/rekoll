"""P0 definition-of-done: the memory record + IDs behave correctly."""

from __future__ import annotations

import pytest

from rekoll import Kind, MemoryRecord, Provenance, Scope, TrustTier
from rekoll.ids import content_hash, human_id, normalize_content, record_id


def test_content_hash_normalizes_whitespace_and_newlines():
    assert content_hash("hello\r\nworld") == content_hash("hello\nworld")
    assert content_hash("  spaced  ") == content_hash("spaced")
    assert normalize_content("a\r\nb") == "a\nb"


def test_content_hash_deterministic_and_distinct():
    assert content_hash("alpha") == content_hash("alpha")
    assert content_hash("alpha") != content_hash("beta")


def test_record_id_is_deterministic_and_scope_sensitive():
    # (record_id takes kind since ADR-0026; kind-sensitivity itself is asserted
    # where the collision was reachable, in test_ingest.py.)
    assert record_id("s1", "src", "raw_fact", "h") == record_id("s1", "src", "raw_fact", "h")
    assert record_id("s1", "src", "raw_fact", "h") != record_id("s2", "src", "raw_fact", "h")


def test_human_id_format():
    assert human_id(42) == "MEM-0042"
    with pytest.raises(ValueError):
        human_id(-1)


def test_create_computes_content_address():
    record = MemoryRecord.create(
        scope=Scope(),
        kind=Kind.RAW_FACT,
        content="x",
        provenance=Provenance(source_uri="src://a"),
        trust_tier=TrustTier.OWNER,
    )
    assert record.id.startswith("rk_")
    assert record.content_hash == content_hash("x")
    assert record.verify()


def test_same_content_same_scope_same_id():
    kw = dict(scope=Scope(), kind=Kind.RAW_FACT, provenance=Provenance(source_uri="s"), trust_tier=TrustTier.OWNER)
    a = MemoryRecord.create(content="dup", **kw)
    b = MemoryRecord.create(content="dup", **kw)
    assert a.id == b.id


def test_metadata_rejects_nested_structures():
    with pytest.raises(TypeError):
        MemoryRecord.create(
            scope=Scope(),
            kind=Kind.RAW_FACT,
            content="x",
            provenance=Provenance(source_uri="s"),
            trust_tier=TrustTier.OWNER,
            metadata={"bad": {"nested": 1}},
        )


def test_trust_tier_and_kind_coerced():
    record = MemoryRecord.create(
        scope=Scope(),
        kind="raw_fact",  # coerced to Kind
        content="x",
        provenance=Provenance(source_uri="s"),
        trust_tier=2,  # coerced to TrustTier
    )
    assert record.trust_tier is TrustTier.TRUSTED_SOURCE
    assert record.kind is Kind.RAW_FACT


def test_scope_rejects_slash_and_empty():
    with pytest.raises(ValueError):
        Scope(tenant="a/b")
    with pytest.raises(ValueError):
        Scope(project="")


def test_empty_content_rejected():
    with pytest.raises(ValueError):
        MemoryRecord.create(
            scope=Scope(),
            kind=Kind.RAW_FACT,
            content="",
            provenance=Provenance(source_uri="s"),
            trust_tier=TrustTier.OWNER,
        )


def test_provenance_requires_source_uri():
    with pytest.raises(ValueError):
        Provenance(source_uri="")
