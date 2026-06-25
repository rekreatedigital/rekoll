# ADR-0001 — Separate physical tables per kind; no unbounded JSON

**Status:** Accepted · **Date:** 2026-06-23

## Context
Memory has three (plus one) lifecycle-distinct kinds — raw facts, consolidated
observations, directives, and episodes. Hindsight shipped a single
`memory_units` table + a `fact_type` discriminator and paid for it: 80+
migrations, a literal `observations↔mental_models` rename, and an in-row JSONB
history column that hit Postgres's 256 MB limit (SQLSTATE 54000) and bricked rows.

## Decision
- Store each kind in its **own physical table** (`verbatim_records`,
  `observations`, `directives`, `episodes`). `kind` is a *logical* discriminator.
- **No unbounded JSON** anywhere. Metadata is flat scalars in a bounded child
  table (`record_metadata`); links are typed rows in `record_links`. Any growing
  per-record structure becomes a bounded child table with a write-time cap.

## Consequences
- Each kind evolves independently; the verbatim provenance root stays inviolable.
- The reference SQLite adapter embodies this layout. Other adapters MAY lay out
  storage differently but MUST preserve the same record semantics (conformance).
- Slightly more tables/SQL than a single-table design — accepted in exchange for
  avoiding the churn-and-brick failure mode above.
