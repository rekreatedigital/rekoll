"""Resource-exhaustion guards (P0-3, ADR-0018): content, file, and FTS bounds.

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


# ---- ReDoS gate: the firewall regexes must stay near-LINEAR (P2-8) ----------
#
# History (2026-07-02): the first cut of this gate used an absolute wall-clock
# budget, which is runner-speed-dependent and flaked on slow CI — worse, it
# masked a REAL O(n^2) in two patterns (connection_string scheme, email local
# part) that a greedy prefix rescanned at every word-boundary start. The fix
# bounded those prefixes; this gate now asserts the *scaling* directly, which is
# runner-independent: for a 4x input-size increase a linear pattern takes ~4x
# longer, a quadratic ~16x, an exponential far more. A ratio catches all three
# regardless of how fast the box is. Python's `re` has no atomic groups /
# possessive quantifiers before 3.11, so bounded {m,n} quantifiers are the
# 3.10-safe way to keep prefixes from rescanning.
#
# History (2026-07-06): the ratio of two sub-5ms wall-clock timings is pure
# noise — on a loaded windows-3.13 CI runner a genuinely LINEAR pattern (`phone`
# on a "1-"*n flood) measured 0.58ms -> 4.81ms (x8.3) and tripped the gate. A
# whole BAND of fast combos spikes to x6-8+ from timing jitter alone. The ratio
# is only trustworthy once the absolute time is out of that noise band, so we now
# skip any combo whose 4x-input time is below _RATIO_NOISE_FLOOR_SECONDS. That is
# SAFE, not a coverage hole: the 4x builders are already at/above the 100k content
# cap, so a pattern that screens them in <50ms cannot DoS at any real input size;
# and a true regression to super-linear lands 10x+ above the floor (the O(n^2)
# bugs this suite guards were 0.8-8.6s), so it is still caught. Measured: every
# linear combo sits at x4.0-4.5 once above the floor; the flakes live only below
# ~5ms.

import time as _time

from rekoll import firewall

_SCALE_N = 6_000
_SCALE_FACTOR = 4
# Linear -> ~4x for a 4x input. Allow 8x (2x margin over linear for timing
# noise); quadratic (~16x) and exponential blow well past it.
_MAX_SCALE_RATIO = 8.0
# Only trust the scaling ratio once the 4x-input time clears the timing-noise
# band (see the 2026-07-06 note above). 50ms is 10x over the observed flake band
# (<5ms) and 2x over the slowest benign combo (~25ms); anything genuinely
# super-linear blows far past it. Below it, the pattern is already fast enough at
# >= production-scale input that no DoS is possible.
_RATIO_NOISE_FLOOR_SECONDS = 5e-2
# Absolute backstop: post-fix, screening these sizes is single-digit ms, so a
# generous cap only ever trips on a true hang / exponential blowup.
_HANG_BUDGET_SECONDS = 5.0

# Adversarial builders that maximize backtracking across the pattern shapes we
# ship (URL schemes, base64/alnum runs, digit/dash runs, keyword floods).
_STRESS_BUILDERS = [
    ("sk-prefix-flood", lambda n: "sk-" * n),
    ("scheme-chars-flood", lambda n: "ab.+-" * n),
    ("digit-dash-flood", lambda n: "1-" * n),
    ("at-dot-flood", lambda n: "a.@" * n),
    ("alnum-flood", lambda n: "a1" * n),
    ("url-no-terminator", lambda n: "x://" + "u" * n),
    ("keyword-marker-flood", lambda n: "ignore all " * n),
    ("word-space-flood", lambda n: "override your " * n),
    # Repeated-prefix floods: a shape that RE-ANCHORS a greedy/lazy scan at every
    # occurrence. The private_key full-block (repeated BEGIN header, no END) and
    # jwt (repeated eyJ) were both O(n^2) here and INVISIBLE to the char-run
    # floods above — the gate was blind to them until these builders landed.
    ("pem-begin-flood", lambda n: "-----BEGIN PRIVATE KEY-----" * n),
    ("jwt-eyj-flood", lambda n: "eyJ" * n),
    # Whitespace-only flood: pins the read-path '[n]' rewrite, where a plain
    # "\s*" under (?m) rescans across every newline (O(n^2) per recall). Ingest
    # patterns are linear on it; the read-path gate below is where it bites.
    ("newline-flood", lambda n: "\n" * n),
    # Markdown-markup + unclosed-tag floods: these specifically stress the READ-
    # path delimiter regexes (_ENVELOPE_HEADER_RE's leading [ \t>#*=_~-]* class,
    # and _ROLE_TAG_RE's tag alternation) — the header/tag neutralizers that the
    # ingest per-pattern gate never touches. A future edit that makes either
    # backtrack on forged-header / forged-tag input is caught here.
    ("markup-mix-flood", lambda n: "#>*=_~- " * n),
    ("angle-open-flood", lambda n: "<system" * n),
    ("bracket-tag-flood", lambda n: "[inst" * n),
]


def _all_patterns():
    out = list(firewall._SECRET_PATTERNS) + list(firewall._PII_PATTERNS)
    out += [(f"marker[{i}]", p) for i, p in enumerate(firewall._INJECTION_MARKERS)]
    return out


def _min_time(pattern, text, repeats=5):
    best = float("inf")
    for _ in range(repeats):
        t0 = _time.perf_counter()
        pattern.search(text)
        best = min(best, _time.perf_counter() - t0)
    return best


def test_every_firewall_pattern_scales_linearly():
    # Whitebox: each shipped regex, run in isolation against every adversarial
    # input, must scale ~linearly. Isolation (not full screen()) keeps the ratio
    # undiluted so even a single quadratic pattern is caught cleanly.
    offenders = []
    for pname, pat in _all_patterns():
        for bname, build in _STRESS_BUILDERS:
            t_n = _min_time(pat, build(_SCALE_N))
            t_big = _min_time(pat, build(_SCALE_N * _SCALE_FACTOR))
            if t_big < _RATIO_NOISE_FLOOR_SECONDS:
                continue  # too fast to time reliably (and too fast to DoS) — skip
            ratio = t_big / t_n if t_n > 1e-6 else 1.0
            if ratio > _MAX_SCALE_RATIO:
                offenders.append(
                    f"{pname} on {bname!r}: {t_n*1e3:.2f}ms -> {t_big*1e3:.2f}ms "
                    f"(x{ratio:.1f} for {_SCALE_FACTOR}x input)"
                )
    assert not offenders, (
        "super-linear (ReDoS-prone) regex scaling — a pattern rescans on "
        "backtracking:\n  " + "\n  ".join(offenders)
    )


def _read_path_render(text: str) -> None:
    """Drive the FULL read path a recall exercises: neutralize + envelope render.

    build_envelope() calls _neutralize_delimiters() on every hit's content, and
    render() stitches them. This is what runs on EVERY recall/context() call, so
    its regexes must scale as tightly as the ingest ones — the ingest-only gate
    above never touched it, which is how the '[n]' whitespace quadratic shipped.
    """
    from rekoll import Kind, MemoryRecord, Provenance, Scope, TrustTier
    from rekoll.adapters.base import QueryHit

    record = MemoryRecord.create(
        scope=Scope(), kind=Kind.RAW_FACT, content=text,
        provenance=Provenance(source_uri="t://redos"), trust_tier=TrustTier.TRUSTED_SOURCE,
    )
    firewall.build_envelope([QueryHit(record=record, score=1.0)]).render()


def test_read_path_neutralize_scales_linearly():
    # Same runner-independent scaling assertion as the ingest gate, but over the
    # READ path (_neutralize_delimiters + build_envelope().render()). The newline
    # flood is the killer here: a "\s*"-anchored '[n]' rewrite rescans every line.
    offenders = []
    for target, label in ((firewall._neutralize_delimiters, "neutralize"),
                          (_read_path_render, "envelope-render")):
        for bname, build in _STRESS_BUILDERS:
            t_n = _min_time_call(target, build(_SCALE_N))
            t_big = _min_time_call(target, build(_SCALE_N * _SCALE_FACTOR))
            if t_big < _RATIO_NOISE_FLOOR_SECONDS:
                continue  # too fast to time reliably (and too fast to DoS) — skip
            ratio = t_big / t_n if t_n > 1e-6 else 1.0
            if ratio > _MAX_SCALE_RATIO:
                offenders.append(
                    f"{label} on {bname!r}: {t_n*1e3:.2f}ms -> {t_big*1e3:.2f}ms "
                    f"(x{ratio:.1f} for {_SCALE_FACTOR}x input)"
                )
    assert not offenders, (
        "super-linear scaling on the READ path (runs on every recall):\n  "
        + "\n  ".join(offenders)
    )


def _min_time_call(fn, arg, repeats=5):
    best = float("inf")
    for _ in range(repeats):
        t0 = _time.perf_counter()
        fn(arg)
        best = min(best, _time.perf_counter() - t0)
    return best


def _pathological_inputs():
    n = 20_000
    return {
        "marker-filler-star": "ignore " + "all " * n + "no terminal keyword",
        "marker-restart": "ignore all " * (n // 2) + "x",
        "marker-homoglyph-flood": "Ignоre аll " * (n // 4) + "x",
        "credential-long-tail": "api_key = '" + "A" * n,
        "jwt-two-segments": "eyJ" + "a" * n + "." + "b" * n,
        "connection-string-bait": "scheme://" + "u" * n + ":" + "p" * n + "@",
        "key-prefix-flood": "sk-" * (n // 2),
        "email-local-flood": "1-" * n + "@x.co",
        # Repeated-prefix floods: each 'eyJ' / '-----BEGIN ... PRIVATE KEY-----'
        # used to re-anchor a full forward scan (jwt / private_key O(n^2)). These
        # exercise screen()'s secret patterns on those shapes directly.
        "jwt-eyj-flood": "eyJ" * n,
        "pem-begin-flood": "-----BEGIN PRIVATE KEY-----" * (n // 4),
    }


@pytest.mark.parametrize("name", list(_pathological_inputs()))
def test_screen_does_not_hang_on_pathological_input(name):
    # Coarse absolute backstop over the FULL screen() (both PII off and on), so a
    # true hang / exponential blowup fails even if the scaling test somehow
    # misses it. Generous cap: linear screening here is single-digit ms.
    from rekoll import TrustTier
    from rekoll.firewall import screen

    payload = _pathological_inputs()[name]
    for pii in (False, True):
        start = _time.perf_counter()
        screen(payload, source_trust=TrustTier.UNVERIFIED, redact_pii=pii)
        elapsed = _time.perf_counter() - start
        assert elapsed < _HANG_BUDGET_SECONDS, (
            f"screen(redact_pii={pii}) took {elapsed:.2f}s on {name!r} "
            f"({len(payload):,} chars) — likely catastrophic backtracking"
        )


@pytest.mark.parametrize("name", ["newline-flood", "pem-begin-flood", "jwt-eyj-flood", "bracket-index-flood"])
def test_read_path_does_not_hang_on_pathological_input(name):
    # The screen() backstop above never touches the READ path. This one does: a
    # whitespace-heavy or repeated-prefix record run through build_envelope's
    # neutralizer + render must not hang either (the '[n]' rewrite was O(n^2) on
    # whitespace). bracket-index-flood pins the '[n]' rewrite shape specifically.
    n = 20_000
    payloads = {
        "newline-flood": "\n" * n,
        "pem-begin-flood": "-----BEGIN PRIVATE KEY-----" * (n // 4),
        "jwt-eyj-flood": "eyJ" * n,
        "bracket-index-flood": ("   [1] x\n" * n),
    }
    payload = payloads[name]
    start = _time.perf_counter()
    _read_path_render(payload)
    elapsed = _time.perf_counter() - start
    assert elapsed < _HANG_BUDGET_SECONDS, (
        f"read-path render took {elapsed:.2f}s on {name!r} "
        f"({len(payload):,} chars) — likely catastrophic backtracking"
    )
