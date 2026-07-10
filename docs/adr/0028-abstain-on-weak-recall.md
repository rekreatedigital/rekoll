# ADR-0028 — Abstain on weak recall: an opt-in cosine floor, gated pre-fusion

**Status:** Accepted · **Date:** 2026-07-10 · **Extends:** ADR-0024 (honest degradation), ADR-0007 (zero-LLM read path)

## Context

`recall()` always returns up to `k` hits, however weak the best match is. On the
frozen `semantic_v1` fixture (1,000 docs), the 12 negative-control queries —
whose answers are absent from the corpus *by construction* — receive `k` hits
every time. An agent consuming the envelope cannot distinguish best-effort
garbage from a genuine match, and the envelope is rendered identically either
way. The failure is quiet and it is the expensive kind: the agent proceeds
confidently on retrieved text that has nothing to do with the question.

The signal to fix this already exists in the read path. Over the pre-registered
threshold sweep (fastembed `bge-small-en-v1.5`, dim 384, corpus 1,000, seed
20260707), **top-1 cosine similarity separates answerable from unanswerable
queries with AUC 0.931** (means: 0.777 answerable, 0.646 unanswerable). That is
a labeled diagnostic, not a product metric — but it is enough to build an
honest refusal on.

Rekoll already refuses rather than bluffs when it cannot rank (ADR-0024: on an
embedder mismatch, refuse the vector leg, serve lexical, and say so in
`RecallResult.mode`). Returning `k` hits for a question the store cannot answer
is the same class of bluff, one layer up.

## The trap this ADR exists to avoid

A `min_score` that thresholds `hits[0].score` would be **worthless, and worse,
plausible**.

After Reciprocal Rank Fusion, a `QueryHit.score` is the *fused rank score* —
`sum(1/(60 + rank))` over the legs, empirically ~0.01–0.03 — a number with no
relation to similarity. It ranks; it does not measure. Attach a reranker and the
score becomes a cross-encoder logit instead. The AUC 0.931 evidence is measured
on **neither**: it is measured on the *vector leg's top-1 cosine*, which exists
only *before* fusion, and which the sqlite adapter's `vector_query` already
returns as `QueryHit.score`.

So the cosine must be captured where it is still a cosine, and threaded out
explicitly. Any design that reads the number off the returned hits is measuring
the wrong quantity while looking exactly like it is measuring the right one.

## Decision

An **opt-in abstain gate**: `recall(query, min_score=...)` (and
`hybrid_search(..., min_score=...)`), default `None` = off, changing nothing for
existing callers.

1. **The gate is a floor on the vector leg's top-1 cosine**, captured
   pre-fusion, over the hits that would actually be allowed to surface. If no
   surfacable memory is at least `min_score` similar to the query, the search
   abstains: **zero hits**, `abstained=True`.

2. **It is a query-level decision, not a per-hit filter.** "Is anything in this
   store about this at all?" is precisely the question the AUC 0.931 evidence
   answers. Nothing is claimed about the calibration of the 2nd..kth hit's
   cosine, so nothing is filtered on it. A per-hit floor would have been the
   more *featureful* design and the less *defensible* one.

3. **An abstain short-circuits.** The lexical leg, fusion, and the reranker
   never run. A pipeline that did not run may not be named in `mode`.

4. **An abstain announces itself.** `RecallResult.abstained` is `True` and
   `mode` carries the numbers:

   ```
   vector: abstained (top-1 cosine 0.646 < min_score 0.700)
   ```

   An empty store, by contrast, reports `vector+lexical; min_score not applied
   (no vector candidates)` with `abstained=False`. **Zero hits from an abstain
   and zero hits from an empty store are never the same result.** This is the
   binding requirement: honest degradation, never a fabricated emptiness.

5. **`top_vector_score` is published on every ordinary recall**, gate or no
   gate. It is the gate's input, so surfacing it *is* the "documented threshold
   recipe": run ungated, look at the score on queries you know are answerable
   and on queries you know are not, put `min_score` between them. Copying 0.70
   from this ADR is not the recipe — that number belongs to one embedder and one
   corpus. On a 4-document scope with the same embedder, 0.70 **false-abstains**
   an answerable query whose top-1 cosine is 0.694, while a threshold read off
   `top_vector_score` (0.569) separates the classes perfectly. The test
   `test_real_embedder_separates_answerable_from_unanswerable` pins this.

6. **`min_score` is validated as a cosine** (`-1.0 <= min_score <= 1.0`), which
   rejects the most likely misuse outright: passing a fused score someone read
   off a hit.

### Degraded modes: every one is defined

The gate is evaluated only when a cosine exists *and means what `min_score`
means*. When it cannot be, the gate is neither silently skipped nor guessed —
`gate` carries an `"unavailable: ..."` reason, `mode` gains a
`min_score not applied (...)` clause, and the hits are returned **ungated**.

| Condition | Gate | Behavior |
|---|---|---|
| Normal cosine vector leg | `pass` / `abstain` | Gate decides |
| No vector leg (`use_vector=False`, or embedder mismatch, ADR-0024) | `unavailable: no vector leg` | Ungated hits + **warning**. No cosine exists to threshold. |
| `adapter.distance_metric != "cosine"` | `unavailable: non-cosine metric` | Ungated hits + **warning**. `top_vector_score` stays `None` — a non-cosine score is never published as one. |
| Vector leg returned no surfacable candidate | `unavailable: no vector candidates` | Ungated hits, no warning. Nothing to score means nothing to abstain *from*. |
| Stub embedder | `pass` / `abstain` | **Gate runs.** See below. |
| Reranker attached | `pass` / `abstain` | Gate decides on the cosine, pre-rerank. The reranker only reorders survivors. |

Returning ungated hits rather than abstaining, in the three `unavailable` rows,
is the deliberate call. Abstaining on a quantity that was never measured is the
same bluff in the opposite direction — and it would turn ADR-0024's *documented,
working* lexical-only degradation into a dead store for any caller who sets
`min_score` globally. The mode string is the contract, and it tells the truth.

**Stub embedder.** `StubEmbedder` hashes whitespace tokens into a signed
bag-of-hashed-tokens vector. Its cosine is a real cosine of real vectors — it
just measures *token overlap*, not meaning. The gate therefore runs, and on a
paraphrase it **fails closed**: a semantics-free embedder cannot clear a
semantic threshold, so it honestly refuses instead of guessing. `mode` already
carries `(stub-embedder)` to explain why the number is not what a reader might
assume. Failing closed is the right direction for a gate whose entire purpose is
to withhold.

## Consequences

- Agents can, for the first time, be told "I don't know" by the memory layer,
  and can tell that apart from "I am empty" and from "I am degraded."
- **`adapter.distance_metric` stops being decorative.** The gate thresholds the
  vector score *as a cosine* on the strength of that declaration, and
  `StorageAdapter` defaults it to `"cosine"` — so a backend scoring on another
  scale that never overrides the default would have a plausible-looking number
  compared against a cosine-calibrated threshold. The conformance suite now
  verifies the claim (`assert_distance_metric_honest`): if an adapter says
  `"cosine"`, a record scored against its own embedding must come back as
  exactly 1.0, and every score must lie in [-1, 1]. Third-party adapters that
  were quietly wrong will now fail conformance. That is the point.
- The gate always reads **exactly the set the search is about to return**. With
  `include_quarantined=True` (forensics), quarantined hits will surface, so
  their cosines legitimately hold the gate open. With the default surfacing
  filter they cannot, so a quarantined near-match cannot bluff the gate.
- The read path stays **zero-LLM and zero-write** (ADR-0007). The gate is a
  comparison of two floats. An abstain surfaces no ids, so it credits nothing to
  the was-it-used ledger — which is correct: nothing was surfaced to be used.
- `hybrid_search` now returns a `FusedResult` (a `QueryResult` subclass carrying
  `abstained` / `top_vector_score` / `gate`). Existing callers read `.hits` and
  are unaffected. The adapter contract (`QueryResult`) is untouched: abstention
  is a retrieval-layer concept and does not belong in the storage interface.
- `health()` and `self_test()` never pass `min_score`. A probe of the index must
  not be able to abstain, or a healthy scope could read as a broken one.

## Caveats, stated plainly

- **The gate is evaluated before content-hash verification** (ADR-0019 runs
  post-fusion). A tampered record with a high cosine can therefore hold the gate
  *open* — after which it is still withheld from the results, as always. The
  failure mode is a non-abstain that returns fewer/weaker hits, never a leaked
  record. Fail-open on the gate, fail-closed on surfacing.
- **An abstain suppresses lexical hits.** Records stored without a vector (e.g.
  written during an embedder mismatch, ADR-0024 §2) are reachable lexically but
  invisible to the gate, which reads only the vector leg. A gated recall can
  therefore abstain on a query that the lexical leg would have answered. Callers
  who need those hits should not gate, or should `reindex()`.
- **`context()` does not reveal an abstain.** Per `RecallResult`'s existing
  contract, `mode` is deliberately *not* rendered into the envelope, so agent
  prompt caches are not busted by a mode string. An abstained `context()` is an
  empty envelope, indistinguishable from an empty store's. Callers gating on
  `min_score` must read `abstained` / `mode`. `Memory.context()` takes no
  `min_score` and so can never abstain.
- **The `unavailable` warnings fire once per call site**, not once per call —
  that is Python's default warning filter, not a Rekoll choice. This is why the
  per-call contract is `RecallResult.mode` / `gate`, which are recomputed and
  truthful on *every* recall, warning or not. Never branch on the warning.
- **AUC 0.931 is a labeled diagnostic on one fixture, one embedder, one corpus
  size.** It establishes that the signal exists and is strong. It is not a
  product SLA, and 0.70 is not a default — the gate ships **off**.
