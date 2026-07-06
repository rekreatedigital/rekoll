#!/usr/bin/env python
"""Arm comparison over the frozen semantic_v1 fixture (Lane 1b, pre-registered).

Scores four retrieval arms on identical corpus content, stratified by
lexical-overlap bucket and by the paraphrase flag, with percentile-bootstrap
95% CIs and paired significance tests — everything pre-registered in
benchmarks/fixtures/PREREGISTRATION_semantic_v1.md BEFORE any run.

    python benchmarks/compare_arms.py                       # full sweep
    python benchmarks/compare_arms.py --sizes 1000          # one corpus size

Arms:
    stub-hybrid              StubEmbedder + FTS5/BM25 via RRF   [PIPELINE-ONLY]
    bm25-only                hybrid_search(use_vector=False)
    fastembed-hybrid         real embedder + FTS5/BM25 via RRF  [under test]
    fastembed-hybrid-rerank  + cross-encoder                     [diagnostic]

HONESTY GUARDS (hard-fail):
- quality arms assert dim==384 and identity name is a fastembed model (never
  "stub-hash") — a stub number can never be presented as quality;
- the fixture content hash and the generated-filler hashes are re-verified
  before any scoring.

Stats are pure stdlib (no numpy/scipy). All seeds fixed (20260707). The
product read path (rekoll.retrieval.hybrid_search / adapter.vector_query) is
exercised as shipped — nothing is patched.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import math
import random
import statistics
import sys
import time
from pathlib import Path

from rekoll import Kind, MemoryRecord, Provenance, Scope, StubEmbedder, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.evaluation import LabeledQuery, evaluate
from rekoll.retrieval import hybrid_search

HERE = Path(__file__).resolve().parent
FIXDIR = HERE / "fixtures"
SCOPE = Scope(tenant="bench", project="bench", agent="bench")
SEED = 20260707

PRIMARY_METRICS = ("recall_at_k", "reciprocal_rank", "hit_rate_at_k")
METRIC_LABELS = {"recall_at_k": "recall@5", "reciprocal_rank": "MRR", "hit_rate_at_k": "hit-rate@5"}


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


verify_lib = _load_module("verify_semantic_v1", FIXDIR / "verify_semantic_v1.py")
gen_lib = _load_module("gen_distractors", FIXDIR / "gen_distractors.py")


def guard_real_embedder(embedder) -> None:
    """Pre-registered stub-vs-real guard: quality numbers require the real model."""
    ident = embedder.identity()
    assert embedder.dim == 384, f"quality arm requires dim=384, got {embedder.dim}"
    assert ident.name != "stub-hash", "stub embedder in a quality arm — REJECT"
    assert ident.name.startswith("fastembed:"), f"unexpected embedder {ident.name!r}"


def build_db(embedder, documents: list[dict]) -> tuple[SQLiteAdapter, dict[str, str]]:
    """Ingest documents (single batch embed) via adapter.add — screen/redact OFF,
    chunking n/a: content is stored verbatim as one record per doc."""
    db = SQLiteAdapter(":memory:")
    texts = [d["text"] for d in documents]
    vecs = embedder.embed(texts)
    name, dim = embedder.identity().name, embedder.dim
    keymap: dict[str, str] = {}
    records = []
    for d, vec in zip(documents, vecs):
        rec = MemoryRecord.create(
            scope=SCOPE, kind=Kind.RAW_FACT, content=d["text"],
            provenance=Provenance(source_uri=f"doc://{d['key']}"), trust_tier=TrustTier.OWNER,
        )
        rec.with_embedding(vec, name=name, dim=dim)
        keymap[d["key"]] = rec.id
        records.append(rec)
    db.add(records=records)
    return db, keymap


# --------------------------------------------------------------------------
# stdlib statistics: bootstrap CI, paired sign test, sign-flip permutation
# --------------------------------------------------------------------------

def bootstrap_ci(values: list[float], resamples: int, seed: int) -> dict:
    n = len(values)
    mean = sum(values) / n if n else 0.0
    if n == 0:
        return {"n": 0, "mean": 0.0, "lo": 0.0, "hi": 0.0}
    rng = random.Random(seed)
    means = []
    for _ in range(resamples):
        s = 0.0
        for _ in range(n):
            s += values[rng.randrange(n)]
        means.append(s / n)
    means.sort()
    lo = means[int(0.025 * (resamples - 1))]
    hi = means[int(0.975 * (resamples - 1))]
    return {"n": n, "mean": mean, "lo": lo, "hi": hi}


def sign_test(diffs: list[float]) -> dict:
    wins = sum(1 for d in diffs if d > 0)
    losses = sum(1 for d in diffs if d < 0)
    ties = len(diffs) - wins - losses
    m = wins + losses
    if m == 0:
        return {"wins": wins, "losses": losses, "ties": ties, "p": 1.0}
    cdf_lo = sum(math.comb(m, i) for i in range(0, min(wins, losses) + 1)) / 2**m
    p = min(1.0, 2 * cdf_lo)
    return {"wins": wins, "losses": losses, "ties": ties, "p": p}


def permutation_test(diffs: list[float], permutations: int, seed: int) -> dict:
    n = len(diffs)
    if n == 0:
        return {"mean_diff": 0.0, "p": 1.0}
    obs = abs(sum(diffs) / n)
    rng = random.Random(seed)
    hits = 0
    for _ in range(permutations):
        s = 0.0
        for d in diffs:
            s += d if rng.random() < 0.5 else -d
        if abs(s / n) >= obs - 1e-15:
            hits += 1
    return {"mean_diff": sum(diffs) / n, "p": (hits + 1) / (permutations + 1)}


# --------------------------------------------------------------------------


def rank_auc(pos: list[float], neg: list[float]) -> float:
    """Mann-Whitney AUC: P(pos > neg) + 0.5 P(tie)."""
    if not pos or not neg:
        return float("nan")
    wins = ties = 0
    for p in pos:
        for q in neg:
            if p > q:
                wins += 1
            elif p == q:
                ties += 1
    return (wins + 0.5 * ties) / (len(pos) * len(neg))


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="semantic_v1 arm comparison (pre-registered)")
    ap.add_argument("--fixture", default=str(FIXDIR / "semantic_v1.json"))
    ap.add_argument("--sizes", default="100,1000,10000")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--resamples", type=int, default=10_000)
    ap.add_argument("--permutations", type=int, default=10_000)
    ap.add_argument("--out", default=str(FIXDIR / "results_semantic_v1.json"))
    args = ap.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    errors = verify_lib.verify(fixture)
    if errors:
        raise SystemExit("fixture failed verification: " + "; ".join(errors))

    from rekoll.embedding import FastEmbedEmbedder
    from rekoll.reranking import CrossEncoderReranker

    stub = StubEmbedder()
    real = FastEmbedEmbedder()
    guard_real_embedder(real)
    reranker = CrossEncoderReranker()

    committed_docs = fixture["documents"]
    queries = fixture["queries"]
    controls = fixture["controls"]
    sizes = [int(s) for s in args.sizes.split(",")]

    subsets = {
        "all": [i for i, q in enumerate(queries)],
        "paraphrase": [i for i, q in enumerate(queries) if q["paraphrase"]],
        "low": [i for i, q in enumerate(queries) if q["bucket"] == "low"],
        "med": [i for i, q in enumerate(queries) if q["bucket"] == "med"],
        "high": [i for i, q in enumerate(queries) if q["bucket"] == "high"],
        "multi_gold": [i for i, q in enumerate(queries) if len(q["relevant"]) >= 3],
    }

    results: dict = {
        "protocol": "benchmarks/fixtures/PREREGISTRATION_semantic_v1.md",
        "fixture": {"name": fixture["name"], "version": fixture["version"],
                    "content_sha256": fixture["metadata"]["content_sha256"],
                    "n_scored": len(queries), "n_controls": len(controls)},
        "config_card": {
            "embedder": {"name": real.identity().name, "dim": real.dim, "fastembed_version": "0.8.0"},
            "stub_embedder": {"name": stub.identity().name, "dim": stub.dim, "label": "PIPELINE-ONLY"},
            "reranker": {"name": reranker.model_name, "depth": max(args.k * 6, args.k)},
            "k": args.k, "similarity": "cosine", "fusion": "RRF k=60",
            "chunking": "n/a (direct records)", "screen_redact": "OFF (adapter.add direct)",
            "kind": "RAW_FACT", "scope": "tenant=bench/project=bench/agent=bench",
            "trust_tier": "OWNER",
            "seeds": SEED, "ci": f"percentile bootstrap, {args.resamples} resamples",
            "paired_tests": f"exact sign test + sign-flip permutation ({args.permutations})",
            "dataset": "semantic_v1 (synthetic, AI-authored, MIT)",
            "env": "Python 3.12.6, Windows 11, CPU-only",
        },
        "sizes": {},
    }

    for size in sizes:
        t0 = time.time()
        filler = gen_lib.generate(size - len(committed_docs)) if size > len(committed_docs) else []
        filler_hash = gen_lib.corpus_hash(filler) if filler else None
        corpus = committed_docs + [{"key": f["key"], "text": f["text"]} for f in filler]
        assert len(corpus) == size

        stub_db, stub_map = build_db(stub, corpus)
        real_db, real_map = build_db(real, corpus)

        def make_labeled(keymap):
            return [
                LabeledQuery(query=q["query"],
                             relevant_ids=frozenset(keymap[k] for k in q["relevant"]))
                for q in queries
            ]

        def searcher(db, embedder, use_vector=True, rr=None):
            def fn(text: str):
                hits = hybrid_search(
                    db, scope=SCOPE, query=text, embedder=embedder, k=args.k,
                    reranker=rr, use_vector=use_vector,
                ).hits
                return [h.record.id for h in hits]
            return fn

        arms = {
            "stub-hybrid": (stub_db, stub_map, searcher(stub_db, stub)),
            "bm25-only": (real_db, real_map, searcher(real_db, real, use_vector=False)),
            "fastembed-hybrid": (real_db, real_map, searcher(real_db, real)),
            "fastembed-hybrid-rerank": (real_db, real_map, searcher(real_db, real, rr=reranker)),
        }

        size_res: dict = {"filler_sha256": filler_hash, "arms": {}, "comparisons": {}}
        rows_by_arm: dict[str, list] = {}
        for arm_name, (db, keymap, fn) in arms.items():
            if arm_name.startswith("fastembed") or arm_name == "bm25-only":
                guard_real_embedder(real)  # quality arms run against real-embedder DB
            res = evaluate(fn, make_labeled(keymap), k=args.k, per_query=True)
            rows_by_arm[arm_name] = list(res.per_query)
            arm_out = {"label": "PIPELINE-ONLY (never quality)" if arm_name == "stub-hybrid" else "quality",
                       "subsets": {}}
            for sub_name, idxs in subsets.items():
                sub = {}
                for metric in PRIMARY_METRICS + ("ndcg_at_k",):
                    vals = [getattr(res.per_query[i], metric) for i in idxs]
                    ci = bootstrap_ci(vals, args.resamples, SEED)
                    if metric == "ndcg_at_k":
                        ci["label"] = "diagnostic only (binary gold)"
                    sub[metric] = ci
                arm_out["subsets"][sub_name] = sub
            size_res["arms"][arm_name] = arm_out

        # paired comparisons (pre-registered): fastembed-hybrid vs bm25-only / stub-hybrid
        for base in ("bm25-only", "stub-hybrid"):
            comp: dict = {}
            for sub_name in ("paraphrase", "low", "med", "high", "all"):
                idxs = subsets[sub_name]
                comp[sub_name] = {}
                for metric in PRIMARY_METRICS:
                    diffs = [
                        getattr(rows_by_arm["fastembed-hybrid"][i], metric)
                        - getattr(rows_by_arm[base][i], metric)
                        for i in idxs
                    ]
                    comp[sub_name][metric] = {
                        "sign_test": sign_test(diffs),
                        "permutation": permutation_test(diffs, args.permutations, SEED),
                    }
            size_res["comparisons"][f"fastembed-hybrid_vs_{base}"] = comp

        # negative controls: no abstain path exists — report what comes back anyway
        ctrl_fn = arms["fastembed-hybrid"][2]
        n_hits = [len(ctrl_fn(c["query"])) for c in controls]
        size_res["controls"] = {
            "note": "controls are EXCLUDED from all metric means; the product has no abstain path, so k hits come back regardless",
            "mean_hits_returned": sum(n_hits) / len(n_hits),
        }

        # abstention diagnostic (pre-registered at corpus=1000)
        if size == 1000:
            def top1(text: str) -> float:
                vec = real.embed([text])[0]
                hits = real_db.vector_query(scope=SCOPE, embedding=vec, k=1).hits
                return float(hits[0].score) if hits else 0.0

            pos = [top1(q["query"]) for q in queries]
            neg = [top1(c["query"]) for c in controls]
            grid = [0.40, 0.45, 0.50, 0.55, 0.60, 0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95]
            size_res["abstention_diagnostic"] = {
                "label": ("diagnostic — no abstain path exists in the product; "
                          "routed to fix-orchestrator"),
                "score": "top-1 cosine (adapter.vector_query, real embedder)",
                "answerable": {"n": len(pos), "mean": statistics.mean(pos),
                               "min": min(pos), "max": max(pos)},
                "control": {"n": len(neg), "mean": statistics.mean(neg),
                            "min": min(neg), "max": max(neg)},
                "auc": rank_auc(pos, neg),
                "sweep": [
                    {"tau": t,
                     "answerable_accept": sum(1 for s in pos if s >= t) / len(pos),
                     "control_accept": sum(1 for s in neg if s >= t) / len(neg)}
                    for t in grid
                ],
            }

        size_res["wall_seconds"] = round(time.time() - t0, 1)
        results["sizes"][str(size)] = size_res
        stub_db.close()
        real_db.close()

        # console summary
        print(f"\n=== corpus={size} (filler sha256={str(filler_hash)[:12]}) ===")
        for arm_name, arm_out in size_res["arms"].items():
            para = arm_out["subsets"]["paraphrase"]["recall_at_k"]
            alls = arm_out["subsets"]["all"]["recall_at_k"]
            print(f"  {arm_name:<26} recall@5 all={alls['mean']:.3f} [{alls['lo']:.3f},{alls['hi']:.3f}]"
                  f"  paraphrase={para['mean']:.3f} [{para['lo']:.3f},{para['hi']:.3f}]"
                  f"  ({arm_out['label']})")

    out_path = Path(args.out)
    out_path.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nresults written to {out_path}")


if __name__ == "__main__":
    main()
