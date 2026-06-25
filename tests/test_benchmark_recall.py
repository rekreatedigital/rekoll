"""P1: retrieval recall gate over a committed fixture (stub embedder, no network).

This is a PIPELINE regression gate, not a model-quality benchmark: the fixture is
keyword-distinct so the stub scores perfectly, and any break in scope filtering,
fusion, or the adapter drops recall/MRR and fails CI. Real semantic numbers (where
fastembed + rerank differentiate) come from benchmarks/run_benchmark.py and, next,
a LongMemEval subset (ADR-0011).
"""

from __future__ import annotations

import json
from pathlib import Path

from rekoll import Kind, MemoryRecord, Provenance, Scope, StubEmbedder, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.evaluation import LabeledQuery, evaluate, recall_at_k, reciprocal_rank
from rekoll.retrieval import hybrid_search

FIXTURE = Path(__file__).resolve().parents[1] / "benchmarks" / "recall_smoke.json"

# Committed baselines for the stub embedder on the smoke fixture. Observed 1.0/1.0;
# the gate fails if a regression drops below these. Raise them, never silently lower.
BASELINE_RECALL_AT_5 = 0.9
BASELINE_MRR = 0.85


def test_metric_helpers():
    assert recall_at_k(["a", "b", "c"], frozenset({"b"}), 5) == 1.0
    assert recall_at_k(["a", "b", "c"], frozenset({"z"}), 5) == 0.0
    assert recall_at_k(["a", "b", "c", "d"], frozenset({"a", "z"}), 5) == 0.5
    assert reciprocal_rank(["a", "b", "c"], frozenset({"b"})) == 0.5
    assert reciprocal_rank(["a", "b"], frozenset({"z"})) == 0.0


def test_recall_gate_over_fixture():
    data = json.loads(FIXTURE.read_text(encoding="utf-8"))
    embedder = StubEmbedder()
    db = SQLiteAdapter(":memory:")
    scope = Scope(tenant="bench", project="bench", agent="bench")

    keymap: dict[str, str] = {}
    records = []
    for doc in data["documents"]:
        record = MemoryRecord.create(
            scope=scope, kind=Kind.RAW_FACT, content=doc["text"],
            provenance=Provenance(source_uri=f"doc://{doc['key']}"), trust_tier=TrustTier.OWNER,
        )
        record.with_embedding(
            embedder.embed([doc["text"]])[0], name=embedder.identity().name, dim=embedder.dim
        )
        keymap[doc["key"]] = record.id
        records.append(record)
    db.add(records=records)

    labeled = [
        LabeledQuery(query=q["query"], relevant_ids=frozenset(keymap[k] for k in q["relevant"]))
        for q in data["queries"]
    ]

    def search_fn(query: str):
        hits = hybrid_search(db, scope=scope, query=query, embedder=embedder, k=5).hits
        return [h.record.id for h in hits]

    result = evaluate(search_fn, labeled, k=5)
    assert result.recall_at_k >= BASELINE_RECALL_AT_5, f"recall regressed: {result}"
    assert result.mrr >= BASELINE_MRR, f"MRR regressed: {result}"
    db.close()
