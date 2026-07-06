# Pre-registration — `semantic_v1` fixture & arm comparison

**Status:** pre-registered BEFORE any retrieval results were produced.
The git history is the proof: this file's first commit precedes the first
results commit on this branch. The fixture content-hash is appended in the
freeze commit (also before any results commit).

**Author:** synthetic fixture + protocol written by an AI session
(Claude Fable 5) for the Rekoll efficacy program. All content is original
synthetic work, MIT-licensed with the repository. No external datasets are
used or downloaded.

---

## 1. Question under test (P0)

On **paraphrase** queries (meaning matches, surface form does not), does the
real embedder pipeline (`fastembed` hybrid) beat (a) the stub-embedder
pipeline and (b) lexical-only BM25 — with non-overlapping bootstrap 95% CIs?

We commit to reporting the outcome **whichever way it comes out**, per bucket,
with no post-hoc metric or bucket swapping.

## 2. Metrics

- **PRIMARY:** recall@5, MRR, hit-rate@5 (means over scored queries).
- **Diagnostic only (never headlined):** nDCG@5 (gold is binary here, so
  nDCG is roughly "rank of first hit" — ADR-0011 addendum), precision@5, AP.
- **k = 5** for all @k metrics.
- **P0 decision criterion:** recall@5 on the paraphrase set — P0 "holds" iff
  the 95% bootstrap CI for fastembed-hybrid is disjoint from and above the CI
  of BOTH bm25-only and stub-hybrid. MRR and hit-rate@5 are reported alongside
  with the same tests as secondary confirmations.

## 3. Arms

All arms score the **same corpus content**; k=5; RRF k=60 (product default);
candidate pool = `max(k*6, k)` = 30 (product default); reads via
`rekoll.retrieval.hybrid_search` unless stated.

| arm id | description | label |
|---|---|---|
| `stub-hybrid` | StubEmbedder (dim 64, hashed tokens, zero semantics) + FTS5/BM25, RRF | **pipeline-only — NEVER a quality number** |
| `bm25-only` | `hybrid_search(use_vector=False)` — SQLite FTS5 BM25, no vector leg | quality baseline |
| `fastembed-hybrid` | FastEmbedEmbedder `BAAI/bge-small-en-v1.5` (dim 384) + FTS5/BM25, RRF | **quality — the arm under test** |
| `fastembed-hybrid-rerank` | above + CrossEncoderReranker `Xenova/ms-marco-MiniLM-L-6-v2`, rerank depth = candidate pool (30) | diagnostic |

**Stub-vs-real guard (programmatic):** for every quality arm the harness
asserts `embedder.dim == 384` and `embedder.identity().name != "stub-hash"`
and `embedder.identity().name.startswith("fastembed:")`, failing loudly
otherwise. A stub number presented as quality is an automatic reject.

## 4. Fixture composition (bars, not targets)

- ≥ 100 scored queries (headline bar); target ≈ 108.
- ≥ 30 scored queries per lexical-overlap bucket (3 buckets, §5).
- Mostly single-gold; a multi-gold subset of ≥ 15 queries with |G| ≥ 3.
- ≥ 40 queries flagged `paraphrase: true` (authored as same-meaning,
  different-surface: synonym/hypernym swaps, restructured phrasing).
- ≥ 12 negative-control queries (answer absent by construction). Controls are
  **EXCLUDED from all recall/MRR/hit-rate/nDCG means** and reported separately
  (§8 abstention diagnostic + a "what does it return anyway" note).
- ≥ 20 hard negatives per scored query (§6).
- Content: dev-memory-shaped records (decisions, constraints, how-tos,
  incidents, conventions) across five fictitious projects of a fictitious
  company — not wordlists.

## 5. Lexical-overlap measure, buckets, honesty rule

- Tokenizer: lowercase, tokens = maximal `[a-z0-9]+` runs.
- Stopwords: a fixed committed list (frozen inside the fixture metadata and
  covered by the content hash).
- Content tokens `T(x)` = tokens of `x` minus stopwords (set semantics).
- Per-query overlap = **max over that query's gold docs** of
  `|T(q) ∩ T(g)| / |T(q)|`.
- Buckets: **LOW** `[0, 0.25)`, **MED** `[0.25, 0.5)`, **HIGH** `[0.5, 1.0]`.
- **Honesty rule:** queries are authored naturally and overlap is *measured*,
  never targeted. We do not engineer overlap toward 0 (that would overstate
  vectors / understate BM25). The full per-bucket overlap distribution
  (histogram + per-bucket mean) is reported in RESULTS. Composition minimums
  (≥30/bucket) may be met by authoring *additional natural queries* before the
  freeze — never by rewording existing queries to move their measured overlap.

## 6. Hard negatives

- Miner: a self-contained **Okapi BM25** implementation (k1 = 1.5, b = 0.75,
  stdlib Python, committed in `verify_semantic_v1.py`) — deliberately NOT the
  embedding model under test and NOT the product's FTS5 code path.
- For each scored query: top 20 non-gold committed docs by BM25 score over the
  100 committed docs; zero-score ties broken by seeded shuffle
  (seed 20260707) so every query gets exactly ≥ 20.
- Hard-negative doc ids are recorded per query in the fixture (committed data).

## 7. Corpus-size sweep

- Sizes: **100** (all committed docs), **1,000** (100 committed + 900
  generated), **10,000** (100 committed + 9,900 generated).
- Filler comes from a committed, seeded, deterministic generator
  (`gen_distractors.py`, seed 20260707); the sha256 of each generated corpus
  is recorded in RESULTS and re-asserted by the harness.
- Generator vocabulary is fixed and disjoint from negative-control key terms,
  so control answers remain absent at every corpus size (verified
  programmatically).
- **Headline tables and the P0 comparison are at corpus = 1,000** (a realistic
  dev-memory size); 100 and 10,000 are robustness sweeps. (Pre-registered here,
  before any run.)

## 8. Abstention diagnostic (labeled — not a product metric)

The product recall path has **no threshold/abstain mechanism**; we do not
fabricate one. This section is a **diagnostic only, routed to the
fix-orchestrator**:

- Score: top-1 cosine similarity from `adapter.vector_query(k=1)` with the
  real embedder at corpus = 1,000.
- Compare distributions: answerable (scored) queries vs negative-control
  queries; report rank-based AUC (Mann–Whitney).
- Pre-registered threshold sweep grid: τ ∈ {0.40, 0.45, 0.50, 0.55, 0.60,
  0.65, 0.70, 0.75, 0.80, 0.85, 0.90, 0.95}; at each τ report the
  answerable-accept rate and the control-accept rate.

## 9. Statistics

- **CIs:** nonparametric percentile bootstrap over per-query metric rows
  (`evaluate(..., per_query=True)`), resampling queries with replacement,
  **10,000 resamples, seed 20260707**, 2.5/97.5 percentiles. Computed per
  arm × bucket × corpus size.
- **Paired comparisons** (fastembed-hybrid vs bm25-only; fastembed-hybrid vs
  stub-hybrid), on the paraphrase set and per bucket:
  - paired **sign test**: exact two-sided binomial on wins/losses, ties
    dropped;
  - paired **sign-flip permutation test** on the mean per-query difference,
    10,000 permutations, seed 20260707;
  - win / loss / tie counts.
- **All runs are reported** — no best-of-N, no seed shopping. All seeds fixed
  at 20260707.
- Pure stdlib implementations (no numpy/scipy).

## 10. Adjudication of gold labels

- Every (query, gold-doc) pair receives **two independent adjudication
  passes**: two fresh AI sessions with different prompt framings and
  independently shuffled pair order, no shared notes.
- **Honest label: the annotators are AI passes, not humans.** ~10 sample pairs
  are listed in the PR description for a human owner spot-check.
- Report raw agreement + Cohen's κ. Any pair rejected by either pass is
  repaired (query or doc rewritten, or pair dropped) and repaired pairs are
  re-adjudicated before the freeze. The frozen fixture contains only pairs
  accepted by both passes (original or re-adjudicated).

## 11. Freeze & integrity

- The fixture is frozen as `benchmarks/fixtures/semantic_v1.json` with a
  sha256 **content hash** computed over the canonical JSON serialization
  (sorted keys, compact separators, UTF-8) of the fixture object minus the
  `content_sha256` field itself.
- The hash is recorded below (appended in the freeze commit) and asserted by
  `tests/test_semantic_fixture.py`, so silent edits fail CI.
- The integrity test is stub-safe (no model download, offline, no quality
  gate — quality gating is Lane 1d, conductor-owned).

## 12. Environment / config card constants

- embedder: `fastembed` 0.8.0, model `BAAI/bge-small-en-v1.5`, dim 384,
  local ONNX (already cached; no network at run time).
- reranker (diagnostic arm only): `Xenova/ms-marco-MiniLM-L-6-v2`, depth 30.
- similarity: cosine; fusion: RRF k=60; chunking: n/a (direct records);
  firewall screen/redact: OFF in the harness (records ingested via
  `adapter.add` directly — content is never mutated).
- scope: single bench scope (`tenant=bench, project=bench, agent=bench`);
  kind: RAW_FACT for all records; trust: OWNER.
- No latency numbers are claimed in this lane.
- Python 3.12.6, Windows 11, CPU-only.

---

## Freeze record (appended at the freeze commit — still before any results commit)

- `semantic_v1.json` content_sha256:
  `8580fe4bcce5415a7cdfcbbe534b08ffbcbd07b13d266317620d8442591a1a58`
- Committed docs: 100 (46 gold, 54 near-miss distractors)
- Scored queries: 118 — buckets LOW 33 / MED 34 / HIGH 51; paraphrase-flagged 64;
  multi-gold (|G| >= 3) 15
- Negative controls: 12 (excluded from all metric means)
- Hard negatives: 20 per scored query, miner `okapi-bm25-stdlib (k1=1.5, b=0.75)`,
  tiebreak seed 20260707
- Filler corpora (seed 20260707): sha256 n=900 `73f7eae8d34bf067…`,
  n=9900 `69e287192b9987ff…` (full values pinned in tests/test_semantic_fixture.py).
  *Post-freeze amendment, disclosed:* the first generator version produced
  duplicate texts, which the adapter's UNIQUE (scope, content_hash) constraint
  rejects at ingest — the 1k corpus was unbuildable, so NO 1k/10k results ever
  existed under the old hashes (`04ab012e…`/`a9f9d120…`). The generator gained
  draw-until-unique dedup (same seed, same templates) and these hashes were
  re-pinned BEFORE the first successful 1k/10k scoring run. Committed docs and
  the fixture content hash are untouched.
- Adjudication (see `adjudication_semantic_v1.json` — annotators are AI passes,
  honestly labeled):
  - Round 1 (gold pairs only, 148): both passes all-yes; raw agreement 1.00;
    κ UNDEFINED (degenerate all-yes marginals) — reported honestly; this
    motivated round 2.
  - Round 2 (calibrated: 148 gold + 40 blinded bait pairs = query × its own
    top-ranked BM25 hard negative): gold accepted 148/148 by both passes; bait
    rejected 39/40 by both; raw agreement 1.00; Cohen's κ = 1.00
    (mixed-category set).
  - One repair: (q102, d-atlas-postgres-version) — bait judged relevant by both
    passes; relabeled as gold for q102 (keeping it a negative would have been
    label noise biased against the lexical arm). Derived fields re-derived and
    re-verified.
  - Residual-noise estimate: 1/40 sampled top-rank hard negatives (2.5%) was an
    unlabeled positive; the remaining mined negatives were not exhaustively
    adjudicated (known weakness).
- One earlier draft of the fixture (hash `6b816a15…`, pre-repair) was committed
  before adjudication completed; the only content change since is the q102
  relabel above. No retrieval results existed before this freeze commit.
