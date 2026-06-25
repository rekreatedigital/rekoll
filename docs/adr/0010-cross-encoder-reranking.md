# ADR-0010 — Cross-encoder reranking for retrieval precision (local, optional)

**Status:** Accepted · **Date:** 2026-06-23

## Context
First-pass hybrid retrieval (vector + BM25, fused by RRF) has good recall but
imprecise top-ranking — dogfooding showed a relevant-but-not-best chunk landing
first. A cross-encoder, which scores (query, passage) jointly rather than via
independent embeddings, is the standard precision fix.

## Decision
- Add a `Reranker` protocol and `CrossEncoderReranker` (fastembed
  `TextCrossEncoder`, default `Xenova/ms-marco-MiniLM-L-6-v2`). It is a small
  **local** ONNX transformer in the same optional `embeddings` extra — no new
  dependency, no API key, no data egress.
- A cross-encoder is a scorer, **not a generative LLM**, so using it on the read
  path does not violate "reads never call an LLM" (ADR-0007).
- `hybrid_search` gains an optional `reranker`: when present it fuses a larger
  candidate pool with RRF, then reranks to `k`; when absent it returns RRF order
  (documented **passthrough** — the core path never requires the extra).

## Consequences
- Precision improves materially on ambiguous queries; latency cost is one local
  model pass over the candidate pool (bounded, no network).
- The model loads lazily on first use and is cached; CI (dev-only deps) skips the
  real-model test, while it runs locally when the extra is installed.
- Future: tune the candidate-pool size and add score thresholds; evaluate against
  the LongMemEval/LoCoMo gate (next).
