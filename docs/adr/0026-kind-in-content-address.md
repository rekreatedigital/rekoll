# ADR-0026 — `kind` is part of the content address

**Status:** Accepted · **Date:** 2026-07-07 · **Amends:** [ADR-0006](0006-content-addressed-ids.md)

## Context

ADR-0006 derived a record's primary id from `scope_key | source_uri |
content_hash`. `kind` was not in the payload — but the canonical schema stores
each kind in a SEPARATE physical table (ADR-0001), and the child tables
(`record_metadata`, `record_links`) and the FTS index are keyed by record id
alone. So the same content saved as two kinds — plain public API,
`remember(x, kind=RAW_FACT)` then `remember(x, kind=OBSERVATION)` — produced
ONE id living in TWO tables, and everything keyed by that id cross-wired
(each symptom confirmed by repro):

- `get(id)` returned **two records for one id**;
- the second write **silently overwrote the first record's metadata**
  (`record_metadata` has no kind column);
- the second write **replaced the first record's FTS row**, making it
  lexically invisible;
- `forget(one_id)` **deleted both rows**.

## Decision

- `record_id()` hashes `scope_key | source_uri | kind | content_hash` (the
  `Kind` value string, e.g. `"raw_fact"`). `MemoryRecord.create` coerces
  `kind` before addressing and passes `kind.value`.
- Idempotency is unchanged where it matters: **same content + kind + source
  (in the same scope) → same id**; re-ingesting is still update-in-place, and
  the conformance suite still asserts it. Distinct kinds are now distinct,
  fully independent records (own metadata, own index rows, own deletion).
- This changes id derivation relative to pre-0026 stores. Accepted because
  Rekoll has no published users and dev DBs are gitignored. For any existing
  store: `UNIQUE(scope_key, content_hash)` dedup per table is untouched, so
  re-ingesting identical content dedups against the existing row (the
  trust-monotonic upsert keeps the incumbent id, ADR-0023) — no duplication,
  no migration step.

## Consequences

- The id format is unchanged (`rk_` + 24 hex chars); adapters need no schema
  change.
- A hypothetical pre-0026 id recorded outside the store (e.g. in a host's own
  logs) no longer matches the id a fresh ingest would mint — acceptable at
  this stage (see above), and worth a release note when the first version
  ships.
- ADR-0006's formula is amended in place with a pointer here.
