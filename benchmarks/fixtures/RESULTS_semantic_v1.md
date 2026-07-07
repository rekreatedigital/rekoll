# RESULTS — semantic_v1 (Lane 1b)

**Protocol:** everything here was pre-registered in
[PREREGISTRATION_semantic_v1.md](PREREGISTRATION_semantic_v1.md) *before* any
run (git history is the proof: prereg `19fa18b` → freeze `6b43f1a` → this
results commit). Fixture frozen at content_sha256
`8580fe4bcce5415a7cdfcbbe534b08ffbcbd07b13d266317620d8442591a1a58`.

**Reproduce exactly** (seeds are fixed inside the scripts; these are the full
invocations used for every number below):

```
python -m pytest tests/test_semantic_fixture.py
python benchmarks/fixtures/verify_semantic_v1.py
python benchmarks/run_benchmark.py --fixture benchmarks/fixtures/semantic_v1.json --fastembed
python benchmarks/compare_arms.py --sizes 100,1000,10000 --resamples 10000 --permutations 10000 --out benchmarks/fixtures/results_semantic_v1.json
```

**All runs disclosure (no best-of-N):** (1) one harness smoke run at
corpus=100 with reduced resamples (200) during development — same fixed seeds,
point estimates identical to the full run below (CIs differ only in resample
count); (2) one full-sweep attempt that completed corpus=100 (identical
numbers to below) and then CRASHED building the 1k corpus because the filler
generator emitted duplicate texts (see the disclosed post-freeze amendment in
the prereg freeze record — no 1k/10k results existed before the fix);
(3) `run_benchmark.py --fastembed` on this fixture (corpus=100 committed docs):
recall@5=0.912, MRR=0.868 — consistent with the fastembed-hybrid arm below;
(4) the full sweep reported here. Nothing else was run.

**Stub-vs-real guard:** the harness programmatically asserts
`embedder.dim == 384` and `identity().name.startswith("fastembed:")` (never
`"stub-hash"`) for every quality arm. The stub-hybrid row is pipeline-health
context only and is never a quality claim.

---

## 1. Fixture composition & measured lexical-overlap distribution

118 scored queries (64 paraphrase-flagged, 15
multi-gold |G|≥3, one |G|=4 after the adjudication repair), 12 negative
controls (excluded from every mean), 100 committed docs (46 gold / 54
near-miss distractors), 20 BM25-mined hard negatives per query.

Overlap measured per prereg §5 (max over golds of |T(q)∩T(g)|/|T(q)|),
**never targeted** — the authored distribution came out as:

| bucket | n | mean overlap | median | min | max | paraphrase-flagged |
|---|---|---|---|---|---|---|
| low | 33 | 0.101 | 0.125 | 0.000 | 0.222 | 32 |
| med | 34 | 0.331 | 0.333 | 0.250 | 0.429 | 30 |
| high | 51 | 0.930 | 1.000 | 0.500 | 1.000 | 2 |

Histogram over all scored queries (overlap decile: count):
`[0.0,0.1): 11  [0.1,0.2): 18  [0.2,0.3): 18  [0.3,0.4): 10  [0.4,0.5): 10  [0.5,0.6): 4  [0.6,0.7): 1  [0.7,0.8): 2  [0.8,0.9): 5  [0.9,1.0): 39`

Paraphrase-flagged queries by bucket: {'med': 30, 'low': 32, 'high': 2} — paraphrases are
present in every bucket; overlap was NOT engineered toward 0 (median paraphrase
overlap is well above zero).

Adjudication: two-round, two independent AI passes (honestly labeled: AI, not
human), round-2 calibrated with 40 blinded bait pairs — gold 148/148 accepted
by both, bait 39/40 rejected by both, raw agreement 1.00, Cohen's κ = 1.00
(round-1 κ undefined on the all-gold set — reported honestly). One repair
(q102). Full record: [adjudication_semantic_v1.json](adjudication_semantic_v1.json).

---

## 2. Headline: corpus = 1,000 (pre-registered headline size)

### 2.1 All scored queries + paraphrase subset

> **Config card** — embedder: `fastembed:BAAI/bge-small-en-v1.5` (fastembed 0.8.0, dim 384); stub arm: `stub-hash` dim 64 (PIPELINE-ONLY); reranker (diagnostic arm only): `Xenova/ms-marco-MiniLM-L-6-v2`, depth 30; k=5; similarity: cosine; fusion: RRF k=60; chunking: n/a (direct records); screen/redact: OFF (adapter.add direct); kind: RAW_FACT; scope: `tenant=bench/project=bench/agent=bench`; trust: OWNER; dataset: semantic_v1 (synthetic, AI-authored, MIT), content_sha256 `8580fe4bcce5415a…`, 118 scored queries + 12 controls (controls excluded from all means); seeds: 20260707; CI: percentile bootstrap, 10000 resamples; paired tests: exact sign test + sign-flip permutation (10000); env: Python 3.12.6, Windows 11, CPU-only. No latency numbers are claimed.

Corpus: 1000 docs (100 committed + seeded filler, sha256 `73f7eae8d34bf067…`).

**Subset: all** (n=118)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.639 [0.554, 0.722] | 0.541 [0.460, 0.621] | 0.669 [0.585, 0.754] | 0.557 [0.477, 0.634] |
| bm25-only | 0.831 [0.763, 0.893] | 0.742 [0.671, 0.810] | 0.847 [0.780, 0.907] | 0.759 [0.690, 0.823] |
| fastembed-hybrid | 0.941 [0.898, 0.977] | 0.877 [0.825, 0.924] | 0.966 [0.932, 0.992] | 0.886 [0.838, 0.930] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.932 [0.887, 0.972] | 0.883 [0.832, 0.932] | 0.949 [0.907, 0.983] | 0.890 [0.842, 0.935] |

**Subset: paraphrase** (n=64)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.396 [0.276, 0.516] | 0.222 [0.148, 0.301] | 0.422 [0.297, 0.547] | 0.261 [0.180, 0.345] |
| bm25-only | 0.703 [0.594, 0.807] | 0.540 [0.436, 0.643] | 0.734 [0.625, 0.844] | 0.573 [0.470, 0.671] |
| fastembed-hybrid | 0.901 [0.828, 0.964] | 0.780 [0.693, 0.861] | 0.938 [0.875, 0.984] | 0.802 [0.721, 0.876] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.880 [0.797, 0.948] | 0.795 [0.706, 0.873] | 0.906 [0.828, 0.969] | 0.807 [0.723, 0.881] |


### 2.2 P0 — paraphrase subset, pre-registered criterion (recall@5 CIs disjoint)

- fastembed-hybrid vs bm25-only: **DISJOINT, fastembed-hybrid above (0.828 > 0.807)**
- fastembed-hybrid vs stub-hybrid: **DISJOINT, fastembed-hybrid above (0.828 > 0.516)**

**P0 HOLDS** under the pre-registered
criterion (recall@5, paraphrase subset, corpus=1,000, non-overlapping 95%
bootstrap CIs against BOTH baselines).

Paired tests (paraphrase subset, n=64):

| comparison | metric | mean diff | sign test W/L/T | sign p | permutation p |
|---|---|---|---|---|---|
| fastembed-hybrid vs bm25-only | recall@5 | +0.198 | 17/1/46 | 1.45e-04 | 4.00e-04 |
| fastembed-hybrid vs bm25-only | MRR | +0.241 | 31/1/32 | 1.54e-08 | 1.00e-04 |
| fastembed-hybrid vs bm25-only | hit-rate@5 | +0.203 | 14/1/49 | 9.77e-04 | 1.00e-03 |
| fastembed-hybrid vs stub-hybrid | recall@5 | +0.505 | 37/1/26 | 2.84e-10 | 1.00e-04 |
| fastembed-hybrid vs stub-hybrid | MRR | +0.559 | 54/1/9 | 3.11e-15 | 1.00e-04 |
| fastembed-hybrid vs stub-hybrid | hit-rate@5 | +0.516 | 34/1/29 | 2.10e-09 | 1.00e-04 |

### 2.3 Stratified by overlap bucket (corpus = 1,000)



> **Config card** — embedder: `fastembed:BAAI/bge-small-en-v1.5` (fastembed 0.8.0, dim 384); stub arm: `stub-hash` dim 64 (PIPELINE-ONLY); reranker (diagnostic arm only): `Xenova/ms-marco-MiniLM-L-6-v2`, depth 30; k=5; similarity: cosine; fusion: RRF k=60; chunking: n/a (direct records); screen/redact: OFF (adapter.add direct); kind: RAW_FACT; scope: `tenant=bench/project=bench/agent=bench`; trust: OWNER; dataset: semantic_v1 (synthetic, AI-authored, MIT), content_sha256 `8580fe4bcce5415a…`, 118 scored queries + 12 controls (controls excluded from all means); seeds: 20260707; CI: percentile bootstrap, 10000 resamples; paired tests: exact sign test + sign-flip permutation (10000); env: Python 3.12.6, Windows 11, CPU-only. No latency numbers are claimed.

Corpus: 1000 docs (100 committed + seeded filler, sha256 `73f7eae8d34bf067…`).

**Subset: low** (n=33)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.144 [0.030, 0.273] | 0.074 [0.014, 0.156] | 0.152 [0.030, 0.273] | 0.085 [0.019, 0.162] |
| bm25-only | 0.455 [0.293, 0.616] | 0.292 [0.161, 0.432] | 0.485 [0.303, 0.667] | 0.320 [0.193, 0.455] |
| fastembed-hybrid | 0.838 [0.717, 0.939] | 0.642 [0.512, 0.765] | 0.909 [0.788, 1.000] | 0.679 [0.557, 0.794] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.828 [0.707, 0.939] | 0.694 [0.559, 0.819] | 0.879 [0.758, 0.970] | 0.709 [0.587, 0.823] |

**Subset: med** (n=34)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.608 [0.451, 0.755] | 0.385 [0.272, 0.500] | 0.706 [0.559, 0.853] | 0.420 [0.308, 0.531] |
| bm25-only | 0.941 [0.863, 1.000] | 0.792 [0.686, 0.887] | 0.971 [0.912, 1.000] | 0.822 [0.729, 0.905] |
| fastembed-hybrid | 0.951 [0.873, 1.000] | 0.919 [0.831, 0.985] | 0.971 [0.912, 1.000] | 0.917 [0.834, 0.983] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.931 [0.843, 1.000] | 0.892 [0.794, 0.971] | 0.941 [0.853, 1.000] | 0.902 [0.806, 0.977] |

**Subset: high** (n=51)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.980 [0.941, 1.000] | 0.948 [0.892, 0.990] | 0.980 [0.941, 1.000] | 0.954 [0.902, 0.993] |
| bm25-only | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| fastembed-hybrid | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |
| fastembed-hybrid-rerank *(diagnostic)* | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] | 1.000 [1.000, 1.000] |

**Subset: multi_gold** (n=15)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.361 [0.178, 0.544] | 0.411 [0.194, 0.633] | 0.600 [0.333, 0.800] | 0.327 [0.155, 0.510] |
| bm25-only | 0.600 [0.378, 0.800] | 0.633 [0.400, 0.833] | 0.733 [0.467, 0.933] | 0.583 [0.361, 0.789] |
| fastembed-hybrid | 0.800 [0.644, 0.933] | 0.806 [0.650, 0.933] | 1.000 [1.000, 1.000] | 0.756 [0.578, 0.907] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.800 [0.622, 0.933] | 0.844 [0.667, 1.000] | 0.933 [0.800, 1.000] | 0.792 [0.610, 0.945] |


Paired tests per bucket (fastembed-hybrid vs bm25-only / stub-hybrid):

**LOW bucket:**

| comparison | metric | mean diff | sign test W/L/T | sign p | permutation p |
|---|---|---|---|---|---|
| fastembed-hybrid vs bm25-only | recall@5 | +0.384 | 15/0/18 | 6.10e-05 | 2.00e-04 |
| fastembed-hybrid vs bm25-only | MRR | +0.351 | 22/0/11 | 4.77e-07 | 1.00e-04 |
| fastembed-hybrid vs bm25-only | hit-rate@5 | +0.424 | 14/0/19 | 1.22e-04 | 3.00e-04 |
| fastembed-hybrid vs stub-hybrid | recall@5 | +0.694 | 26/0/7 | 2.98e-08 | 1.00e-04 |
| fastembed-hybrid vs stub-hybrid | MRR | +0.568 | 29/0/4 | 3.73e-09 | 1.00e-04 |
| fastembed-hybrid vs stub-hybrid | hit-rate@5 | +0.758 | 25/0/8 | 5.96e-08 | 1.00e-04 |

**MED bucket:**

| comparison | metric | mean diff | sign test W/L/T | sign p | permutation p |
|---|---|---|---|---|---|
| fastembed-hybrid vs bm25-only | recall@5 | +0.010 | 3/1/30 | 6.25e-01 | 1.00e+00 |
| fastembed-hybrid vs bm25-only | MRR | +0.127 | 10/1/23 | 1.17e-02 | 4.10e-02 |
| fastembed-hybrid vs bm25-only | hit-rate@5 | +0.000 | 1/1/32 | 1.00e+00 | 1.00e+00 |
| fastembed-hybrid vs stub-hybrid | recall@5 | +0.343 | 16/1/17 | 2.75e-04 | 4.00e-04 |
| fastembed-hybrid vs stub-hybrid | MRR | +0.534 | 27/1/6 | 2.16e-07 | 1.00e-04 |
| fastembed-hybrid vs stub-hybrid | hit-rate@5 | +0.265 | 10/1/23 | 1.17e-02 | 1.09e-02 |

**HIGH bucket:**

| comparison | metric | mean diff | sign test W/L/T | sign p | permutation p |
|---|---|---|---|---|---|
| fastembed-hybrid vs bm25-only | recall@5 | +0.000 | 0/0/51 | 1.00e+00 | 1.00e+00 |
| fastembed-hybrid vs bm25-only | MRR | +0.000 | 0/0/51 | 1.00e+00 | 1.00e+00 |
| fastembed-hybrid vs bm25-only | hit-rate@5 | +0.000 | 0/0/51 | 1.00e+00 | 1.00e+00 |
| fastembed-hybrid vs stub-hybrid | recall@5 | +0.020 | 1/0/50 | 1.00e+00 | 1.00e+00 |
| fastembed-hybrid vs stub-hybrid | MRR | +0.052 | 4/0/47 | 1.25e-01 | 1.23e-01 |
| fastembed-hybrid vs stub-hybrid | hit-rate@5 | +0.020 | 1/0/50 | 1.00e+00 | 1.00e+00 |

---

## 3. Corpus-size sweep (robustness)

### 3.1 Corpus = 100 (fully committed, no filler)

> **Config card** — embedder: `fastembed:BAAI/bge-small-en-v1.5` (fastembed 0.8.0, dim 384); stub arm: `stub-hash` dim 64 (PIPELINE-ONLY); reranker (diagnostic arm only): `Xenova/ms-marco-MiniLM-L-6-v2`, depth 30; k=5; similarity: cosine; fusion: RRF k=60; chunking: n/a (direct records); screen/redact: OFF (adapter.add direct); kind: RAW_FACT; scope: `tenant=bench/project=bench/agent=bench`; trust: OWNER; dataset: semantic_v1 (synthetic, AI-authored, MIT), content_sha256 `8580fe4bcce5415a…`, 118 scored queries + 12 controls (controls excluded from all means); seeds: 20260707; CI: percentile bootstrap, 10000 resamples; paired tests: exact sign test + sign-flip permutation (10000); env: Python 3.12.6, Windows 11, CPU-only. No latency numbers are claimed.

Corpus: 100 docs (100 committed, no filler).

**Subset: all** (n=118)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.638 [0.553, 0.720] | 0.556 [0.473, 0.638] | 0.661 [0.576, 0.746] | 0.568 [0.487, 0.648] |
| bm25-only | 0.828 [0.760, 0.890] | 0.740 [0.668, 0.807] | 0.847 [0.780, 0.907] | 0.754 [0.685, 0.818] |
| fastembed-hybrid | 0.912 [0.859, 0.958] | 0.868 [0.811, 0.919] | 0.932 [0.881, 0.975] | 0.873 [0.818, 0.921] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.944 [0.904, 0.977] | 0.903 [0.857, 0.945] | 0.966 [0.932, 0.992] | 0.907 [0.863, 0.947] |

**Subset: paraphrase** (n=64)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.401 [0.281, 0.521] | 0.257 [0.169, 0.350] | 0.422 [0.297, 0.547] | 0.290 [0.199, 0.385] |
| bm25-only | 0.693 [0.583, 0.797] | 0.520 [0.417, 0.620] | 0.719 [0.609, 0.828] | 0.556 [0.456, 0.654] |
| fastembed-hybrid | 0.844 [0.750, 0.927] | 0.757 [0.661, 0.844] | 0.875 [0.781, 0.953] | 0.770 [0.679, 0.854] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.901 [0.828, 0.964] | 0.831 [0.750, 0.901] | 0.938 [0.875, 0.984] | 0.838 [0.762, 0.905] |


### 3.2 Corpus = 10,000 (committed + seeded filler)

> **Config card** — embedder: `fastembed:BAAI/bge-small-en-v1.5` (fastembed 0.8.0, dim 384); stub arm: `stub-hash` dim 64 (PIPELINE-ONLY); reranker (diagnostic arm only): `Xenova/ms-marco-MiniLM-L-6-v2`, depth 30; k=5; similarity: cosine; fusion: RRF k=60; chunking: n/a (direct records); screen/redact: OFF (adapter.add direct); kind: RAW_FACT; scope: `tenant=bench/project=bench/agent=bench`; trust: OWNER; dataset: semantic_v1 (synthetic, AI-authored, MIT), content_sha256 `8580fe4bcce5415a…`, 118 scored queries + 12 controls (controls excluded from all means); seeds: 20260707; CI: percentile bootstrap, 10000 resamples; paired tests: exact sign test + sign-flip permutation (10000); env: Python 3.12.6, Windows 11, CPU-only. No latency numbers are claimed.

Corpus: 10000 docs (100 committed + seeded filler, sha256 `69e287192b9987ff…`).

**Subset: all** (n=118)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.696 [0.616, 0.774] | 0.518 [0.445, 0.591] | 0.729 [0.644, 0.805] | 0.557 [0.485, 0.628] |
| bm25-only | 0.820 [0.751, 0.883] | 0.725 [0.652, 0.794] | 0.839 [0.771, 0.898] | 0.742 [0.672, 0.807] |
| fastembed-hybrid | 0.929 [0.884, 0.969] | 0.879 [0.828, 0.926] | 0.958 [0.915, 0.992] | 0.882 [0.834, 0.926] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.932 [0.887, 0.972] | 0.904 [0.857, 0.948] | 0.958 [0.915, 0.992] | 0.901 [0.855, 0.944] |

**Subset: paraphrase** (n=64)

| arm | recall@5 | MRR | hit-rate@5 | nDCG@5 *(diagnostic)* |
|---|---|---|---|---|
| stub-hybrid **[PIPELINE-ONLY — never quality]** | 0.479 [0.359, 0.594] | 0.220 [0.163, 0.279] | 0.516 [0.391, 0.641] | 0.281 [0.210, 0.352] |
| bm25-only | 0.688 [0.573, 0.792] | 0.508 [0.407, 0.609] | 0.719 [0.609, 0.828] | 0.544 [0.443, 0.643] |
| fastembed-hybrid | 0.870 [0.792, 0.943] | 0.785 [0.697, 0.866] | 0.922 [0.844, 0.984] | 0.788 [0.703, 0.864] |
| fastembed-hybrid-rerank *(diagnostic)* | 0.880 [0.802, 0.948] | 0.834 [0.751, 0.907] | 0.922 [0.844, 0.984] | 0.827 [0.747, 0.898] |


P0-style CI check at the sweep sizes (recall@5, paraphrase, NOT the
pre-registered decision size — reported for robustness):

- corpus=100: vs bm25-only → OVERLAPPING ([0.750,0.927] vs [0.583,0.797]); vs stub → DISJOINT, fastembed-hybrid above (0.750 > 0.521)
- corpus=10,000: vs bm25-only → OVERLAPPING ([0.792,0.943] vs [0.573,0.792]); vs stub → DISJOINT, fastembed-hybrid above (0.792 > 0.594)

---

## 4. Abstention diagnostic (labeled — NOT a product metric)

**diagnostic — no abstain path exists in the product; routed to fix-orchestrator**

Score: top-1 cosine (adapter.vector_query, real embedder), corpus = 1,000.

- answerable queries (n=118): mean top-1 cosine 0.777 (min 0.603, max 0.927)
- negative controls (n=12): mean top-1 cosine 0.646 (min 0.574, max 0.696)
- **AUC (rank-based, answerable vs control): 0.931**

Pre-registered threshold sweep (accept iff top-1 cosine ≥ τ):

| τ | answerable accept | control accept |
|---|---|---|
| 0.40 | 1.000 | 1.000 |
| 0.45 | 1.000 | 1.000 |
| 0.50 | 1.000 | 1.000 |
| 0.55 | 1.000 | 1.000 |
| 0.60 | 1.000 | 0.917 |
| 0.65 | 0.941 | 0.417 |
| 0.70 | 0.763 | 0.000 |
| 0.75 | 0.542 | 0.000 |
| 0.80 | 0.424 | 0.000 |
| 0.85 | 0.331 | 0.000 |
| 0.90 | 0.076 | 0.000 |
| 0.95 | 0.000 | 0.000 |

Negative controls at every corpus size returned k hits regardless (mean hits
returned at 1k: 5.0) —
the product recall path has **no abstain mechanism**; this section is routed
to the fix-orchestrator as a product gap, not scored as a metric.

---

## 5. Honest weaknesses of this fixture

1. **Synthetic content, AI-authored.** Realistic in shape but not harvested
   from real usage; distributional quirks of one author (style, vocabulary)
   apply to both queries and docs and may flatter embedding models trained on
   similar text.
2. **Annotators are AI passes, not humans** (two independent framings,
   calibrated with bait; still the same base model family — correlated error
   is possible). 10 sample pairs are listed for human spot-check.
3. **Round-1 adjudication was uninformative** (all-gold set → ceiling
   agreement, κ undefined); only the calibrated round 2 carries evidence.
4. **Residual label noise in hard negatives:** 1/40 sampled top-rank negatives
   (2.5%) was an unlabeled positive (repaired); the remaining ~2,300 mined
   negatives were not exhaustively adjudicated.
5. **Filler is templated.** The 1k/10k filler is generated from ~28 template
   families; it is lexically plausible but less diverse than real memories,
   so large-corpus difficulty is probably understated for BOTH arms.
6. **Single embedder / single reranker** (bge-small-en-v1.5;
   ms-marco-MiniLM-L-6-v2): results say nothing about other models.
7. **BM25 arm is SQLite FTS5 defaults** (unicode61, no stemming): a stemmed or
   tuned lexical baseline would close some of the gap on plural/inflection
   mismatches; the hard-negative miner (Okapi, stopword-filtered) partially
   shares this blind spot.
8. **English-only, single scope, k=5 only.**

*Generated from `results_semantic_v1.json` (committed alongside) by the Lane
1b session. Wall times per corpus size: 100: 35.6s, 1000: 93.1s, 10000: 479.5s.*
