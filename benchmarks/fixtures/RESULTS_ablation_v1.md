# RESULTS — retrieval-leg ablation & reranker depth study (Lanes 1c & 4)

**Protocol:** fixture, metrics, statistics, and the decision corpus size
(1,000; seed 20260707) were pre-registered in
[PREREGISTRATION_semantic_v1.md](PREREGISTRATION_semantic_v1.md) by Lane 1b
and are reused unchanged (the decision size was NOT re-picked). The config
enumeration, adjacent-comparison list, and depth grid for these two lanes are
declared in the module docstring of `benchmarks/ablation_arms.py`, committed
before the decision run. Fixture frozen at content_sha256
`8580fe4bcce5415a7cdfcbbe534b08ffbcbd07b13d266317620d8442591a1a58`; decision
filler (n=900, seed 20260707) sha256
`73f7eae8d34bf067e36b1033c6dd7d6ec28bede11ffc8e2ebcd720cfddf5b164`
(re-asserted by the harness against the pin in
`tests/test_semantic_fixture.py`).

**Reproduce exactly** (seeds fixed inside the scripts; this is the full
invocation behind every number below — metric numbers are deterministic given
the seeds; **latency fields are exempt from bit-reproducibility** and carry
the environment card in §3):

```
python -m pytest tests/test_semantic_fixture.py
python benchmarks/fixtures/verify_semantic_v1.py
python benchmarks/ablation_arms.py --mode all --size 1000 --resamples 10000 --permutations 10000 --depths 30,50,100,200 --warmup 10 --out benchmarks/fixtures/results_ablation_v1.json
```

**All runs disclosure (no best-of-N):** (1) one harness smoke run at the
NON-decision corpus size 100 with reduced stats (`--size 100 --resamples 200
--permutations 200 --depths 30,50 --warmup 3`, output to a scratch directory,
not committed) — harness validation only, never a candidate result; for the
record, at that easier size vector-only led hybrid on paraphrase, consistent
with Lane 1b's observation that corpus=100 flatters every arm; (2) the single
full decision run reported here (exit 0; ablation 125.3 s + depth sweep
343.9 s wall). Nothing else was run. The machine was otherwise idle during
the timed pass.

**Stub-vs-real guard:** the harness asserts `embedder.dim == 384` and
`identity().name.startswith("fastembed:")` (never `"stub-hash"`) before every
quality config; the stub embedder is never instantiated anywhere in
`ablation_arms.py`. The defined-empty row is a contract check, never a
quality claim.

**Contamination label (applies to EVERY table in this file):** the reranker
`Xenova/ms-marco-MiniLM-L-6-v2` is MS-MARCO-trained and the embedder
`BAAI/bge-small-en-v1.5` is web-corpus-trained; `semantic_v1` is synthetic,
AI-authored (2026) — **zero-shot for both models; no MS-MARCO-derived data is
evaluated anywhere here.**

---

## 0. Config enumeration — why 7 valid configs

The full 2×2×2 grid over (vector leg, lexical leg, cross-encoder reranker)
has 8 raw combos. `hybrid_search` has no `use_lexical` lever (by design), so
the legs are exercised as follows — **product source untouched**:

| # | config | legs (V/L/R) | code path | status |
|---|---|---|---|---|
| 1 | `lexical-only` | off / on / off | `hybrid_search(use_vector=False)` (shipped lever) | measured |
| 2 | `lexical-only+rerank` | off / on / on | `hybrid_search(use_vector=False, reranker=ce)` | measured |
| 3 | `vector-only` | on / off / off | `adapter.vector_query(k=30)` direct (harness-composed) | measured |
| 4 | `vector-only+rerank` | on / off / on | `adapter.vector_query(k=30)` → `rerank(top=5)` (harness-composed) | measured |
| 5 | `hybrid-rrf` | on / on / off | `hybrid_search()` (shipped default path) | measured |
| 6 | `hybrid-rrf+rerank` | on / on / on | `hybrid_search(reranker=ce)` (the `Memory` auto default) | measured |
| 7 | `defined-empty` | off / off / (±R collapse) | shipped ADR-0024 empty branch via a harness capability shim | defined-empty |

Vector-off ∧ lexical-off is **defined-empty by product contract** (ADR-0024:
"honestly empty rather than a garbage ranking"), and reranking an empty
candidate list is the identity — so its two ±rerank raw combos collapse into
one config. The collapse was **asserted at run time** by driving the shipped
empty branch with and without the reranker (`empty_collapse_check:
plain_hits=0, rerank_hits=0`), and the config's per-query rows are real
all-zero rows produced by that shipped path. **8 − 2 + 1 = 7 valid configs:
6 measured + 1 defined-empty.**

Harness-composition note for configs 3–4: the same query sanitation
(`sanitize_unicode` + `MAX_QUERY_CHARS`) and candidate pool (30 =
`max(k*6, k)`, the product default) as the product path; the product's
tamper-verify and quarantine filters are skipped — both are no-ops on this
freshly ingested OWNER-only bench corpus (every record verifies, nothing is
quarantined).

---

## 1. Lane 1c — ablation at the decision corpus (1,000)

> **Config card** — embedder: `fastembed:BAAI/bge-small-en-v1.5` (fastembed
> 0.8.0, dim 384); reranker (configs 2/4/6): `Xenova/ms-marco-MiniLM-L-6-v2`,
> rerank depth = candidate pool = 30; k=5; similarity: cosine; fusion: RRF
> k=60; chunking: n/a (direct records); screen/redact: OFF (adapter.add
> direct); kind: RAW_FACT; scope: `tenant=bench/project=bench/agent=bench`;
> trust: OWNER; dataset: semantic_v1 (synthetic, AI-authored, MIT), 118 scored
> queries + 12 controls (controls excluded from all means); corpus: 1,000 docs
> (100 committed + seeded filler `73f7eae8…`); seeds: 20260707; CI: percentile
> bootstrap, 10,000 resamples; paired tests: exact sign test + sign-flip
> permutation (10,000); env: Python 3.12.6, Windows 11, CPU-only. **No latency
> numbers are claimed in this lane** (latency lives in §2–§3). Zero-shot /
> no-contamination label at the top of this file applies.

**Subset: all** (n=118)

| config | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| lexical-only | 0.831 [0.763, 0.893] | 0.742 [0.671, 0.810] | 0.847 [0.780, 0.907] | 0.759 [0.690, 0.823] |
| lexical-only+rerank | 0.856 [0.791, 0.915] | 0.831 [0.764, 0.893] | 0.864 [0.797, 0.924] | 0.836 [0.770, 0.897] |
| vector-only | 0.924 [0.879, 0.963] | 0.908 [0.858, 0.952] | 0.949 [0.907, 0.983] | 0.902 [0.854, 0.944] |
| vector-only+rerank | 0.944 [0.904, 0.977] | 0.891 [0.843, 0.936] | 0.966 [0.932, 0.992] | 0.896 [0.851, 0.938] |
| hybrid-rrf | 0.941 [0.898, 0.977] | 0.877 [0.825, 0.924] | 0.966 [0.932, 0.992] | 0.886 [0.838, 0.930] |
| hybrid-rrf+rerank | 0.932 [0.887, 0.972] | 0.883 [0.832, 0.932] | 0.949 [0.907, 0.983] | 0.890 [0.842, 0.935] |
| defined-empty **[NEVER QUALITY]** | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |

**Subset: paraphrase** (n=64)

| config | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| lexical-only | 0.703 [0.594, 0.807] | 0.540 [0.436, 0.643] | 0.734 [0.625, 0.844] | 0.573 [0.470, 0.671] |
| lexical-only+rerank | 0.740 [0.630, 0.844] | 0.698 [0.586, 0.802] | 0.750 [0.641, 0.859] | 0.707 [0.597, 0.809] |
| vector-only | 0.859 [0.776, 0.932] | 0.839 [0.751, 0.914] | 0.906 [0.828, 0.969] | 0.826 [0.741, 0.901] |
| vector-only+rerank | 0.901 [0.828, 0.964] | 0.809 [0.724, 0.883] | 0.938 [0.875, 0.984] | 0.817 [0.741, 0.885] |
| hybrid-rrf | 0.901 [0.828, 0.964] | 0.780 [0.693, 0.861] | 0.938 [0.875, 0.984] | 0.802 [0.721, 0.876] |
| hybrid-rrf+rerank | 0.880 [0.797, 0.948] | 0.795 [0.706, 0.873] | 0.906 [0.828, 0.969] | 0.807 [0.723, 0.881] |
| defined-empty **[NEVER QUALITY]** | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] | 0.000 [0.000, 0.000] |

(`lexical-only` and `hybrid-rrf`/`hybrid-rrf+rerank` reproduce Lane 1b's
`bm25-only` / `fastembed-hybrid` / `fastembed-hybrid-rerank` rows exactly —
same seeds, same shipped path — a cross-harness consistency check.)

Per-bucket tables (low/med/high/multi_gold) are in
`results_ablation_v1.json` (`ablation.configs.*.subsets`), along with
per-query metric rows for every config (`ablation.per_query`) for independent
re-analysis. Headline observations from the buckets: the HIGH bucket is
saturated (1.000 everywhere measurable); the LOW bucket is where the legs
separate (lexical-only 0.455 recall@5 vs vector-only 0.838 / hybrid 0.838).

### 1.1 Adjacent paired comparisons (pre-declared)

Exact sign test + sign-flip permutation (10,000; seed 20260707), win/loss/tie.

**Fusion questions** — does each leg earn its keep?

| comparison | subset | metric | mean diff | W/L/T | sign p | perm p |
|---|---|---|---|---|---|---|
| hybrid-rrf vs lexical-only | paraphrase | recall@5 | +0.198 | 17/1/46 | 1.45e-04 | 4.00e-04 |
| hybrid-rrf vs lexical-only | paraphrase | MRR | +0.241 | 31/1/32 | 1.54e-08 | 1.00e-04 |
| hybrid-rrf vs lexical-only | paraphrase | hit-rate@5 | +0.203 | 14/1/49 | 9.77e-04 | 1.00e-03 |
| hybrid-rrf vs lexical-only | all | recall@5 | +0.110 | 18/1/99 | 7.63e-05 | 3.00e-04 |
| hybrid-rrf vs lexical-only | all | MRR | +0.135 | 32/1/85 | 7.92e-09 | 1.00e-04 |
| hybrid-rrf vs lexical-only | all | hit-rate@5 | +0.119 | 15/1/102 | 5.19e-04 | 5.00e-04 |
| hybrid-rrf vs vector-only | paraphrase | recall@5 | +0.042 | 6/2/56 | 2.89e-01 | 3.33e-01 |
| hybrid-rrf vs vector-only | paraphrase | MRR | −0.058 | 7/14/43 | 1.89e-01 | 2.14e-01 |
| hybrid-rrf vs vector-only | paraphrase | hit-rate@5 | +0.031 | 4/2/58 | 6.88e-01 | 6.88e-01 |
| hybrid-rrf vs vector-only | all | recall@5 | +0.017 | 6/3/109 | 5.08e-01 | 5.12e-01 |
| hybrid-rrf vs vector-only | all | MRR | −0.031 | 7/14/97 | 1.89e-01 | 2.22e-01 |
| hybrid-rrf vs vector-only | all | hit-rate@5 | +0.017 | 4/2/112 | 6.88e-01 | 6.84e-01 |

**±rerank at fixed legs** (rerank depth 30):

| comparison | subset | metric | mean diff | W/L/T | sign p | perm p |
|---|---|---|---|---|---|---|
| lexical+rerank vs lexical | paraphrase | recall@5 | +0.036 | 6/2/56 | 2.89e-01 | 4.17e-01 |
| lexical+rerank vs lexical | paraphrase | MRR | **+0.158** | 19/4/41 | **2.60e-03** | **2.00e-04** |
| lexical+rerank vs lexical | paraphrase | hit-rate@5 | +0.016 | 3/2/59 | 1.00e+00 | 1.00e+00 |
| lexical+rerank vs lexical | all | MRR | **+0.089** | 20/4/94 | **1.54e-03** | **1.00e-04** |
| vector+rerank vs vector | paraphrase | recall@5 | +0.042 | 6/2/56 | 2.89e-01 | 2.88e-01 |
| vector+rerank vs vector | paraphrase | MRR | −0.030 | 8/11/45 | 6.48e-01 | 4.91e-01 |
| vector+rerank vs vector | all | recall@5 | +0.020 | 6/3/109 | 5.08e-01 | 3.81e-01 |
| vector+rerank vs vector | all | MRR | −0.018 | 8/12/98 | 5.03e-01 | 4.52e-01 |
| hybrid+rerank vs hybrid | paraphrase | recall@5 | −0.021 | 3/5/56 | 7.27e-01 | 6.87e-01 |
| hybrid+rerank vs hybrid | paraphrase | MRR | +0.015 | 10/11/43 | 1.00e+00 | 6.86e-01 |
| hybrid+rerank vs hybrid | paraphrase | hit-rate@5 | −0.031 | 2/4/58 | 6.88e-01 | 6.89e-01 |
| hybrid+rerank vs hybrid | all | recall@5 | −0.008 | 4/5/109 | 1.00e+00 | 8.08e-01 |
| hybrid+rerank vs hybrid | all | MRR | +0.007 | 10/12/96 | 8.32e-01 | 7.37e-01 |
| hybrid+rerank vs hybrid | all | hit-rate@5 | −0.017 | 2/4/112 | 6.88e-01 | 6.81e-01 |

(The full grid — every metric × both subsets for all five comparisons — is in
`results_ablation_v1.json` → `ablation.comparisons`.)

### 1.2 What the ablation says

1. **The vector leg decisively earns its keep.** hybrid vs lexical-only is
   significant on every metric, both subsets (all p ≤ 1e-3; paraphrase
   recall@5 +0.198, 17 wins / 1 loss). Same P0 conclusion as Lane 1b, now
   with the leg isolated.
2. **The lexical leg's incremental value on top of vectors is NOT
   established on this fixture.** hybrid vs vector-only: recall@5 trends
   positive (+0.042 paraphrase, 6W/2L) but is far from significant
   (p ≈ 0.3), and MRR trends *negative* (−0.058, 7W/14L, p ≈ 0.2). The
   lexical leg's justification at this corpus size is architectural, not
   metric: it is the shipped honest-degradation fallback when the embedder
   identity mismatches (ADR-0024), and it costs recall nothing measurable.
3. **The reranker at the default depth 30 does not earn its keep on hybrid**
   — every hybrid+rerank vs hybrid difference is statistically
   indistinguishable from zero (recall trends −0.021 paraphrase, MRR trends
   +0.015). It *does* significantly lift MRR on lexical-only (+0.158
   paraphrase, p = 2.6e-03) — exactly the degraded mode the product falls
   back to on embedder mismatch.
4. **defined-empty is a contract check** (all-zero by ADR-0024), included so
   the config count is honest — it is what the product returns when both
   legs are off, and the ±rerank collapse was asserted, not assumed.

---

## 2. Lane 4 — reranker value vs candidate depth (corpus = 1,000)

> **Config card** — path: `hybrid_search(candidates=N[, reranker=ce])` — the
> shipped kwarg, no product change; rerank depth = fused pool = N; embedder /
> fixture / corpus / seeds / CI / paired tests identical to §1's card. Latency
> is measured in the same pass as the metrics (10 untimed warmup calls per
> arm first), stages via injected timing wrappers around the embedder and
> reranker (the product read path runs as shipped); `retrieve+fuse` is
> DERIVED as end_to_end − embed − rerank and covers both SQLite legs
> (brute-force cosine scan + FTS5), RRF, and content-hash verification.
> Latency env in §3; latency fields NOT bit-reproducible. Zero-shot /
> no-contamination label applies. Lane-1b context this lane was built on: at
> depth 30, rerank hurt paraphrase recall@5 (0.880 vs 0.901) while helping
> MRR (0.795 vs 0.780) — reproduced exactly below.

### 2.1 Metrics × depth (95% bootstrap CIs)

| depth | arm | recall@5 all | recall@5 paraphrase | MRR all | MRR paraphrase | hit-rate@5 all | hit-rate@5 paraphrase |
|---|---|---|---|---|---|---|---|
| 30 | no-rerank | 0.941 [0.898, 0.977] | 0.901 [0.828, 0.964] | 0.877 [0.825, 0.924] | 0.780 [0.693, 0.861] | 0.966 [0.932, 0.992] | 0.938 [0.875, 0.984] |
| 30 | rerank | 0.932 [0.887, 0.972] | 0.880 [0.797, 0.948] | 0.883 [0.832, 0.932] | 0.795 [0.706, 0.873] | 0.949 [0.907, 0.983] | 0.906 [0.828, 0.969] |
| 50 | no-rerank | 0.924 [0.876, 0.966] | 0.870 [0.786, 0.943] | 0.868 [0.813, 0.918] | 0.765 [0.672, 0.850] | 0.949 [0.907, 0.983] | 0.906 [0.828, 0.969] |
| 50 | rerank | 0.932 [0.887, 0.972] | 0.880 [0.797, 0.948] | 0.879 [0.827, 0.927] | 0.787 [0.699, 0.867] | 0.949 [0.907, 0.983] | 0.906 [0.828, 0.969] |
| 100 | no-rerank | 0.898 [0.842, 0.949] | 0.823 [0.724, 0.906] | 0.850 [0.790, 0.906] | 0.732 [0.630, 0.827] | 0.915 [0.864, 0.966] | 0.844 [0.750, 0.922] |
| 100 | rerank | 0.915 [0.867, 0.960] | 0.849 [0.760, 0.927] | 0.874 [0.821, 0.925] | 0.779 [0.685, 0.862] | 0.932 [0.890, 0.975] | 0.875 [0.781, 0.953] |
| 200 | no-rerank | 0.864 [0.802, 0.921] | 0.760 [0.651, 0.859] | 0.843 [0.778, 0.901] | 0.717 [0.609, 0.818] | 0.881 [0.822, 0.932] | 0.781 [0.672, 0.875] |
| 200 | rerank | 0.915 [0.867, 0.960] | 0.849 [0.760, 0.927] | 0.874 [0.821, 0.925] | 0.779 [0.685, 0.862] | 0.932 [0.890, 0.975] | 0.875 [0.781, 0.953] |

### 2.2 Paired tests, rerank vs no-rerank at each depth

| depth | subset | metric | mean diff | W/L/T | sign p | perm p |
|---|---|---|---|---|---|---|
| 30 | paraphrase | recall@5 | −0.021 | 3/5/56 | 7.27e-01 | 6.87e-01 |
| 30 | paraphrase | MRR | +0.015 | 10/11/43 | 1.00e+00 | 6.86e-01 |
| 30 | all | recall@5 | −0.008 | 4/5/109 | 1.00e+00 | 8.08e-01 |
| 30 | all | MRR | +0.007 | 10/12/96 | 8.32e-01 | 7.37e-01 |
| 50 | paraphrase | recall@5 | +0.010 | 4/4/56 | 1.00e+00 | 9.26e-01 |
| 50 | paraphrase | MRR | +0.023 | 10/10/44 | 1.00e+00 | 5.32e-01 |
| 50 | all | recall@5 | +0.008 | 5/4/109 | 1.00e+00 | 8.04e-01 |
| 50 | all | MRR | +0.011 | 10/11/97 | 1.00e+00 | 5.88e-01 |
| 100 | paraphrase | recall@5 | +0.026 | 4/1/59 | 3.75e-01 | 4.25e-01 |
| 100 | paraphrase | MRR | +0.047 | 10/8/46 | 8.15e-01 | 2.63e-01 |
| 100 | all | recall@5 | +0.017 | 5/1/112 | 2.19e-01 | 3.53e-01 |
| 100 | all | MRR | +0.024 | 10/9/99 | 1.00e+00 | 2.91e-01 |
| 200 | paraphrase | recall@5 | **+0.089** | 8/1/55 | **3.91e-02** | **4.02e-02** |
| 200 | paraphrase | MRR | +0.061 | 10/8/46 | 8.15e-01 | 1.93e-01 |
| 200 | paraphrase | hit-rate@5 | +0.094 | 7/1/56 | 7.03e-02 | 6.96e-02 |
| 200 | all | recall@5 | **+0.051** | 9/1/108 | **2.15e-02** | **3.12e-02** |
| 200 | all | MRR | +0.032 | 10/9/99 | 1.00e+00 | 2.15e-01 |

And rerank@depth vs rerank@30 (does a deeper rerank beat the default-depth
rerank?): **never positive.** depth 50 vs 30: 0W/1L/63T on paraphrase MRR,
identical recall; depth 100 and 200 vs 30: recall@5 −0.031 paraphrase
(1W/3L, p = 0.625). Reranked output is *identical* at depths 100 and 200 —
candidates ranked beyond ~100 by RRF never crack the reranked top-5 on this
fixture. Full grids in `results_ablation_v1.json` →
`depth_sweep.depths.*.rerank_depth*_vs_depth30`.

### 2.3 Metric-vs-latency frontier (the decision table)

Latency: end-to-end per query around the shipped `hybrid_search` call;
stages per §2's config card; environment per §3. **Latency values are not
bit-reproducible; metric values are.** Zero-shot / no-contamination label
applies.

| depth | arm | recall@5 para [CI] | MRR para | recall@5 all | MRR all | e2e p50 ms | e2e p95 ms | rerank-stage p50 ms |
|---|---|---|---|---|---|---|---|---|
| **30** | **no-rerank** | **0.901 [0.828, 0.964]** | 0.780 | **0.941** | 0.877 | **155.9** | **163.5** | — |
| 30 | rerank *(shipped `Memory` default)* | 0.880 [0.797, 0.948] | 0.795 | 0.932 | 0.883 | 249.2 | 267.0 | 91.6 |
| 50 | no-rerank | 0.870 [0.786, 0.943] | 0.765 | 0.924 | 0.868 | 167.7 | 175.3 | — |
| 50 | rerank | 0.880 [0.797, 0.948] | 0.787 | 0.932 | 0.879 | 318.8 | 353.1 | 157.2 |
| 100 | no-rerank | 0.823 [0.724, 0.906] | 0.732 | 0.898 | 0.850 | 191.7 | 205.2 | — |
| 100 | rerank | 0.849 [0.760, 0.927] | 0.779 | 0.915 | 0.874 | 485.5 | 552.4 | 302.1 |
| 200 | no-rerank | 0.760 [0.651, 0.859] | 0.717 | 0.864 | 0.843 | 206.5 | 275.5 | — |
| 200 | rerank | 0.849 [0.760, 0.927] | 0.779 | 0.915 | 0.874 | 803.0 | 913.3 | 590.5 |

Stage breakdown (p50, depth 30): embed ≈ 3.6–4.0 ms; retrieve+fuse (derived)
≈ 152–154 ms; rerank 91.6 ms. The retrieve+fuse stage — dominated by the
SQLite adapter's brute-force cosine scan over the 1,000-record scope —
dwarfs embedding at every depth and grows only mildly with the pool
(152 → 202 ms from depth 30 → 200); the rerank stage scales roughly linearly
with depth (≈ 3 ms per candidate).

### 2.4 What the depth sweep says

1. **RRF-without-rerank degrades monotonically as the pool deepens**
   (paraphrase recall@5: 0.901 → 0.870 → 0.823 → 0.760 at 30/50/100/200).
   Mechanism: `rrf_fuse` keeps `top=pool` and truncates to k *after* fusion,
   so a mediocre doc surfacing in BOTH legs' deep tails accumulates two
   reciprocal-rank contributions and displaces a gold that one leg ranked
   highly. Deeper pools = more double-counted tail junk. `candidates` is a
   quality *footgun* without a reranker.
2. **The reranker is a rescue lever for deep pools, not a lift at the
   default.** At depth 30 it is statistically indistinguishable on
   everything (and recall trends down: −0.021 paraphrase). At depth 200 it
   significantly rescues recall (+0.089 paraphrase, sign p = 0.039;
   +0.051 all, p = 0.022) — but the rescued 0.849 still sits *below* the
   plain depth-30 default's 0.901 (point estimates; CIs overlap).
3. **No depth beats 30 for the reranked arm** (50/100/200 vs 30: never a
   positive mean diff; reranked output saturates at depth ≈ 100).
4. **MRR is the one metric the reranker consistently trends up on hybrid**
   (+0.015 → +0.061 across depths, never significant at any depth on this
   fixture; the only *significant* MRR win is on lexical-only, §1.1).

---

## 3. Latency environment (applies to every latency number above)

| field | value |
|---|---|
| CPU | AMD Ryzen 7 7800X3D 8-Core Processor (8C/16T) |
| RAM | 32 GB |
| OS | Windows 11 Pro (10.0.26200) |
| Python | 3.12.6 |
| fastembed / onnxruntime | 0.8.0 / 1.27.0 |
| threads | onnxruntime defaults (not pinned) |
| batch | 1 query per call (the product read-path shape) |
| warmup | 10 untimed full-pipeline calls per arm |
| N | 118 timed queries per arm; timer `time.perf_counter` |
| store | SQLite `:memory:`, 1,000 records, single scope |

Latency numbers are environment-bound and **not bit-reproducible**; metric
numbers are deterministic given the fixed seeds (20260707).

---

## 4. Recommendation — KEEP / TUNE / DROP

**Fusion (hybrid RRF, default pool 30): KEEP.** The vector leg is decisively
required (§1.1, p ≤ 1e-3 everywhere vs lexical-only). The lexical leg adds
no *measurable* lift over vector-only on this fixture (recall trend +0.042
paraphrase, n.s.; MRR trend −0.058, n.s.) but costs nothing measurable
either, and it is the shipped honest-degradation fallback on embedder
mismatch (ADR-0024) — keep it for the architecture, with the honest caveat
that its metric contribution at 1k is unproven.

**Reranker (`Xenova/ms-marco-MiniLM-L-6-v2`): TUNE — do not keep as a
blanket default, do not drop.** The evidence:

- At the shipped default (`Memory` auto-enables the reranker; hybrid, depth
  30) it buys **no statistically detectable metric change** (recall@5
  −0.021 paraphrase / −0.008 all, MRR +0.015 / +0.007 — all p ≥ 0.69) for
  **+60% end-to-end p50 latency** (156 → 249 ms) on this fixture.
- It **earns its keep** in two specific places: (a) deep candidate pools —
  at depth 200 it significantly rescues recall@5 (+0.089 paraphrase,
  p = 0.039); (b) the lexical-only degraded mode (embedder mismatch), where
  it significantly lifts MRR (+0.158 paraphrase, p = 2.6e-03).
- Deeper rerank depths never beat depth 30 (§2.4-3), so if the reranker is
  on, the default depth 30 is already the right depth — do NOT raise
  `candidates` looking for quality.

Concretely, the evidence-backed sweet spot on this fixture is
**hybrid-RRF, depth 30, rerank OFF** (best recall@5, best hit-rate@5,
lowest latency) with rerank ON reserved for MRR/precision-oriented callers,
degraded lexical-only mode, or any caller that raises `candidates`. **The
default flip itself is an OWNER call** — `Memory(reranker="auto")` +
`rerank=True` is the shipped default today; this lane only recommends, with
the evidence above. A DROP is not supported by the data (the rescue and
degraded-mode wins are real and significant).

---

## 5. Honest weaknesses & routing

1. Everything inherits the semantic_v1 fixture weaknesses (RESULTS_semantic_v1
   §5): synthetic AI-authored content, AI adjudication, templated filler,
   single embedder/reranker pair, English-only, k=5 only.
2. The paraphrase subset is n=64 — ±rerank effects of the observed size
   (±0.02) are far below this sample's detection threshold; the honest
   statement is "no detectable difference," not "no difference."
3. Latency is one machine, one store (`:memory:` SQLite), one corpus size;
   the retrieve+fuse stage is a brute-force scan that scales linearly with
   corpus size, so the reranker's *relative* cost shrinks on bigger corpora.
4. Vector-only configs are harness-composed (§0) — identical sanitation and
   pool, but they skip the (here no-op) tamper/quarantine filters.

**Routed to the fix-orchestrator:**

- **(F1)** `retrieve+fuse` dominates read latency (~152 ms p50 at corpus=1k,
  ~97% of the no-rerank read) because the SQLite adapter's vector leg is a
  full-scope brute-force cosine scan in Python. An ANN / vectorized scan is
  the highest-leverage read-latency fix available.
- **(F2)** `candidates` is a silent quality footgun: raising it WITHOUT a
  reranker monotonically degrades top-k quality (§2.4-1: 0.901 → 0.760
  paraphrase recall@5 at 200). Consider documenting this on the kwarg, or
  clamping/warning when `candidates >> k*6` and `reranker is None`.
- **(F3)** The `Memory` default (auto-reranker ON at depth 30) pays +60% p50
  read latency for no detectable quality change on this fixture — owner
  decision on the default flip, evidence in §4.

*Generated from `results_ablation_v1.json` (committed alongside) by the Lane
1c/4 session. Wall time: ablation 125.3 s, depth sweep 343.9 s.*
