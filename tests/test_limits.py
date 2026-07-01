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


# ---- read path: query sanitization (P2-8, DESIGN §7) -----------------------

def test_recall_query_is_sanitized_like_stored_content():
    # Stored content had zero-width chars stripped at ingest; the SAME query
    # with an embedded ZWSP must still match ("ig<ZWSP>nore" would otherwise
    # tokenize as "ig", "nore" and miss).
    mem = _mem()
    mem.remember("rotation policy for backup archives is monthly")
    hits = mem.recall("rota​tion policy backup", k=3)
    assert any("rotation policy" in t for t in hits.texts())
    mem.close()


def test_hybrid_search_truncates_oversized_query():
    from rekoll.retrieval import MAX_QUERY_CHARS

    mem = _mem()
    mem.remember("alpha beta gamma delta epsilon zeta")
    # The needle sits past the cap: if truncation works, it never reaches the
    # engine; the head terms still do. This also pins that a >cap query cannot
    # push unbounded text into embedding/lexical work.
    query = "alpha beta " + ("pad " * (MAX_QUERY_CHARS // 4)) + " epsilon"
    assert len(query) > MAX_QUERY_CHARS
    hits = mem.recall(query, k=3)
    assert any("alpha beta" in t for t in hits.texts())
    mem.close()


# ---- ReDoS gate: the firewall regexes must stay near-linear (P2-8) ----------
#
# Audit note (2026-07-02): none of the current patterns backtracks
# catastrophically — the marker alternation-stars are anchored by literal
# prefixes ("ignore", "disregard", ...) and every secret pattern uses disjoint
# character classes. This gate exists so a future pattern edit that introduces
# real blowup (nested quantifiers, overlapping alternations) fails CI instead
# of shipping. Budgets are generous for slow CI: catastrophic backtracking on
# these sizes would take minutes, not seconds.

REDOS_BUDGET_SECONDS = 2.0


def _pathological_inputs() -> list[tuple[str, str]]:
    n = 20_000
    return [
        ("marker-filler-star", "ignore " + "all " * n + "no terminal keyword"),
        ("marker-restart", "ignore all " * (n // 2) + "x"),
        ("marker-homoglyph-flood", "Ignоre аll " * (n // 4) + "x"),
        ("credential-long-tail", "api_key = '" + "A" * n),
        ("jwt-two-segments", "eyJ" + "a" * n + "." + "b" * n),
        ("connection-string-bait", "scheme://" + "u" * n + ":" + "p" * n + "@"),
        ("pem-header-flood", "-----BEGIN " * (n // 8)),
        ("key-prefix-flood", "sk-" * (n // 2)),
        ("plain-prose-control", "the deploy runs nightly and rotates logs " * (n // 8)),
    ]


@pytest.mark.parametrize(
    "name,payload", _pathological_inputs(), ids=[c[0] for c in _pathological_inputs()]
)
def test_screen_is_time_bounded_on_pathological_input(name, payload):
    import time

    from rekoll import TrustTier
    from rekoll.firewall import screen

    start = time.perf_counter()
    screen(payload, source_trust=TrustTier.UNVERIFIED)
    elapsed = time.perf_counter() - start
    assert elapsed < REDOS_BUDGET_SECONDS, (
        f"firewall screen took {elapsed:.2f}s on {name!r} "
        f"({len(payload):,} chars) — a pattern likely regressed to "
        "catastrophic backtracking"
    )
