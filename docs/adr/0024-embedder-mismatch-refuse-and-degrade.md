# ADR-0024 — On embedder-identity mismatch: refuse the vector leg, degrade honestly

**Status:** Accepted · **Date:** 2026-07-02 · **Amends:** ADR-0014 (warn-only), ADR-0009 (identity guard)

## Context
Rekoll records a per-scope embedder identity (name, dim, config_hash) and checks
it on every `Memory` open (ADR-0009). What to DO on a mismatch was left open:
DESIGN §10 (P1) wanted a hard-fail; the `Memory` facade shipped warn-only
(ADR-0014, "a model swap warns, not corrupts"); the DESIGN behavioral note
called the authoritative behavior "an open decision."

Warn-only is not enough. A silent model/config swap is the classic silent
recall killer: vectors from two embedders live in one space that ranks them as
if comparable, and recall quality collapses with no error anywhere — the
symptom is "the agent just got dumber." Production experience in the author's
prior memory system bore this out; its recall path asserts the stored embedder
identity against the pinned model and *refuses* the semantic leg on mismatch
rather than trusting whatever cosine says. A `warnings.warn` at construction
is routinely invisible in exactly the environments that matter (daemons,
notebooks re-running cells, agent hosts capturing stdout).

Hard-fail is too much. The store still holds every byte of content, and the
lexical arm is fully sound — killing all reads over a fixable indexing problem
turns a degradation into an outage (and pushes users to delete their data to
get unblocked).

## Decision
On identity mismatch, **refuse the vector leg and degrade honestly**:

1. **Reads:** `recall()` never embeds the query and never calls
   `vector_query`; it serves lexical-only results and says so in
   `RecallResult.mode` (`"lexical-only: embedder mismatch"`). On a backend
   with no lexical arm the result is honestly empty (`"none: embedder
   mismatch"`) — an empty answer over a garbage answer.
2. **Writes:** genuinely new content is stored WITHOUT a vector (lexical still
   indexes it). Writing a second vector family into the scope would deepen the
   very corruption the guard exists to stop. Crucially, an embedding-less write
   never NULLS an existing vector: ids are content-addressed, so re-ingesting
   identical content lands on a row that may already carry a good (pre-swap)
   vector, and the write path carries that stored vector forward on the
   in-place upsert. Otherwise the "just re-ingest" recovery advice would
   *destroy* the very vectors recovery needs.
3. **Loud, twice:** the construction-time warning stays (full identity on both
   sides, since a dim/config-only swap under the same name must be visible),
   and `health()` reports `ok=False` with `identity="mismatch"` until the
   scope is reindexed.
4. The low-level `guard_identity()` (hard-fail) remains available for callers
   who want construction to raise.

## Recovery: `Memory.reindex()`, not "re-ingest"
The honest recovery is a first-class facade method, **`Memory.reindex()`**: it
re-embeds every in-scope record with the embedder you are holding now and THEN
rebinds the scope's stored identity to it. Vectors are written FIRST and the
identity is rebound LAST, so a crash midway leaves the scope still-mismatched
(safe, degraded) rather than identity-clean over half-stale vectors; the write
is idempotent (same content-addressed ids, unchanged trust, so the
trust-monotonic upsert of ADR-0023 updates each row's embedding in place —
neither dropping trust nor nulling the fresh vector). After `reindex()`,
`health().ok` is `True` and `RecallResult.mode` returns to `vector+lexical`.

"Just re-ingest the scope" is explicitly NOT the recovery: under the mismatch a
re-ingest of identical content stores no vector (see 2). The construction
warning and the `health()` note both direct the caller to `reindex()`.

## Consequences
- Recall can no longer silently return confidently-wrong rankings after a
  model swap — the failure mode becomes visible (mode string, health check)
  and bounded (lexical still works).
- A mismatch is now *recoverable in place* rather than a permanent trap: the
  refuse-the-leg degradation is temporary, cleared by `reindex()`, and cannot
  destroy the pre-swap vectors in the meantime.
- A vector-only backend under mismatch returns empty results until it is
  reindexed; that is deliberate (see 1). Such an adapter must be able to
  enumerate its records for `reindex()` to run, else recovery is re-ingesting
  the sources into a fresh store.
- `RecallResult.mode` (honest degradation) is the contract agents/hosts read
  to avoid bluffing on a broken index; docs and the MCP/REST doors should
  surface it rather than hide it.
- Supersedes ADR-0014's warn-only wording and closes the DESIGN §10 open
  decision: neither warn-only (invisible) nor hard-fail (outage) — refuse the
  broken leg, keep the honest ones, and give a real way back.
