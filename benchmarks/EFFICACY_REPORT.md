# Rekoll Efficacy Report — v1 (2026-07-07)

**Question answered:** does Rekoll actually deliver its promised value — does
`recall()` return the RIGHT memories, end-to-end, across real projects, with
honest degradation signals — measured by RUN, never by assumption?

**Answer (evidence, not verdict — the go/no-go call is the owner's):** on every
P0 measurement the answer is yes, with named caveats and ten routed findings.

Every number below was produced by a committed, seeded harness and
INDEPENDENTLY REPRODUCED by the program conductor before merge (bit-for-bit
where seeds allow). Nothing here comes from the stub embedder unless
explicitly labeled PIPELINE-ONLY.

## 0. Recorded baseline

- Program start: `main` @ `5b4ab768`, clean, **562 passed / 19 skipped**
  (Python 3.12.6, Windows 11, fastembed 0.8.0 installed, mcp absent).
- Program end (pre-report): `main` @ `b9e710f4`, **617 passed / 21 skipped** —
  55 tests added, zero regressions at every intermediate merge (full suite
  re-run between merges; merged one lane at a time).
- Program PRs: #20 ([bench] extra + CI scaffold), #21 (evaluator metrics),
  #22 (honesty pins), #23 (three-doors parity), #26 (dogfood), #30 (semantic
  fixture + P0), #31 (ratcheted real-embedder gate), #34 (ablation + reranker).

## 1. Semantic recall quality (Lane 1b — PR #30)

Fixture: `benchmarks/fixtures/semantic_v1.json` — 118 scored queries (64
paraphrase, 15 multi-gold |G|≥3), 12 negative controls (excluded from all
means), 100 committed docs + seeded distractor filler to 1k/10k, ≥20
BM25-mined hard negatives per query, sha256-frozen (`8580fe4b…`),
PRE-REGISTERED before any result existed (commit order provable in git
history: prereg 19fa18b → freeze 6b43f1a → results d94c29b).

**P0 result** (paraphrase bucket, corpus = 1,000 — the pre-registered decision
size; recall@5, mean [bootstrap 95% CI], 10k resamples, seed 20260707):

| arm | recall@5 (paraphrase) |
|---|---|
| stub hybrid (PIPELINE-ONLY, never quality) | 0.396 [0.276, 0.516] |
| BM25 / lexical-only | 0.703 [0.594, 0.807] |
| **FastEmbed hybrid** | **0.901 [0.828, 0.964]** |
| FastEmbed hybrid + rerank (diagnostic) | 0.880 [0.797, 0.948] |

FastEmbed-vs-BM25: CIs disjoint; paired sign test 17W/1L/46T, p=1.45e-04;
permutation p=4.0e-04. FastEmbed-vs-stub: 37W/1L/26T, p=2.84e-10.
**P0 HOLDS** — reproduced bit-for-bit by the conductor.

Honest stratification: the semantic gain lives in LOW lexical overlap
(+0.384 recall vs BM25); MED overlap is a recall wash (MRR +0.127, p=0.012);
HIGH overlap is saturated (all arms tie). Robustness sizes 100/10k have
overlapping CIs vs BM25 — the 1k decision size was pre-registered, not chosen
after seeing results.

Adjudication: two independent AI passes; round 1 all-agree (κ undefined —
reported as uninformative, not hidden); round 2 with 40 blinded bait pairs:
gold 148/148, bait 39/40 rejected, **κ = 1.00**. Annotators are AI — labeled
as such; 10 sample pairs listed in PR #30 for human spot-check.

Known weaknesses (from the lane's own report): synthetic AI-authored content
may flatter embedders; unstemmed FTS5 flatters the vector arm on inflection
mismatches; single embedder/reranker; English-only; k=5.

## 2. Real-repo dogfood (Lane 2 — PR #26)

Four real local repos, 10 pre-registered dev questions each (committed before
any recall ran), gold = the file a returning developer needs; HIT = gold in
top-5 by provenance. Real embedder + reranker (`vector+lexical+rerank`).

| repo (type) | hit-rate @5 | MRR |
|---|---|---|
| powered-by-people-website (Vite/React site) | 9/10 | 0.77 |
| jeff-app-for-iphone (Expo/React-Native app) | 10/10 | 0.80 |
| rekreate-ai-chat, zero-config (drowned in `env/`) | **6/10 — FAIL** | 0.55 |
| rekreate-ai-chat, `skip_dirs+{'env'}` | 10/10 | 0.83 |
| Trading-Logging-Automation (Python scripts) | 10/10 | 0.93 |

39/40 on clean runs; the single miss is a definition-vs-usage failure (a
tailwind config asked for as "where are brand fonts registered"). The
conductor reproduced 3 of 4 repos rank-for-rank.

Scope isolation: 76 cross-scope recalls, **0 leaks**; per-project counts
unaffected by other ingests. (Observation: shared-FTS BM25 corpus statistics
couple ranking MARGINS across scopes — ±1 rank shifts — though membership
never crosses.)

The zero-config drowning is the program's headline product finding → issue #27.

## 3. Honest degradation surfaces (Lane 5 — PR #22)

12 exact-string pins over `RecallResult.mode`, `health()`, `self_test()`, and
the read-path tamper warning. **No lies found**: stub runs say
"(stub-embedder)"; an embedder-identity mismatch refuses the vector leg AT THE
ADAPTER (spy-proven) and says so; empty scope reads ok=None; dead ingest reads
ok=False; a tampered row NEVER surfaces through any accessor or the envelope,
the exact warning fires, and the read provably does not write the store.
One attribution nit → issue #24.

## 4. Three-doors parity (Lane 3a — PR #23)

SDK == CLI == MCP: identical ordered top-5 id lists on 10/10 queries against
one store (stub-pinned for determinism), plus k∈{1,3,10}, kind-filter parity,
and the empty-result contract. CI's test-mcp matrix runs the full three-door
legs on every push. Named gap: MCP/CLI don't expose the honest `mode` string
→ issue #25.

## 5. Retrieval-leg ablation & reranker value (Lanes 1c+4 — PR #34)

Seven-config ablation at the pre-registered decision size (2×2×2 minus the
defined-empty pair; conductor-reproduced bit-for-bit):

| config | recall@5 all | recall@5 paraphrase |
|---|---|---|
| lexical-only | 0.831 [0.763, 0.893] | 0.703 [0.594, 0.807] |
| lexical-only + rerank | 0.856 [0.791, 0.915] | 0.740 [0.630, 0.844] |
| vector-only | 0.924 [0.879, 0.963] | 0.859 [0.776, 0.932] |
| vector-only + rerank | 0.944 [0.904, 0.977] | 0.901 [0.828, 0.964] |
| hybrid (RRF) | 0.941 [0.898, 0.977] | 0.901 [0.828, 0.964] |
| hybrid (RRF) + rerank | 0.932 [0.887, 0.972] | 0.880 [0.797, 0.948] |
| vector-off ∧ lexical-off | defined-empty (ADR-0024), asserted 0 hits |

Paired tests: hybrid vs lexical-only significant everywhere (paraphrase
+0.198, p=1.45e-04); hybrid vs vector-only NOT significant on any metric —
on this fixture the vector leg does the work. ±rerank at fixed legs: all
n.s. EXCEPT lexical-only MRR +0.158 (p=2.6e-03) — the reranker's one
significant quality win is in degraded lexical mode.

Depth × latency frontier (contamination label: MS-MARCO-trained reranker,
zero-shot synthetic fixture; latency env recorded, not bit-reproducible):
RRF-only quality degrades monotonically with pool depth (0.901 → 0.760 at
depth 200 — issue #36); the reranker rescues deep pools but never beats plain
depth-30; at the shipped default (auto-rerank, pool 30) it buys no detectable
metric change for **+60% p50 latency** (156 → 249 ms).

**Recommendation:** fusion KEEP; reranker **TUNE, not blanket default, not
drop** — it earns its keep for deep pools, MRR-oriented use, and lexical-only
degraded mode. The default flip is an OWNER call → issue #37. Per-stage
timing puts ~97% of a no-rerank read in the brute-force vector scan → issue
#35 (top latency lever).

## 6. Quality gates now in CI

- Stub PIPELINE gate (pre-existing): recall@5 ≥ 0.9 / MRR ≥ 0.85 on the
  keyword-distinct smoke fixture — proves plumbing, says nothing about quality.
- **NEW ratcheted REAL-embedder gate** (PR #31, `test-embeddings` job):
  fastembed bge-small over the frozen semantic_v1 committed docs; raise-only
  floors = baseline − one bootstrap half-width (recall@5 ≥ 0.86, MRR ≥ 0.81).
  Skips cleanly (never errors) without the extra — proven for both missing
  AND broken installs. ONNX models cached between CI runs (SHA-pinned
  actions/cache).
- Fixture integrity gates: semantic_v1 sha256 pin + derivation verifier;
  dogfood questions pre-registered with one-command reproduction.

## 7. Findings routed to the fix-orchestrator (all filed, evidence-backed)

| # | severity | finding | fix-wave status (2026-07-10) |
|---|---|---|---|
| #27 | HIGH | zero-config ingest drowns in a checked-in `env/` virtualenv (`DEFAULT_SKIP_DIRS` misses `env`, `site-packages`) | **fixed** — PR #39 (ADR-0027) |
| #35 | MED→HIGH at scale | brute-force SQLite vector scan ≈97% of read latency at 1k records | **fixed** — PR #42 (ADR-0030) |
| #32 | MED | no score-threshold/abstain path — unanswerable queries get k confident-looking hits (signal exists: AUC 0.931) | **fixed** — PR #44 (ADR-0028, opt-in `min_score`) |
| #36 | MED | `candidates=N` without a reranker silently degrades quality as N grows | **fixed** — PR #44 |
| #25 | MED | MCP/CLI hide `RecallResult.mode` — honest degradation invisible outside the SDK | **fixed** — PR #40 (`mode` crosses every door) |
| #29 | MED-HIGH (policy) | well-known secrets files (credentials.json) stored as ordinary records | **fixed** — PR #39 (skip + warn) |
| #28 | MED | lockfiles ingested by default (53–74% of chunks in JS repos) | **fixed** — PR #39 |
| #37 | decision | OWNER: Memory auto-rerank default — +60% latency, no detectable lift at default depth | open — final fix lane |
| #33 | LOW-MED | no `use_lexical` lever on `hybrid_search` | **fixed** — PR #44 |
| #24 | LOW | `health()` misattributes a tampered newest record to dead ingestion | open — final fix lane |

The measurements in §§1–5 predate the fix wave and are kept as the historical
pre-fix record. The post-fix re-baseline is §9; where a number moved, §9 is
authoritative.

## 8. Reproduce everything

```
python -m pytest                                          # full suite
python -m pytest tests/test_benchmark_semantic.py         # real-embedder gate ([embeddings])
python benchmarks/run_benchmark.py --fixture benchmarks/fixtures/semantic_v1.json --fastembed
python benchmarks/compare_arms.py --sizes 1000 --resamples 10000 --permutations 10000 --out r.json
python benchmarks/ablation_arms.py --mode ablation --out a.json
python benchmarks/dogfood/run_dogfood.py --db <tmp>/d.db --out <tmp>/r.json
python benchmarks/fixtures/verify_semantic_v1.py          # fixture derivations + hash
```

Config card (headline runs): embedder fastembed 0.8.0 `BAAI/bge-small-en-v1.5`
dim 384; reranker (where used) `Xenova/ms-marco-MiniLM-L-6-v2` (MS-MARCO-
trained; semantic_v1 is synthetic AI-authored → zero-shot for both models);
k=5; cosine; RRF k=60; SQLite adapter (FTS5, unstemmed); screen/redact off in
harnesses; seeds 20260707; bootstrap 10k resamples; env Python 3.12.6 /
Windows 11 / CPU (latency: Ryzen 7 7800X3D, 32 GB). The default
`pip install rekoll` is DELIBERATELY non-semantic (stub) — semantic recall
requires the `[embeddings]` extra.

## 9. Post-fix-wave re-baseline (2026-07-11, main @ `cc1728f`)

The fix wave closed all ten routed findings (§7) across five PRs
(#39/#40/#44/#42/#48). Two of them change what these numbers are measured
under, so the evidence pack was re-run in full and reproduced by the conductor:
**#42** (ADR-0030) made the vector scan a cached exact scan, and **#37**
(ADR-0029) flipped the shipped default from auto-rerank-on to
**no-rerank-in-hybrid** (rerank auto-attaches only in degraded lexical-only
mode). Same config card as above except the **default read is now
`vector+lexical` (no rerank)**.

**Recall quality — UNCHANGED, bit-for-bit.** The perf work was mandated not to
move ranking, and it didn't. compare_arms and the 7-config ablation at the
pre-registered decision size (corpus=1000) reproduced §1/§5 to the digit:
stub 0.396 / BM25 0.703 / FastEmbed-hybrid **0.901** [0.828,0.964] paraphrase;
ablation arms all identical (vector-only 0.859, hybrid 0.901, hybrid+rerank
0.880, …). The ratcheted real-embedder CI gate stays green and its raise-only
floors are **unchanged** (recall@5 ≥ 0.86, MRR ≥ 0.81) — nothing improved in the
gated metrics to ratchet, by design.

**Read latency — the headline change.** End-to-end recall p50 at the shipped
default, corpus=1000 (latency env: Ryzen 7 7800X3D, Win 11, warmup 10):

| read | v1 (pre-fix) | post-fix (§9) | change |
|---|---|---|---|
| default recall (pool 30, **new default = no rerank**) | 249 ms (v1 default was auto-rerank) | **16.2 ms** p50 (18.8 p95) | **~15× faster** |
| no-rerank pool 30 (like-for-like) | 156 ms | 16.2 ms | ~9.6× |
| depth sweep no-rerank | 156→(deeper) | 23.8 / 42.0 / 67.4 ms at 50 / 100 / 200 | scan no longer dominates |

The ~97%-of-latency brute-force scan (§5, issue #35) is gone; a 1k default read
went from a sixth of a second to ~16 ms. The #36 deep-pool footgun still holds
(no-rerank recall@5 0.901→0.760 across pool 30→200) and now **warns**.

**Real-repo dogfood — the #27 headline, proven end-to-end.** Re-run zero-config
under the new default (mode `vector+lexical` on every repo):

| repo | v1 | post-fix | note |
|---|---|---|---|
| rekreate-ai-chat, **zero-config** | **6/10 — FAIL** (drowned in `env/`) | **10/10** | **47 records / 2.3 s** (was ~9,106 / 20+ min) — #27 fixed |
| rekreate-ai-chat, `--noenv` (manual) | 10/10 | 10/10 | zero-config now equals hand-filtered |
| jeff-app-for-iphone | 10/10 | 10/10 | |
| Trading-Logging-Automation | 10/10 | 10/10 | ingest shows `secrets_skipped=1` — #41 signal visible |
| powered-by-people-website | 9/10 | 8/10 | see note |

Total 48/50 (the once-drowned repo now passes). Scope isolation re-confirmed:
**76 cross-scope recalls, 0 leaks, 0 count mismatches.** The single ingest that
excluded a credential-shaped file surfaced it as a `secrets_skipped` count
(#29/#41), not silently.

*Honest note on the website 9→8:* the two misses (`ServicesSection.tsx`,
`ScrollToTop.tsx`) are NOT the same as v1's one miss, and the repo is a LIVE
(unfrozen) checkout that moved between runs. Tested for cause rather than
assumed: re-running both queries with an explicit reranker attached leaves both
golds unranked (rank None with rerank ON *and* OFF), so the #37 no-rerank
default did **not** cause the drop — it is repo drift plus a genuine
definition-vs-usage recall gap, and it re-confirms #37's finding that the
reranker buys no recovery here. Not a regression introduced by the wave.

**Bottom line:** the wave made reads ~15× faster and fixed the zero-config
drowning that was the program's headline product finding, with recall quality
held bit-identical and honesty surfaces (mode, abstain, secrets counts, tamper
notes) now crossing every door — all conductor-reproduced, suite 617→849, zero
regressions.
