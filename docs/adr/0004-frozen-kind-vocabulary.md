# ADR-0004 — The memory-kind vocabulary is frozen at v0

**Status:** Accepted · **Date:** 2026-06-23

## Context
Renaming or repurposing core concepts after release is a documented disaster
(Hindsight's `observations↔mental_models` swap confused every reader and broke
migrations). The vocabulary must be chosen to be **lifecycle-distinct** and then
locked.

## Decision
Exactly four kinds, frozen, chosen by lifecycle (not topic):
- `raw_fact` — verbatim, immutable, the provenance root.
- `observation` — consolidated/derived knowledge (with proof count + trend).
- `directive` — a standing rule that steers the agent (highest blast radius).
- `episode` — a dated event/session sequence.

These names are fixed in the `Kind` enum and the schema. A dropped name is never
reused for a different concept.

## Consequences
- Readers and contributors get one stable mental model.
- `episode`'s storage shape (table vs. view over `raw_fact`) is an open detail,
  but the *name and meaning* are locked. New needs get new fields, not renames.
