"""Integrity guard for the frozen semantic_v1 fixture (Lane 1b).

INTEGRITY ONLY — no retrieval quality gate lives here (that is Lane 1d,
conductor-owned). Stub-safe: no model download, no network, pure stdlib —
default CI stays offline.

The pinned hashes make silent edits fail loudly: changing fixture content (or
the filler generator) requires touching this file too, which is visible in
review. The derivation rules themselves are re-run from raw content via
benchmarks/fixtures/verify_semantic_v1.py, so derived fields (overlap,
buckets, hard negatives) cannot be hand-tuned either.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

FIXDIR = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures"
FIXTURE_PATH = FIXDIR / "semantic_v1.json"

# Frozen at the freeze commit; recorded in PREREGISTRATION_semantic_v1.md.
EXPECTED_CONTENT_SHA256 = "8580fe4bcce5415a7cdfcbbe534b08ffbcbd07b13d266317620d8442591a1a58"
# Deterministic filler corpora (seed 20260707) for the 1k / 10k sweep.
EXPECTED_FILLER_900_SHA256 = "04ab012e921bbbbc863192a069df26f42c59484c29226d90b94436c67bd8f1cf"
EXPECTED_FILLER_9900_SHA256 = "a9f9d120db661a5c454edfb3390cfe84b84cf8bc3a593b45e5153c144522c762"


def _load(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def fixture() -> dict:
    return json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def verify_lib():
    return _load("verify_semantic_v1", FIXDIR / "verify_semantic_v1.py")


@pytest.fixture(scope="module")
def gen_lib():
    return _load("gen_distractors", FIXDIR / "gen_distractors.py")


def test_content_hash_pinned(fixture, verify_lib):
    """Silent fixture edits must fail CI: metadata hash AND pinned constant."""
    assert fixture["metadata"]["content_sha256"] == EXPECTED_CONTENT_SHA256
    assert verify_lib.canonical_content_hash(fixture) == EXPECTED_CONTENT_SHA256


def test_full_derivation_verifies(fixture, verify_lib):
    """Re-derive overlap/buckets/hard-negatives/hash from raw content."""
    assert verify_lib.verify(fixture) == []


def test_schema(fixture):
    doc_keys = set()
    for d in fixture["documents"]:
        assert set(d) == {"key", "project", "kind", "role", "text"}
        assert d["role"] in ("gold", "distractor")
        assert d["text"].strip()
        doc_keys.add(d["key"])
    assert len(doc_keys) == len(fixture["documents"]) == 100

    qids = set()
    for q in fixture["queries"]:
        assert set(q) == {"qid", "query", "relevant", "paraphrase", "control",
                          "overlap", "bucket", "hard_negatives"}
        assert q["control"] is False
        assert q["bucket"] in ("low", "med", "high")
        assert 0.0 <= q["overlap"] <= 1.0
        assert q["relevant"], "scored query without gold"
        assert set(q["relevant"]) <= doc_keys
        qids.add(q["qid"])
    assert len(qids) == len(fixture["queries"])

    for c in fixture["controls"]:
        assert c["control"] is True
        assert c["control_terms"], "control without control_terms"
        assert c["qid"] not in qids


def test_composition_bars(fixture):
    """Pre-registered minimums (PREREGISTRATION_semantic_v1.md section 4)."""
    queries = fixture["queries"]
    assert len(queries) >= 100
    buckets = {b: sum(1 for q in queries if q["bucket"] == b) for b in ("low", "med", "high")}
    assert all(v >= 30 for v in buckets.values()), buckets
    assert sum(1 for q in queries if q["paraphrase"]) >= 40
    assert sum(1 for q in queries if len(q["relevant"]) >= 3) >= 15
    assert len(fixture["controls"]) >= 12


def test_hard_negatives(fixture):
    for q in fixture["queries"]:
        assert len(q["hard_negatives"]) >= 20, q["qid"]
        assert not (set(q["hard_negatives"]) & set(q["relevant"])), q["qid"]
    assert "bm25" in fixture["metadata"]["hard_negatives"]["miner"].lower()


def test_controls_absent_from_committed_and_generated_corpus(fixture, verify_lib, gen_lib):
    """Negative-control answers must stay absent at every corpus size."""
    corpus_tokens: set[str] = set()
    for d in fixture["documents"]:
        corpus_tokens |= verify_lib.content_tokens(d["text"])
    for f in gen_lib.generate(9900):
        corpus_tokens |= verify_lib.content_tokens(f["text"])
    for c in fixture["controls"]:
        hits = [t for t in c["control_terms"] if t in corpus_tokens]
        assert not hits, f"{c['qid']}: control terms present in corpus: {hits}"


def test_generator_deterministic(gen_lib):
    assert gen_lib.corpus_hash(gen_lib.generate(900)) == EXPECTED_FILLER_900_SHA256
    assert gen_lib.corpus_hash(gen_lib.generate(9900)) == EXPECTED_FILLER_9900_SHA256
    # committed + filler keys never collide
    assert all(f["key"].startswith("f-") for f in gen_lib.generate(50))
