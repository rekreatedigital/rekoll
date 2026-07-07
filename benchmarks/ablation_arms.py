#!/usr/bin/env python
"""Lane 1c ablation + Lane 4 reranker depth/latency study over frozen semantic_v1.

LANE 1c (ablation): which retrieval legs earn their keep?
LANE 4 (reranker & fusion value): does the cross-encoder lift or hurt, and at
what latency cost, as candidate depth grows?

    python benchmarks/ablation_arms.py                                  # both lanes, decision settings
    python benchmarks/ablation_arms.py --mode ablation
    python benchmarks/ablation_arms.py --mode depth --depths 30,50,100,200

CONFIG ENUMERATION (Lane 1c, declared here in the harness BEFORE any run) —
the full 2x2x2 grid over (vector leg, lexical leg, cross-encoder reranker) has
8 raw combos. ``hybrid_search`` has NO ``use_lexical`` lever (by design: the
lexical leg runs whenever the adapter advertises CAP_LEXICAL), so:

- ``lexical-only`` / ``lexical-only+rerank`` use the shipped lever:
  ``hybrid_search(use_vector=False)``.
- ``vector-only`` / ``vector-only+rerank`` are HARNESS-COMPOSED (product source
  is read-only; no lever is added): ``adapter.vector_query`` called directly
  with the SAME query sanitation (``sanitize_unicode`` + ``MAX_QUERY_CHARS``)
  and the SAME candidate pool as the product path, then truncated/reranked to
  k. Documented divergence: the product's tamper-verify and quarantine filters
  are skipped — both are no-ops on this freshly ingested OWNER-only bench
  corpus (every record verifies; nothing is quarantined).
- ``hybrid-rrf`` / ``hybrid-rrf+rerank`` are the shipped default path.
- vector-off AND lexical-off is DEFINED-EMPTY by product contract (ADR-0024:
  "honestly empty rather than a garbage ranking" — retrieval.py returns
  ``QueryResult(hits=())``). Reranking an empty candidate list is the identity,
  so its two +-rerank raw combos COLLAPSE into ONE config. The collapse is
  ASSERTED at run time by driving the shipped code path (a harness-only
  capability shim reports CAP_LEXICAL off; ``use_vector=False``) with and
  without the reranker — not assumed.

  => 8 raw combos - 2 both-off variants + 1 collapsed defined-empty config
     = 7 valid configs: 6 measured + 1 defined-empty (scored as real all-zero
     per-query rows produced by the shipped empty path).

ADJACENT COMPARISONS (pre-declared): hybrid-rrf vs vector-only; hybrid-rrf vs
lexical-only; +-rerank at fixed legs (lexical, vector, hybrid) — each with
exact sign test + sign-flip permutation + win/loss/tie, on the paraphrase
subset and on all scored queries.

HONESTY GUARDS (hard-fail), same style as compare_arms.py:
- every quality config asserts embedder ``dim == 384`` and identity name is a
  fastembed model (never ``"stub-hash"``); the stub embedder is never
  instantiated anywhere in this harness;
- the fixture content hash is re-verified before scoring; at the pre-registered
  decision corpus size (1,000; seed 20260707 — registered by Lane 1b, not
  re-picked here) the filler sha256 must equal the value pinned in
  tests/test_semantic_fixture.py.

CONTAMINATION RULE (hard): the reranker (Xenova/ms-marco-MiniLM-L-6-v2) is
MS-MARCO-trained; semantic_v1 is synthetic and AI-authored — ZERO-SHOT for
both models. No MS-MARCO-derived data is ever evaluated by this harness, and
every emitted table block carries this label.

LATENCY (Lane 4 only): per-stage p50/p95 via timing wrappers around the
*injected* embedder/reranker (the product read path runs as shipped — nothing
is patched); ``retrieve+fuse`` is derived as end_to_end - embed - rerank and
labeled as derived. Latency numbers are environment-bound and NOT
bit-reproducible; the environment card is embedded in the output. Metric
numbers ARE deterministic given the fixed seeds.

Stats are pure stdlib, imported from compare_arms.py (percentile bootstrap,
exact sign test, sign-flip permutation — 10,000 resamples/permutations, seed
20260707, unchanged). The ``--size`` flag exists for harness smoke tests only;
any run at a non-decision size is marked ``"decision": false`` in the output.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import math
import os
import platform
import sys
import time
from pathlib import Path

from rekoll.adapters.base import CAP_LEXICAL
from rekoll.evaluation import LabeledQuery, evaluate
from rekoll.firewall import sanitize_unicode
from rekoll.retrieval import MAX_QUERY_CHARS, hybrid_search

HERE = Path(__file__).resolve().parent
FIXDIR = HERE / "fixtures"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


# Reuse the Lane-1b machinery additively (its CLI/outputs are untouched).
ca = _load_module("compare_arms", HERE / "compare_arms.py")

SCOPE = ca.SCOPE
SEED = ca.SEED  # 20260707
PRIMARY_METRICS = ca.PRIMARY_METRICS
METRIC_LABELS = ca.METRIC_LABELS

DECISION_CORPUS_SIZE = 1000
# Pinned in tests/test_semantic_fixture.py; re-asserted here at decision size.
EXPECTED_FILLER_900_SHA256 = "73f7eae8d34bf067e36b1033c6dd7d6ec28bede11ffc8e2ebcd720cfddf5b164"

CONTAMINATION_NOTE = (
    "ZERO-SHOT / NO CONTAMINATION: reranker Xenova/ms-marco-MiniLM-L-6-v2 is "
    "MS-MARCO-trained and embedder BAAI/bge-small-en-v1.5 is web-corpus-trained; "
    "semantic_v1 is synthetic, AI-authored (2026) — zero-shot for both models. "
    "No MS-MARCO-derived data is evaluated."
)

COMPARISON_SUBSETS = ("paraphrase", "all")  # pre-declared for all paired tests


# --------------------------------------------------------------------------
# harness-only shims (product source is READ-ONLY and runs as shipped)
# --------------------------------------------------------------------------


class _LexicalOff:
    """Capability shim: same adapter, CAP_LEXICAL reported off.

    Lets the SHIPPED ``hybrid_search`` defined-empty branch (ADR-0024) execute
    for real instead of being assumed. Everything else delegates.
    """

    def __init__(self, inner):
        self._inner = inner

    def supports(self, capability: str) -> bool:
        if capability == CAP_LEXICAL:
            return False
        return self._inner.supports(capability)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class TimingEmbedder:
    """Delegating embedder that records per-call wall time (injected dependency;
    the product path itself is untouched)."""

    def __init__(self, inner):
        self._inner = inner
        self.times: list[float] = []

    @property
    def dim(self) -> int:
        return self._inner.dim

    def identity(self):
        return self._inner.identity()

    def embed(self, texts):
        t0 = time.perf_counter()
        out = self._inner.embed(texts)
        self.times.append(time.perf_counter() - t0)
        return out


class TimingReranker:
    """Delegating reranker that records per-call wall time."""

    def __init__(self, inner):
        self._inner = inner
        self.times: list[float] = []

    @property
    def model_name(self) -> str:
        return self._inner.model_name

    def rerank(self, query, hits, *, top=None):
        t0 = time.perf_counter()
        out = self._inner.rerank(query, hits, top=top)
        self.times.append(time.perf_counter() - t0)
        return out


# --------------------------------------------------------------------------
# small stdlib helpers
# --------------------------------------------------------------------------


def pct(values: list[float], p: float) -> float:
    """Linear-interpolated percentile (p in [0,1]) over a non-empty list."""
    s = sorted(values)
    if not s:
        return 0.0
    idx = (len(s) - 1) * p
    lo, hi = math.floor(idx), math.ceil(idx)
    if lo == hi:
        return s[lo]
    return s[lo] + (s[hi] - s[lo]) * (idx - lo)


def lat_summary(seconds: list[float]) -> dict:
    ms = [v * 1000.0 for v in seconds]
    return {
        "p50_ms": round(pct(ms, 0.50), 2),
        "p95_ms": round(pct(ms, 0.95), 2),
        "mean_ms": round(sum(ms) / len(ms), 2),
        "min_ms": round(min(ms), 2),
        "max_ms": round(max(ms), 2),
    }


def _cpu_name() -> str:
    try:
        import winreg

        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            r"HARDWARE\DESCRIPTION\System\CentralProcessor\0",
        ) as key:
            return str(winreg.QueryValueEx(key, "ProcessorNameString")[0]).strip()
    except Exception:
        return platform.processor() or platform.machine()


def latency_env(warmup: int, n_timed: int) -> dict:
    def _ver(pkg: str) -> str:
        try:
            return importlib.metadata.version(pkg)
        except Exception:
            return "unknown"

    return {
        "cpu": _cpu_name(),
        "logical_cpus": os.cpu_count(),
        "os": platform.platform(),
        "python": platform.python_version(),
        "fastembed": _ver("fastembed"),
        "onnxruntime": _ver("onnxruntime"),
        "threads": "onnxruntime defaults (not pinned)",
        "batch": "1 query per call (the product read path shape)",
        "warmup_runs": warmup,
        "n_timed_queries": n_timed,
        "timer": "time.perf_counter",
        "note": (
            "latency is environment-bound and NOT bit-reproducible; "
            "metric numbers are deterministic given the fixed seeds"
        ),
    }


def subset_cis(rows, subsets: dict[str, list[int]], resamples: int) -> dict:
    out: dict = {}
    for sub_name, idxs in subsets.items():
        block = {}
        for metric in PRIMARY_METRICS + ("ndcg_at_k",):
            vals = [getattr(rows[i], metric) for i in idxs]
            ci = ca.bootstrap_ci(vals, resamples, SEED)
            if metric == "ndcg_at_k":
                ci["label"] = "diagnostic only (binary gold)"
            block[metric] = ci
        out[sub_name] = block
    return out


def paired_tests(rows_a, rows_b, subsets: dict[str, list[int]], permutations: int) -> dict:
    """rows_a minus rows_b, per pre-declared subset, per primary metric."""
    out: dict = {}
    for sub_name in COMPARISON_SUBSETS:
        idxs = subsets[sub_name]
        block = {}
        for metric in PRIMARY_METRICS:
            diffs = [
                getattr(rows_a[i], metric) - getattr(rows_b[i], metric) for i in idxs
            ]
            block[metric] = {
                "sign_test": ca.sign_test(diffs),
                "permutation": ca.permutation_test(diffs, permutations, SEED),
            }
        out[sub_name] = block
    return out


def per_query_dump(rows) -> dict:
    """Audit trail: per-query primary metrics in fixture query order."""
    return {
        metric: [round(getattr(r, metric), 6) for r in rows]
        for metric in PRIMARY_METRICS
    }


# --------------------------------------------------------------------------
# lanes
# --------------------------------------------------------------------------


def run_ablation(db, real, reranker, labeled, queries, subsets, args) -> dict:
    t0 = time.time()
    k = args.k
    pool = max(k * 6, k)  # product default candidate pool (retrieval.py)

    def hybrid_fn(use_vector: bool, rr, adapter=db):
        def fn(text: str):
            hits = hybrid_search(
                adapter, scope=SCOPE, query=text, embedder=real, k=k,
                reranker=rr, use_vector=use_vector,
            ).hits
            return [h.record.id for h in hits]
        return fn

    def vector_only_fn(rr):
        # Harness-composed leg (no use_lexical lever exists; none is added).
        # Same sanitation + pool as the product path; tamper/quarantine filters
        # skipped (no-ops on this fresh OWNER-only corpus).
        def fn(text: str):
            q = sanitize_unicode(text)[:MAX_QUERY_CHARS]
            vec = real.embed([q])[0]
            hits = list(db.vector_query(scope=SCOPE, embedding=vec, k=pool).hits)
            hits = rr.rerank(q, hits, top=k) if rr is not None else hits[:k]
            return [h.record.id for h in hits]
        return fn

    # Defined-empty collapse check: drive the SHIPPED empty branch +-rerank.
    shim = _LexicalOff(db)
    probe = queries[0]["query"]
    empty_plain = hybrid_fn(False, None, adapter=shim)(probe)
    empty_rr = hybrid_fn(False, reranker, adapter=shim)(probe)
    assert empty_plain == [] and empty_rr == [], (
        "ADR-0024 defined-empty contract violated: expected () with and without "
        f"reranker, got {len(empty_plain)}/{len(empty_rr)} hits"
    )

    enumeration = [
        {"id": "lexical-only", "legs": "vector OFF, lexical ON, rerank OFF",
         "path": "hybrid_search(use_vector=False) [shipped lever]", "status": "measured"},
        {"id": "lexical-only+rerank", "legs": "vector OFF, lexical ON, rerank ON",
         "path": "hybrid_search(use_vector=False, reranker=ce) [shipped lever]", "status": "measured"},
        {"id": "vector-only", "legs": "vector ON, lexical OFF, rerank OFF",
         "path": f"adapter.vector_query(k={pool}) direct [harness-composed; no use_lexical lever exists]",
         "status": "measured"},
        {"id": "vector-only+rerank", "legs": "vector ON, lexical OFF, rerank ON",
         "path": f"adapter.vector_query(k={pool}) -> CrossEncoderReranker.rerank(top={k}) [harness-composed]",
         "status": "measured"},
        {"id": "hybrid-rrf", "legs": "vector ON, lexical ON, rerank OFF",
         "path": "hybrid_search() [shipped default]", "status": "measured"},
        {"id": "hybrid-rrf+rerank", "legs": "vector ON, lexical ON, rerank ON",
         "path": "hybrid_search(reranker=ce) [shipped]", "status": "measured"},
        {"id": "defined-empty", "legs": "vector OFF, lexical OFF (+-rerank collapse asserted)",
         "path": "hybrid_search(use_vector=False) with CAP_LEXICAL off [shipped ADR-0024 branch via harness capability shim]",
         "status": "defined-empty (all metrics identically 0; not a quality row)"},
    ]

    configs = {
        "lexical-only": (hybrid_fn(False, None), "quality"),
        "lexical-only+rerank": (hybrid_fn(False, reranker), "quality"),
        "vector-only": (vector_only_fn(None), "quality"),
        "vector-only+rerank": (vector_only_fn(reranker), "quality"),
        "hybrid-rrf": (hybrid_fn(True, None), "quality"),
        "hybrid-rrf+rerank": (hybrid_fn(True, reranker), "quality"),
        "defined-empty": (hybrid_fn(False, None, adapter=shim), "defined-empty (never quality)"),
    }

    out: dict = {
        "config_card": {
            "embedder": {"name": real.identity().name, "dim": real.dim},
            "reranker": {"name": reranker.model_name, "depth": pool},
            "k": k, "candidate_pool": pool, "similarity": "cosine", "fusion": "RRF k=60",
            "chunking": "n/a (direct records)", "screen_redact": "OFF (adapter.add direct)",
            "kind": "RAW_FACT", "scope": "tenant=bench/project=bench/agent=bench",
            "trust_tier": "OWNER", "seeds": SEED,
            "ci": f"percentile bootstrap, {args.resamples} resamples",
            "paired_tests": f"exact sign test + sign-flip permutation ({args.permutations})",
            "dataset": "semantic_v1 (synthetic, AI-authored, MIT)",
            "contamination": CONTAMINATION_NOTE,
            "latency": "no latency numbers are claimed in the ablation lane",
        },
        "config_enumeration": {
            "justification": (
                "2x2x2 grid = 8 raw combos; vector-off AND lexical-off is "
                "defined-empty by product contract (ADR-0024) and reranking an "
                "empty list is the identity, so its two +-rerank variants "
                "collapse into one config (collapse asserted at run time): "
                "8 - 2 + 1 = 7 valid configs, 6 measured + 1 defined-empty."
            ),
            "configs": enumeration,
        },
        "empty_collapse_check": {
            "plain_hits": len(empty_plain), "rerank_hits": len(empty_rr), "collapsed": True,
        },
        "configs": {},
        "per_query": {},
        "comparisons": {},
    }

    rows_by_cfg: dict[str, list] = {}
    for name, (fn, label) in configs.items():
        if label == "quality":
            ca.guard_real_embedder(real)  # stub can never appear in a quality row
        res = evaluate(fn, labeled, k=k, per_query=True)
        rows = list(res.per_query)
        rows_by_cfg[name] = rows
        if name == "defined-empty":
            assert all(
                getattr(r, m) == 0.0 for r in rows for m in PRIMARY_METRICS
            ), "defined-empty config must score identically 0"
        out["configs"][name] = {"label": label, "subsets": subset_cis(rows, subsets, args.resamples)}
        out["per_query"][name] = per_query_dump(rows)
        para = out["configs"][name]["subsets"]["paraphrase"]["recall_at_k"]
        alls = out["configs"][name]["subsets"]["all"]["recall_at_k"]
        print(f"  {name:<22} recall@5 all={alls['mean']:.3f} [{alls['lo']:.3f},{alls['hi']:.3f}]"
              f"  paraphrase={para['mean']:.3f} [{para['lo']:.3f},{para['hi']:.3f}]  ({label})")

    adjacent = [
        ("hybrid-rrf", "vector-only"),
        ("hybrid-rrf", "lexical-only"),
        ("lexical-only+rerank", "lexical-only"),
        ("vector-only+rerank", "vector-only"),
        ("hybrid-rrf+rerank", "hybrid-rrf"),
    ]
    for a, b in adjacent:
        out["comparisons"][f"{a}_vs_{b}"] = paired_tests(
            rows_by_cfg[a], rows_by_cfg[b], subsets, args.permutations
        )

    out["wall_seconds"] = round(time.time() - t0, 1)
    return out


def run_depth_sweep(db, real, reranker, labeled, queries, subsets, args) -> dict:
    t0 = time.time()
    k = args.k
    depths = [int(d) for d in args.depths.split(",")]
    depth_subsets = {name: subsets[name] for name in COMPARISON_SUBSETS}

    out: dict = {
        "config_card": {
            "embedder": {"name": real.identity().name, "dim": real.dim},
            "reranker": {"name": reranker.model_name, "depth": "= candidates (rerank depth == fused pool)"},
            "k": k, "depths": depths, "similarity": "cosine", "fusion": "RRF k=60",
            "path": "hybrid_search(candidates=N[, reranker=ce]) — shipped kwarg, no product change",
            "kind": "RAW_FACT", "scope": "tenant=bench/project=bench/agent=bench",
            "trust_tier": "OWNER", "seeds": SEED,
            "ci": f"percentile bootstrap, {args.resamples} resamples",
            "paired_tests": f"exact sign test + sign-flip permutation ({args.permutations})",
            "dataset": "semantic_v1 (synthetic, AI-authored, MIT)",
            "contamination": CONTAMINATION_NOTE,
            "latency_env": latency_env(args.warmup, len(labeled)),
            "latency_stages": (
                "embed & rerank measured via injected timing wrappers; "
                "retrieve_fuse = end_to_end - embed - rerank (DERIVED); "
                "end_to_end wraps the whole hybrid_search call"
            ),
        },
        "depths": {},
        "frontier": [],
    }

    rows_cache: dict[tuple[int, bool], list] = {}
    for depth in depths:
        depth_out: dict = {}
        for use_rr in (False, True):
            arm = "rerank" if use_rr else "no-rerank"
            ca.guard_real_embedder(real)
            temb = TimingEmbedder(real)
            trr = TimingReranker(reranker) if use_rr else None
            e2e: list[float] = []

            def fn(text: str):
                t = time.perf_counter()
                hits = hybrid_search(
                    db, scope=SCOPE, query=text, embedder=temb, k=k,
                    reranker=trr, candidates=depth,
                ).hits
                e2e.append(time.perf_counter() - t)
                return [h.record.id for h in hits]

            for q in queries[: args.warmup]:  # warmup (untimed)
                fn(q["query"])
            temb.times.clear()
            if trr is not None:
                trr.times.clear()
            e2e.clear()

            res = evaluate(fn, labeled, k=k, per_query=True)
            rows = list(res.per_query)
            rows_cache[(depth, use_rr)] = rows
            n = len(rows)
            assert len(e2e) == n and len(temb.times) == n
            rer = list(trr.times) if trr is not None else [0.0] * n
            assert len(rer) == n
            fuse = [e2e[i] - temb.times[i] - rer[i] for i in range(n)]

            metrics = subset_cis(rows, depth_subsets, args.resamples)
            latency = {
                "end_to_end": lat_summary(e2e),
                "embed": lat_summary(temb.times),
                "rerank": lat_summary(rer) if use_rr else {"note": "stage absent"},
                "retrieve_fuse_derived": lat_summary(fuse),
                "per_query_ms": {
                    "end_to_end": [round(v * 1000, 3) for v in e2e],
                    "embed": [round(v * 1000, 3) for v in temb.times],
                    "rerank": [round(v * 1000, 3) for v in rer],
                },
            }
            depth_out[arm] = {"metrics": metrics, "latency": latency,
                              "per_query": per_query_dump(rows)}

            para = metrics["paraphrase"]["recall_at_k"]
            alls = metrics["all"]["recall_at_k"]
            mrr_p = metrics["paraphrase"]["reciprocal_rank"]
            print(f"  depth={depth:<4} {arm:<10} recall@5 para={para['mean']:.3f} "
                  f"[{para['lo']:.3f},{para['hi']:.3f}] all={alls['mean']:.3f} "
                  f"MRR para={mrr_p['mean']:.3f}  e2e p50={latency['end_to_end']['p50_ms']}ms "
                  f"p95={latency['end_to_end']['p95_ms']}ms")

            out["frontier"].append({
                "depth": depth, "arm": arm,
                "recall5_para": round(para["mean"], 3),
                "recall5_para_ci": [round(para["lo"], 3), round(para["hi"], 3)],
                "mrr_para": round(mrr_p["mean"], 3),
                "recall5_all": round(alls["mean"], 3),
                "mrr_all": round(metrics["all"]["reciprocal_rank"]["mean"], 3),
                "e2e_p50_ms": latency["end_to_end"]["p50_ms"],
                "e2e_p95_ms": latency["end_to_end"]["p95_ms"],
                "rerank_p50_ms": latency["rerank"]["p50_ms"] if use_rr else 0.0,
            })

        depth_out["rerank_vs_no-rerank"] = paired_tests(
            rows_cache[(depth, True)], rows_cache[(depth, False)],
            subsets, args.permutations,
        )
        out["depths"][str(depth)] = depth_out

    base = depths[0]
    for depth in depths[1:]:
        out["depths"][str(depth)][f"rerank_depth{depth}_vs_depth{base}"] = paired_tests(
            rows_cache[(depth, True)], rows_cache[(base, True)],
            subsets, args.permutations,
        )

    out["wall_seconds"] = round(time.time() - t0, 1)
    return out


# --------------------------------------------------------------------------


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="semantic_v1 ablation + reranker depth study (Lanes 1c & 4)")
    ap.add_argument("--fixture", default=str(FIXDIR / "semantic_v1.json"))
    ap.add_argument("--mode", choices=("all", "ablation", "depth"), default="all")
    ap.add_argument("--size", type=int, default=DECISION_CORPUS_SIZE,
                    help="corpus size (decision size is pre-registered at 1000; anything else marks the run non-decision/smoke)")
    ap.add_argument("-k", type=int, default=5)
    ap.add_argument("--resamples", type=int, default=10_000)
    ap.add_argument("--permutations", type=int, default=10_000)
    ap.add_argument("--depths", default="30,50,100,200")
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--out", default=str(FIXDIR / "results_ablation_v1.json"))
    args = ap.parse_args()

    fixture = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    errors = ca.verify_lib.verify(fixture)
    if errors:
        raise SystemExit("fixture failed verification: " + "; ".join(errors))

    from rekoll.embedding import FastEmbedEmbedder
    from rekoll.reranking import CrossEncoderReranker

    real = FastEmbedEmbedder()
    ca.guard_real_embedder(real)
    reranker = CrossEncoderReranker()

    committed_docs = fixture["documents"]
    queries = fixture["queries"]
    n_filler = args.size - len(committed_docs)
    filler = ca.gen_lib.generate(n_filler) if n_filler > 0 else []
    filler_hash = ca.gen_lib.corpus_hash(filler) if filler else None
    decision = args.size == DECISION_CORPUS_SIZE
    if decision:
        assert filler_hash == EXPECTED_FILLER_900_SHA256, (
            f"decision-corpus filler drifted: {filler_hash} != pinned {EXPECTED_FILLER_900_SHA256}"
        )
    corpus = committed_docs + [{"key": f["key"], "text": f["text"]} for f in filler]
    assert len(corpus) == args.size

    print(f"building corpus={args.size} DB with {real.identity().name} "
          f"({'DECISION size' if decision else 'NON-DECISION smoke size'})")
    db, keymap = ca.build_db(real, corpus)
    labeled = [
        LabeledQuery(query=q["query"], relevant_ids=frozenset(keymap[r] for r in q["relevant"]))
        for q in queries
    ]
    subsets = {
        "all": [i for i, q in enumerate(queries)],
        "paraphrase": [i for i, q in enumerate(queries) if q["paraphrase"]],
        "low": [i for i, q in enumerate(queries) if q["bucket"] == "low"],
        "med": [i for i, q in enumerate(queries) if q["bucket"] == "med"],
        "high": [i for i, q in enumerate(queries) if q["bucket"] == "high"],
        "multi_gold": [i for i, q in enumerate(queries) if len(q["relevant"]) >= 3],
    }

    results: dict = {
        "protocol": (
            "benchmarks/fixtures/PREREGISTRATION_semantic_v1.md (fixture, metrics, stats, "
            "decision corpus size) + the ablation_arms.py module docstring "
            "(config enumeration + adjacent comparisons, declared before any run)"
        ),
        "invocation": " ".join(["python"] + sys.argv),
        "decision": decision,
        "fixture": {"name": fixture["name"], "version": fixture["version"],
                    "content_sha256": fixture["metadata"]["content_sha256"],
                    "n_scored": len(queries), "n_controls": len(fixture["controls"])},
        "corpus": {"size": args.size, "filler_sha256": filler_hash, "seed": SEED},
        "contamination": CONTAMINATION_NOTE,
    }

    if args.mode in ("all", "ablation"):
        print(f"\n=== LANE 1c ablation (corpus={args.size}, k={args.k}) ===")
        results["ablation"] = run_ablation(db, real, reranker, labeled, queries, subsets, args)
    if args.mode in ("all", "depth"):
        print(f"\n=== LANE 4 depth sweep (corpus={args.size}, depths={args.depths}) ===")
        results["depth_sweep"] = run_depth_sweep(db, real, reranker, labeled, queries, subsets, args)

    db.close()
    out_path = Path(args.out)
    out_path.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"\nresults written to {out_path}")


if __name__ == "__main__":
    main()
