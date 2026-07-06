# ADR-0011 — Benchmarking: a CI recall gate now, LongMemEval for headline numbers

**Status:** Accepted · **Date:** 2026-06-23

## Context
"Did that change help?" must be a tracked number, not a vibe — both to prevent
regressions and to be credible against Mem0 (49%) / Zep (63.8%). But CI runs with
dev-only deps (no model downloads, no network), so the in-CI signal must work on
the stub embedder.

## Decision
- A dependency-free `rekoll.evaluation` module: `Recall@k`, `MRR`, and an
  `evaluate(search_fn, queries, k)` decoupled from storage/embedding via a
  `search_fn(query) -> list[id]`. The same harness scores the stub gate, a
  fastembed run, or a LongMemEval subset.
- **CI gate (now):** a committed keyword-distinct fixture
  (`benchmarks/recall_smoke.json`) run on the **stub** embedder. It is a *pipeline
  regression gate* (stub scores 1.0; a break in scope/fusion/adapter drops it),
  with committed baselines that may only be raised, never silently lowered.
- **Headline numbers (next):** a LongMemEval subset run via
  `benchmarks/run_benchmark.py --fastembed --rerank`, with a sealed train/test
  split (tune on train, touch test once at release) to avoid teaching-to-the-test.
  Not in CI (needs the dataset + model downloads); produces the published table.

## Consequences
- Every retrieval change is measured; regressions fail CI cheaply and offline.
- The smoke fixture is deliberately easy (it does not discriminate stub vs
  fastembed vs rerank) — that discrimination is LongMemEval's job, kept separate
  so the CI gate stays fast and network-free.
- `run_benchmark.py` already supports `--fastembed`/`--rerank`, so wiring
  LongMemEval is "add a loader + a sealed split," not new harness code.

## Addendum (2026-07-07) — metric policy for the extended evaluator
`rekoll.evaluation` now also reports hit-rate@k, precision@k, MAP, and nDCG@k
(binary + graded), plus per-query rows for CIs/paired tests. Policy: on
binary/low-positive fixtures (the smoke gate, LongMemEval-style subsets),
**recall@k / MRR / hit-rate@k are PRIMARY**; nDCG@k is a labeled diagnostic
only (≈ rank-of-first on binary gold) and is headlined only on a genuinely
graded fixture (real `relevant_grades`).
