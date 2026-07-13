"""Battle-tester repros: read/ingest crashes, a fail-open safety gate, an honesty
lie, a query DoS, and two validation gaps found by red-team v1.

Every test failed on the pre-fix tree and passes after the fix in the same commit.

  R1 (high)  a ~12-32KB adversarial .py (deeply nested unary) made ast.parse raise
             an UNCAUGHT RecursionError/MemoryError; chunk_python caught only
             SyntaxError, so one poisoned file aborted a whole ingest_path walk.
  R2 (high)  a malformed embedding cell crashed recall (uncaught TypeError from
             array('d', ...) / MemoryRecord validation) — invisible to the
             content-only tamper hash (ADR-0019).
  R3 (high)  a NaN/Infinity embedding poisoned the ADR-0028 abstain gate:
             `NaN < min_score` is False -> GATE_PASS, so withheld content surfaced.
  R4 (medium) secrets_stored counted a credential-shaped file whose every chunk
             sanitized to empty — the #41 honesty signal claimed a retrievable
             credential existed when ZERO records were stored.
  R5 (medium) recall() ran sanitize_unicode on the FULL query before the
             MAX_QUERY_CHARS cap, so a 20M-char query cost ~2s (unbounded read).
  R6 (medium) a where={'min_trust': <str/NaN/inf/list>} crashed the read with a raw
             ValueError/OverflowError/TypeError from deep in the scan.
  R7 (low)   whitespace-only provenance.source_uri satisfied the NOT-NULL guard.
  R8 (low)   a Scope part with a lone surrogate constructed, then crashed every DB
             op with a deferred UnicodeEncodeError.
"""

from __future__ import annotations

import os
import sqlite3
import tempfile
import time

import pytest

from rekoll import Memory, Provenance, Scope
from rekoll.chunking import chunk_file
from rekoll.model import Kind, MemoryRecord, TrustTier


def _db():
    return os.path.join(tempfile.mkdtemp(), "m.db")


# ---- R1: adversarial Python source ------------------------------------------

@pytest.mark.parametrize("src", [
    "x = " + "not " * 6000 + "True",    # nested unary -> parser stack overflow
    "x = " + "-" * 6000 + "1",
    "y = " + "(" * 4000 + "1" + ")" * 4000,
])
def test_chunk_python_survives_pathological_source(src):
    pieces = chunk_file("evil.py", src)   # must fall back, not raise
    assert pieces  # produced text chunks


def test_ingest_path_walk_not_aborted_by_one_poison_py(tmp_path):
    (tmp_path / "good1.py").write_text("def a():\n    return 1\n")
    (tmp_path / "evil.py").write_text("x = " + "not " * 6000 + "True")
    (tmp_path / "good2.py").write_text("def b():\n    return 2\n")
    m = Memory(path=str(tmp_path / "m.db"), embedder="stub")
    stats = m.ingest_path(str(tmp_path))     # must not raise
    assert stats["files"] >= 3               # the poison file did not abort the walk


# ---- R2 / R3: adversarial embedding values ----------------------------------

def _tamper_embedding(dbp: str, value: str) -> str:
    con = sqlite3.connect(dbp)
    rid = con.execute("SELECT id FROM verbatim_records LIMIT 1").fetchone()[0]
    con.execute("UPDATE verbatim_records SET embedding=? WHERE id=?", (value, rid))
    con.commit()
    con.close()
    return rid


@pytest.mark.parametrize("value", ['"garbage"', "[[1,2],[3,4]]", '{"a":1}', "not json"])
def test_malformed_embedding_fails_cleanly_not_a_raw_typeerror(value):
    # Rekoll's contract: a hand-edited/corrupt store fails VISIBLY, not with a
    # raw traceback and not with silent-wrong results. A valid-JSON-but-wrong-
    # shape cell ('"garbage"', '[[1,2]]') used to crash recall with an UNCAUGHT
    # TypeError from array('d', ...); now every corrupt shape is one clean
    # ValueError (which the CLI turns into a clean exit 1).
    dbp = _db()
    m = Memory(path=dbp, embedder="stub")
    m.remember("the capital of france is paris")
    _tamper_embedding(dbp, value)
    with pytest.raises(ValueError):
        Memory(path=dbp, embedder="stub").recall("france capital", k=3)


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_embedding_does_not_fail_open_the_abstain_gate(token):
    # A NaN/Infinity embedding used to poison the ADR-0028 abstain gate:
    # top_vector_score became NaN, `NaN < min_score` is False -> GATE_PASS, so
    # content that should have been WITHHELD surfaced. Now a non-finite cell is a
    # corrupt cell -> clean ValueError, so it can neither surface nor poison the
    # gate (fail visible, consistent with the malformed-cell contract above).
    dbp = _db()
    m = Memory(path=dbp, embedder="stub")
    m.remember("the quick brown fox jumps over the lazy dog")
    _tamper_embedding(dbp, "[" + ",".join([token] * 8) + "]")
    with pytest.raises(ValueError):
        Memory(path=dbp, embedder="stub").recall(
            "photosynthesis chloroplast enzyme", k=5, min_score=0.99,
        )


# ---- R4: secrets_stored honesty ---------------------------------------------

def test_secrets_stored_not_counted_when_nothing_is_stored(tmp_path):
    fp = tmp_path / "credentials.json"
    fp.write_bytes(("​" * 200).encode("utf-8"))  # all zero-width -> 0 chunks
    m = Memory(path=str(tmp_path / "m.db"), embedder="stub")
    before = m.count()
    stats = m.ingest_path(str(fp))
    assert stats["secrets_stored"] == 0          # nothing stored -> count is honest
    assert m.count() - before == 0


def test_secrets_stored_still_counts_a_real_stored_secret(tmp_path):
    fp = tmp_path / "id_rsa"
    fp.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nreal body text here\n-----END OPENSSH PRIVATE KEY-----\n")
    m = Memory(path=str(tmp_path / "m.db"), embedder="stub")
    stats = m.ingest_path(str(fp))
    assert stats["secrets_stored"] == 1          # a stored secret is NOT under-counted


# ---- R5: query-size DoS -----------------------------------------------------

def test_recall_query_size_is_bounded_before_sanitize():
    m = Memory(path=_db(), embedder="stub")
    m.remember("x")
    t = time.perf_counter()
    m.recall("a" * 8_000_000, k=3)
    dt = time.perf_counter() - t
    assert dt < 0.5, f"a huge query still costs {dt:.2f}s (sanitize ran pre-truncation)"


# ---- R6: where-clause validation --------------------------------------------

@pytest.mark.parametrize("bad", ["abc", float("nan"), float("inf"), [1]])
def test_bad_min_trust_raises_clean_valueerror_not_a_read_crash(bad):
    # ``where`` is an adapter-level filter (advanced API). A non-int-coercible
    # min_trust used to crash the read with a raw ValueError/OverflowError/
    # TypeError from deep in the scan; now it is one clean ValueError at the door.
    from rekoll.adapters.sqlite import SQLiteAdapter

    scope = Scope()
    rec = MemoryRecord.create(
        scope=scope, kind=Kind.RAW_FACT, content="hello world",
        provenance=Provenance(source_uri="t://x"), trust_tier=TrustTier.OWNER,
    ).with_embedding([0.1, 0.2, 0.3], name="stub", dim=3)
    ad = SQLiteAdapter()
    ad.add(records=[rec])
    with pytest.raises(ValueError):
        ad.vector_query(scope=scope, embedding=[0.1, 0.2, 0.3], k=5, where={"min_trust": bad})
    with pytest.raises(ValueError):
        ad.lexical_query(scope=scope, text="hello world", k=5, where={"min_trust": bad})


# ---- R7 / R8: construction validation ---------------------------------------

@pytest.mark.parametrize("uri", ["   ", "\n\t", " "])
def test_whitespace_only_source_uri_is_rejected(uri):
    with pytest.raises(ValueError):
        Provenance(source_uri=uri)


def test_scope_rejects_lone_surrogate_at_construction():
    with pytest.raises(ValueError):
        Scope(project=chr(0xD800) + "bad")
