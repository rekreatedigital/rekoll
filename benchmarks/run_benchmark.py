#!/usr/bin/env python
"""Run the retrieval benchmark over a fixture.

Stub embedder by default (fast, deterministic, no network). Use --fastembed for
real semantic numbers and --rerank to add the cross-encoder. Reports Recall@k and
MRR — the same metrics the CI gate enforces (ADR-0011).

    python benchmarks/run_benchmark.py                     # stub baseline
    python benchmarks/run_benchmark.py --fastembed         # real embeddings
    python benchmarks/run_benchmark.py --fastembed --rerank
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from rekoll import Kind, MemoryRecord, Provenance, Scope, StubEmbedder, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.evaluation import LabeledQuery, evaluate
from rekoll.retrieval import hybrid_search

HERE = Path(__file__).resolve().parent
SCOPE = Scope(tenant="bench", project="bench", agent="bench")


def _build(embedder, documents):
    db = SQLiteAdapter(":memory:")
    keymap: dict[str, str] = {}
    records = []
    for doc in documents:
        record = MemoryRecord.create(
            scope=SCOPE, kind=Kind.RAW_FACT, content=doc["text"],
            provenance=Provenance(source_uri=f"doc://{doc['key']}"), trust_tier=TrustTier.OWNER,
        )
        record.with_embedding(
            embedder.embed([doc["text"]])[0], name=embedder.identity().name, dim=embedder.dim
        )
        keymap[doc["key"]] = record.id
        records.append(record)
    db.add(records=records)
    return db, keymap


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="Rekoll retrieval benchmark")
    ap.add_argument("--fixture", default=str(HERE / "recall_smoke.json"))
    ap.add_argument("--fastembed", action="store_true", help="use the real local embedder")
    ap.add_argument("--rerank", action="store_true", help="add the cross-encoder reranker")
    ap.add_argument("-k", type=int, default=5)
    args = ap.parse_args()

    data = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    documents, queries = data["documents"], data["queries"]

    if args.fastembed:
        from rekoll.embedding import FastEmbedEmbedder

        embedder = FastEmbedEmbedder()
    else:
        embedder = StubEmbedder()

    reranker = None
    if args.rerank:
        from rekoll.reranking import CrossEncoderReranker

        reranker = CrossEncoderReranker()

    db, keymap = _build(embedder, documents)
    labeled = [
        LabeledQuery(query=q["query"], relevant_ids=frozenset(keymap[k] for k in q["relevant"]))
        for q in queries
    ]

    def search_fn(query: str):
        hits = hybrid_search(
            db, scope=SCOPE, query=query, embedder=embedder, k=args.k, reranker=reranker
        ).hits
        return [h.record.id for h in hits]

    result = evaluate(search_fn, labeled, k=args.k)
    rerank_label = "on" if reranker else "off"
    print(f"fixture={data.get('name', args.fixture)}  embedder={embedder.identity().name}  rerank={rerank_label}")
    print(result)
    db.close()


if __name__ == "__main__":
    main()
