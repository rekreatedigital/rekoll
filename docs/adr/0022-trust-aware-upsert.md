# ADR-0022 — Trust-aware upsert: identical content is trust-monotonic

**Status:** Accepted · **Date:** 2026-07-02 · **Priority:** P0-0 (orchestrator-routed, highest in the effort)

## Context

Records are content-addressed: `id = f(scope, source_uri, content_hash)`, with a
DB `UNIQUE(scope_key, content_hash)` guaranteeing one row per (scope, content)
for idempotent re-ingestion (ADR-0006). The reference adapter's `upsert` uses
`INSERT OR REPLACE`.

**Confirmed by repro (not theory):** because the `id` includes `source_uri` but
the UNIQUE key does not, the SAME bytes from a DIFFERENT source produce a
different `id` but collide on `(scope_key, content_hash)`. `INSERT OR REPLACE`
resolved that collision by DELETING the existing row and inserting the new one —
so an **UNVERIFIED** source that re-ingested a **trust=OWNER** record's exact
content silently **replaced** it. The survivor kept the content but took the
attacker's `prov_source_uri` and the **lower** trust tier, and remained
recallable. That is a memory-poisoning / provenance-hijack vector and directly
contradicts Rekoll's "trusted provenance" promise (DESIGN §1/§6, "immutable to
LLM output", "structurally incapable of minting trust").

This is a storage-contract bug, not a facade bug: any backend implementing the
naive UNIQUE-upsert inherits it. So the fix lives in the adapter **and** in the
importable conformance suite every future backend runs.

## Decision

**Trust for identical content is monotonic — it may rise, never silently fall.**
On an `upsert` that collides on `(scope_key, content_hash)` with a row of a
*different* `id` (i.e. same bytes, different source):

- **incoming trust ≤ stored trust → no-op.** The trusted (or equal-trust
  incumbent) record is preserved untouched; the lower/equal-trust write is
  dropped. This blocks both the downgrade and an equal-trust provenance hijack.
- **incoming trust > stored trust → take over.** A strictly more-trusted source
  may legitimately replace the record (a real upgrade); the displaced id's
  orphaned fts/metadata/link rows are purged first, as before.

Same-`id` upserts (the *same* source re-ingesting — e.g. re-embedding after a
model swap) are unchanged: they update in place. `add()` (non-replace) still
raises on a duplicate content-address, so it was never a downgrade vector.

New conformance check `assert_upsert_is_trust_monotonic` encodes the contract for
all adapters; `ALL_CHECKS` includes it, so the SQLite reference and any
third-party backend are held to it identically.

## Consequences

- An attacker can no longer downgrade/hijack a trusted memory by replaying its
  bytes from an untrusted source; recall keeps surfacing the trusted record with
  its real provenance.
- Re-ingesting identical content from a *second, equal-or-lower* trust source is
  a dedup no-op (content already present at ≥ trust). `ingest_*` still counts the
  chunk it attempted; `count()` reflects the true stored total. This matches the
  existing content-addressed idempotency model (ADR-0006).
- Trust *elevation* for identical content is possible only by a strictly
  higher-trust ingestion — consistent with DESIGN §6.2 (trust never
  auto-elevates from untrusted signal) and the monotonic-trust spine.
- Not addressed here (documented, separate): a fully compromised backend with
  direct DB write access — that is the read-path `verify()` layer's job
  (ADR-0018) and remains the user's responsibility (SECURITY.md).
