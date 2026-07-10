# ADR-0029 — `reranker='auto'` attaches the cross-encoder only in degraded mode

**Status:** Accepted · **Date:** 2026-07-11 · **Amends:** ADR-0010 (reranker optional), ADR-0021 (trust-bounded rerank) · **Extends:** ADR-0024 (honest degradation)

## Context

`Memory(reranker='auto')` is the default. Until this ADR, `auto` meant "attach
the cross-encoder whenever the `embeddings` extra is installed" — so every
recall on a normal, healthy hybrid store paid for a rerank pass.

Lane 4 of the efficacy program (PR #34, `RESULTS_ablation_v1.md §4`) measured
what that pass buys at the shipped depth (candidate pool 30, k=5, corpus 1,000,
fastembed `bge-small-en-v1.5`, MS-MARCO cross-encoder, Ryzen 7 7800X3D / Win 11):

| metric | no rerank | rerank | verdict |
| --- | --- | --- | --- |
| recall@5 (paraphrase) | 0.901 | 0.880 | n.s. |
| MRR | 0.780 | 0.795 | n.s. |
| end-to-end p50 latency | 156 ms | 249 ms | **+60%** |

The reranker earns its keep in exactly two places, neither of them the default
hybrid path:

- **deep candidate pools** — it rescues depth-200 recall (p=0.039); and
- **lexical-only degraded mode** — when an embedder mismatch has refused the
  vector leg (ADR-0024), reranking the lexical candidates lifts **MRR +0.158
  (p=2.6e-03)** — the one statistically significant *quality* win in the study.

So the shipped default was spending +60% read latency, on every recall, for a
quality change indistinguishable from noise — and switching it fully off would
throw away its single real win, which happens precisely when retrieval is
already degraded and needs the help most.

## Decision

Under `reranker='auto'` (the default), the cross-encoder attaches **only when
the scope is degraded to lexical-only by an embedder mismatch**. In normal
hybrid — vector leg live, identity matching (including the stub embedder) — auto
is **off**. An explicit `reranker=` is always honored verbatim, including
`reranker=None` and an explicit reranker object; explicit intent is never
second-guessed and its semantics are unchanged from before this ADR.

The decision is **dynamic, re-evaluated per search**, not frozen at
construction. Two facts force this:

1. `Memory.__init__` computes the reranker choice *before* the embedder-identity
   state is known, so an init-time resolution cannot see whether the scope is
   degraded; and
2. `Memory.reindex()` clears a mismatch at runtime — a scope that opened
   degraded (auto → on) and was then reindexed must stop reranking without being
   reconstructed.

`Memory.reranker` is therefore a **read-only property** that reports the
reranker a recall would use *right now*: the explicit object when one was
given, else the auto cross-encoder iff `_identity_state == "mismatch"`, else
`None`. The auto model is constructed **lazily and memoized** — the first time a
degraded scope asks for it, never at import and never on the normal hybrid path
— so the zero-dependency default path stays cost-free and the "no heavy import
at import time" invariant holds.

`HealthReport.mode` and `RecallResult.mode` report this truthfully: a degraded
auto scope reads `lexical-only+rerank: embedder mismatch`; a healthy auto scope
reads `vector+lexical` (or `vector+lexical (stub-embedder)`) with no `+rerank`
leg. The honest-degradation strings name what actually runs.

## Consequences

- **Faster default reads.** The common case (healthy hybrid) drops the rerank
  pass and its +60% p50 latency, with no measurable recall/MRR cost.
- **The real win is kept.** Degraded lexical-only recall — the case that most
  needs precision help — still reranks, preserving the MRR +0.158 result.
- **`mem.reranker` becomes dynamic and read-only.** Reading it is unchanged for
  the explicit case; under auto it now reflects live state instead of a frozen
  init choice. It can no longer be assigned (pass `reranker=` to the
  constructor). No shipped code assigned it.
- **Contamination label (unchanged from Lane 4):** the cross-encoder is
  MS-MARCO-trained and was evaluated zero-shot on a synthetic paraphrase
  fixture; the *policy* (attach only where it measurably helps) is robust to
  that caveat because it strictly narrows where a cost is paid.

## Alternatives rejected

- **Keep auto-on everywhere (document the cost).** Simple, but bakes a
  measured-useless +60% latency into every default read.
- **Default fully off, one-line opt-in.** Latency-lean, but discards the one
  significant quality win, and does so in the degraded state where an operator
  is least able to notice they should have opted in.
- **Freeze the degraded-only choice at construction.** Wrong by the two facts
  above: the identity state isn't known yet at init, and `reindex()` moves it.
