# ADR-0023 — Trust-aware upsert: identical content is trust-monotonic

**Status:** Accepted · **Date:** 2026-07-02 · **Priority:** P0-0 (orchestrator-routed, highest in the effort)

## Context

Records are content-addressed: `id = f(scope, source_uri, content_hash)` (since
ADR-0026 the payload also includes `kind`), with a
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

The rule is applied to **any** `(scope, content_hash)` collision, regardless of
whether the incoming record shares the prior row's `id` (same source) or not:

- **incoming trust < stored trust → no-op.** The trusted incumbent is preserved
  untouched. This blocks the cross-source downgrade AND the same-source
  re-ingest-at-a-lower-default downgrade (see re-ingest note below).
- **incoming trust == stored trust, different source → no-op** (dedup; keep the
  incumbent, no equal-trust provenance hijack).
- **incoming trust == stored trust, same source → update in place** (the
  idempotent re-ingest / re-embed path).
- **incoming trust > stored trust → take over** (a real upgrade); if the id
  differs, the displaced id's orphaned fts/metadata/link rows are purged first.

`add()` (non-replace) still raises on a duplicate content-address, so it was
never a downgrade vector.

**Re-ingest note (important):** trust is monotonic, so re-running `ingest_path`
/ `ingest_text` over content already stored at a HIGHER trust is a no-op for
those records — it will not lower their trust even if the re-ingest uses a lower
default. This is deliberate: it stops a re-ingest at the safe UNVERIFIED default
(ADR-0016) from silently downgrading — and, for content that quotes injection
phrases, from getting quarantined and vanishing from recall. To genuinely
*lower* a record's trust, delete it and re-add at the new tier; trust never
falls implicitly.

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
  (ADR-0019) and remains the user's responsibility (SECURITY.md).
