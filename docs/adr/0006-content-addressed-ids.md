# ADR-0006 — Content-addressed IDs make ingestion idempotent

**Status:** Accepted · **Date:** 2026-06-23

## Context
Non-technical users *will* run an import twice. If that duplicates their memories,
trust evaporates. We need idempotence guaranteed by the data model, not by careful
caller behavior (2RD enforced uniqueness only by prose convention, which drifted).

## Decision
- A record's primary `id` is **content-addressed**:
  `rk_` + sha256(scope_key | source_uri | content_hash)[:24].
- A `UNIQUE(scope_key, content_hash)` constraint backs it at the storage layer.
- `upsert` is therefore idempotent by construction; re-ingesting identical content
  into the same scope updates in place rather than duplicating. `add` is strict
  (raises on a duplicate) for callers that want that.
- A separate human-facing `MEM-NNNN` id keeps the future git-auditable views legible.

## Consequences
- Imports and re-syncs are safe to repeat; the conformance suite asserts this.
- Editing content changes the id (it's new content) — links/history must therefore
  reference ids explicitly (handled by `record_links`), which we already do.
