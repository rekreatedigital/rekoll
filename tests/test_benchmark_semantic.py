"""Ratcheted REAL-embedder quality gate over the frozen semantic fixture.

This is the semantic counterpart of tests/test_benchmark_recall.py — but where
that file gates the PIPELINE on the stub embedder (keyword-distinct fixture,
scores ~1.0 by construction), this one gates QUALITY on the real local model,
so a fastembed upgrade, a fusion change, or a retrieval regression that erodes
semantic recall fails loudly instead of silently.

Opt-in by construction: ``pytest.importorskip("fastembed")`` at module import
means the default (no-extra) matrix collects this file and SKIPS it — never
errors. It runs wherever the ``embeddings``/``bench`` extra is installed: the
``test-embeddings`` CI job (which caches the ONNX model between runs) and any
dev machine with the extra.

Scope: corpus = the 100 COMMITTED docs of ``benchmarks/fixtures/semantic_v1.json``
(no generated filler — the gate exercises exactly the hash-frozen data), all
118 scored queries, k=5, hybrid vector+lexical, no reranker. Negative controls
are excluded from means, per the fixture's pre-registration.

RATCHET RULE (same contract as the stub gate): floors may be RAISED when the
real number durably improves; they are never silently lowered. A genuine model
regression that drops below floor is a finding, not a reason to edit the floor.

Floor derivation (ADR-0011 addendum; efficacy program Lane 1d): baseline mean
minus one bootstrap half-width, measured at main@574ad7f with fastembed 0.8.0 /
BAAI/bge-small-en-v1.5 (dim 384), 10,000 resamples, seed 20260707:

    recall@5 = 0.9124, CI [0.8588, 0.9576] -> half-width 0.0494 -> floor 0.86
    MRR      = 0.8679, CI [0.8107, 0.9188] -> half-width 0.0540 -> floor 0.81

(Floors are rounded DOWN to two decimals: the sliver of extra headroom absorbs
cross-platform ONNX numeric noise, which is far smaller than a real regression.)
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

# Default (no-extra) matrix: SKIP, never error. Deliberately try/except rather
# than pytest.importorskip: importorskip only skips a MISSING module — a
# present-but-broken fastembed (or a test shim) raises ImportError during its
# own import and would turn collection into an ERROR on pytest >= 8.2.
try:
    import fastembed  # noqa: F401
except ImportError:
    pytest.skip(
        "optional extra not installed: pip install 'rekoll[embeddings]'",
        allow_module_level=True,
    )

from rekoll import Kind, MemoryRecord, Provenance, Scope, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.embedding import FastEmbedEmbedder
from rekoll.evaluation import LabeledQuery, evaluate
from rekoll.retrieval import hybrid_search

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "fixtures" / "semantic_v1.json"

# Raise these when the real number durably improves; NEVER silently lower them.
BASELINE_RECALL_AT_5_REAL = 0.86
BASELINE_MRR_REAL = 0.81


def test_real_embedder_semantic_gate():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))

    embedder = FastEmbedEmbedder()
    identity = embedder.identity()
    # The stub-vs-real honesty guard: a quality gate must never score the stub.
    assert identity.name != "stub-hash", "quality gate resolved the stub embedder"
    assert embedder.dim == 384, f"unexpected embedder dim {embedder.dim}"

    scope = Scope(tenant="bench", project="bench", agent="bench")
    db = SQLiteAdapter(":memory:")
    keymap: dict[str, str] = {}
    records = []
    texts = [doc["text"] for doc in data["documents"]]
    vectors = embedder.embed(texts)
    for doc, vector in zip(data["documents"], vectors):
        record = MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content=doc["text"],
            provenance=Provenance(source_uri=f"doc://{doc['key']}"),
            trust_tier=TrustTier.OWNER,
        )
        record.with_embedding(vector, name=identity.name, dim=embedder.dim)
        keymap[doc["key"]] = record.id
        records.append(record)
    db.add(records=records)

    # Scored queries only: negative controls are excluded from means by the
    # fixture's pre-registration (their gold set is empty by design).
    labeled = [
        LabeledQuery(
            query=q["query"],
            relevant_ids=frozenset(keymap[key] for key in q["relevant"]),
        )
        for q in data["queries"]
        if not q.get("negative_control", False)
    ]
    assert len(labeled) >= 100, "headline bar: >=100 scored queries"

    def search_fn(query: str):
        hits = hybrid_search(db, scope=scope, query=query, embedder=embedder, k=5).hits
        return [h.record.id for h in hits]

    result = evaluate(search_fn, labeled, k=5)
    assert result.recall_at_k >= BASELINE_RECALL_AT_5_REAL, (
        f"REAL-embedder semantic recall regressed below the ratchet: {result} "
        f"(floor {BASELINE_RECALL_AT_5_REAL}; raise the floor only for durable "
        f"improvements, never lower it for a regression)"
    )
    assert result.mrr >= BASELINE_MRR_REAL, (
        f"REAL-embedder semantic MRR regressed below the ratchet: {result} "
        f"(floor {BASELINE_MRR_REAL})"
    )
    db.close()
