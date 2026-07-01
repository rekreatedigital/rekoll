"""Resource-exhaustion guards (P0-3, ADR-0017): content, file, and FTS bounds.

Complexity guards for the firewall regexes (the ReDoS gate) live here too once
added — this file is the one place the "bounded inputs everywhere" story is
pinned by tests.
"""

from __future__ import annotations

import pytest

from rekoll import Memory
from rekoll.adapters.sqlite import _MAX_FTS_TERMS, _fts_query
from rekoll.embedding import StubEmbedder


def _mem(**kwargs) -> Memory:
    return Memory(path=":memory:", embedder=StubEmbedder(), reranker=None, **kwargs)


# ---- remember(): max_content_chars ----------------------------------------

def test_remember_over_content_cap_raises_and_points_to_ingest():
    mem = _mem(max_content_chars=100)
    with pytest.raises(ValueError, match="ingest_text"):
        mem.remember("x" * 101)
    mem.close()


def test_remember_at_content_cap_is_accepted():
    mem = _mem(max_content_chars=100)
    record = mem.remember("y" * 100)
    assert len(record.content) == 100
    mem.close()


def test_limit_knobs_must_be_positive():
    with pytest.raises(ValueError, match="positive"):
        _mem(max_content_chars=0)
    with pytest.raises(ValueError, match="positive"):
        _mem(max_file_bytes=-1)


# ---- ingest_text(): max_file_bytes ----------------------------------------

def test_ingest_text_over_document_cap_raises():
    mem = _mem(max_file_bytes=1_000)
    with pytest.raises(ValueError, match="max_file_bytes"):
        mem.ingest_text("z" * 1_001, name="big.txt")
    mem.close()


def test_ingest_text_under_cap_still_works():
    mem = _mem(max_file_bytes=10_000)
    assert mem.ingest_text("A short note about deploy windows.", name="ok.txt") == 1
    mem.close()


# ---- ingest_path(): oversized files skipped, never read --------------------

def test_ingest_path_skips_oversized_files_and_counts_them(tmp_path):
    (tmp_path / "big.md").write_text("# Big\n\n" + ("word " * 2_000), encoding="utf-8")
    (tmp_path / "small.md").write_text("# Small\n\nThe deploy runs nightly.", encoding="utf-8")
    mem = _mem(max_file_bytes=1_024)
    stats = mem.ingest_path(str(tmp_path))
    assert stats["files"] == 1
    assert stats["skipped"] == 1
    assert any("nightly" in t for t in mem.recall("deploy nightly", k=3).texts())
    assert all("word" not in t for t in mem.recall("word word word", k=3).texts())
    mem.close()


# ---- lexical query: bounded, de-duplicated FTS expression ------------------

def test_fts_query_caps_and_dedupes_terms():
    huge = "term " * 50_000
    expr = _fts_query(huge)
    assert expr == '"term"'  # 50k repeats of one word: one quoted term
    distinct = " ".join(f"w{i}" for i in range(1_000))
    expr = _fts_query(distinct)
    assert expr is not None
    assert expr.count(" OR ") == _MAX_FTS_TERMS - 1  # capped, not unbounded
    assert len(expr) < 1_000


def test_fts_query_preserves_first_seen_order_and_short_queries():
    assert _fts_query("why postgres over bigquery") == '"why" OR "postgres" OR "over" OR "bigquery"'
    assert _fts_query("...") is None


def test_recall_with_pathological_query_still_returns():
    mem = _mem()
    mem.remember("a plain fact about database maintenance windows")
    hits = mem.recall("database " + "filler " * 30_000 + "maintenance", k=3)
    assert any("maintenance" in t for t in hits.texts())
    mem.close()
