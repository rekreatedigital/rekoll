# ADR-0038 — Stored vectors are materialized lazily on the read path

**Status:** Accepted · **Date:** 2026-07-25 · **Implements:** issue #43 · **Extends:** ADR-0030 (cached exact vector scan) · **Interacts with:** ADR-0005 (adapter contract), ADR-0019 (read-time content verification), ADR-0024 (honest degradation), ADR-0028 (abstain gate)

## Context

Retrieval fuses a pool of `6*k` candidates from **each** leg, so a `k=8` recall
reconstructs ~96 `MemoryRecord`s in order to return 8. RRF ranks on `id` and
`score` and never touches `.embedding`. Every pooled record nonetheless
json-decoded a 384-float vector, validated it, and boxed it into a tuple — ~88 of
which were ranked away and discarded. Measured on a 1,000-record corpus, that was
about half of what remained in read latency once ADR-0030 removed the scan cost:
p50 30.1 ms, with 96 decodes per warm recall.

## Decision

Adapters may hand `MemoryRecord` a `DeferredEmbedding` thunk in place of a decoded
vector. `.embedding` is a data descriptor that materializes on first read and
caches the result; a vector nobody reads is never decoded.

- The thunk must decode through the adapter's **validated** path and return a
  finished `tuple[float, ...]`. In exchange the model skips its eager
  element-by-element re-coercion, which is redundant once `json.loads` has already
  produced floats (issue #43's tail observation).
- **Deferred, never omitted.** The pool records *are* the records `recall()`
  returns, so handing back `embedding=None` for pooled rows would silently null a
  public field. Deferral costs a caller who reads the vector nothing beyond the
  decode they were already paying for.
- Records stay picklable via `__getstate__`/`__setstate__`, which materialize on
  the way out and store under the **public** field name, so pickles round-trip in
  both directions across this change.
- This is **not** an adapter-contract capability: an adapter that keeps decoding
  eagerly remains fully conformant. No ADR-0005 plumbing, no `conformance.py`
  change, no third-party adapter breakage.

## Consequences

- Recall p50 roughly halves on the measured corpus (30.1 ms → 14.1 ms with
  bge-small; 96 decodes → **0**), with **bit-identical ranking** — the ranking math
  never read `.embedding`, which was the whole premise.
- `.embedding` returns the same value and the same type for every caller.
  Deferral is invisible except in timing.
- **A corrupt cell now raises at first read rather than at record construction.**
  Two consequences follow, and both are handled deliberately rather than absorbed:
  - `Memory.health()` reads `.embedding` in its own loop, outside the try that
    used to absorb the error inside `newest()`. Unguarded, that broke health's
    headline "never a propagated exception" contract. The read is now fail-soft
    like the retrievability probe beside it: an undecodable vector counts as
    not-embedded (hence stale, hence not ok) and earns a note naming possible
    tampering — which is what health exists to *report*.
  - A read that uses **no** vector (a scope degraded to lexical-only by an
    embedder-identity mismatch, ADR-0024) no longer raises on a corrupt cell: it
    returns its content-hash-verified hits instead. That is not a silent-*wrong*
    result — the answer is unaffected by a vector nobody consulted — but it is a
    silent one, so `_decode_embedding`'s docstring now states the scope of "fails
    visibly" precisely, and `health()` is the honest channel for the corrupt cell.
    Both halves are pinned by tests.
- First materialization is not atomic: concurrent readers may each decode and
  receive equal-but-distinct tuples. The decode is pure and idempotent, so this is
  benign.
- `record.__dict__` now carries `_embedding` rather than `embedding`. Nothing in
  this repo reads `__dict__`; `asdict`, `replace`, `repr`, `==` and pickling are
  all unaffected. Callers who poke internals are the only ones who can see it.

## Alternatives rejected

- **An adapter hydration mode that omits the embedding for pool fetches.** The
  pool records are the returned records, so this silently nulls a public field for
  every `recall()` caller — a contract regression that ships green because nothing
  pins it. Rescuing it means re-hydrating survivors through a second `get()` after
  fusion, plus reconciling the in-memory quarantine demotion `_verify_hits`
  applies to those same objects: more surface, more round-trips, strictly worse
  latency than deferring, and it needs ADR-0005 capability plumbing that deferral
  does not.
- **Shrinking the fusion pool.** Trades recall quality for latency and would need
  a full efficacy re-run to justify. Unnecessary once the waste is free to remove.
