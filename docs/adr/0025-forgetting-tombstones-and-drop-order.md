# ADR-0025 — Forgetting/compaction: kind-aware drop order, tombstoned evictions, loud budget breaches

**Status:** Proposed (design note — no implementation in this ADR) · **Date:** 2026-07-02

## Context
Rekoll will eventually need compaction: long-running agents accumulate memory
past any context/storage budget, and P3 (DESIGN §10) promises
learning/consolidation "without nondeterministic decay, off the read path."
Most memory layers compact by recency: oldest rows summarized or dropped
first, silently. Production experience in the author's prior memory system
showed both halves of that are wrong — recency-only compression eventually
evicts a standing rule or a security fact because it's *old* (age is exactly
what standing rules accumulate), and silent eviction means the first sign of
loss is an agent violating a constraint it used to know. That system's fix —
a type-aware do-not-compress floor, an append-only tombstone ledger holding
the full pre-compression text, and a loud banner instead of silent
cap-enforcement — ran nightly in production and is the shape adopted here.

This ADR fixes the design so P3 implements one coherent mechanism instead of
accreting ad-hoc eviction rules. Nothing in this ADR changes today's behavior:
Rekoll currently forgets only when the host explicitly calls `forget()`.

## Decision (design to implement in the forgetting/compaction phase)

### 1. Kind-aware drop order — directives are the invariant floor
Eviction pressure consults `Kind` (frozen vocabulary, ADR-0004) before age:

- **`directive` — never auto-evicted.** Directives are standing rules; they
  are the invariant floor ("do-not-compress"). They leave the store only by
  explicit host action (`forget`) or supersession (`status=SUPERSEDED`).
- **`raw_fact` — never silently dropped.** The verbatim tier is the source of
  truth the DESIGN promises always survives consolidation; under budget
  pressure raw facts may be *summarized into* observations only ADDITIVELY
  (original tombstoned if it must physically leave).
- **`episode` first, then `observation`.** Episodes (chatty session logs) age
  out first; observations are derived and re-derivable, so they go second —
  lowest-`proof_count`, oldest first within each kind.

The was-it-used signal feeds this: `proof_count > 0` (a memory that provably
informed an action) EXTENDS retention one tier within its kind. The signal is
promotion-only — usage can save a record, it can never demote another, and it
never touches trust (ADR-0002: trust is set at the ingestion boundary).

### 2. Every eviction is tombstoned to an append-only ledger
No byte leaves the store unrecorded. Each evicted/compacted record appends one
JSON line — full content, provenance key (`id`, `content_hash`,
`source_uri`), scope, kind, and the *gate* that dropped it (age-out /
cap-enforcement / summarized-into:{id}) — to a per-store, append-only
tombstone ledger (never re-read on the hot path; recovery and audit only).
Keyed by content-hash so replayed compaction runs are idempotent: one unique
drop, one ledger line, ever. A ledger write failure aborts that eviction —
losing data beats losing data *and* the record of losing it.

### 3. Budget breaches are loud, never silent
When a scope exceeds its budget and the droppable tiers are exhausted (all
that remains is floor), Rekoll must NOT quietly start eating the floor.
`health()` reports the breach (`ok=False`, a `budget` note), and compaction
surfaces a banner through its report — the human decides what leaves. Silent
loss is the one unrecoverable failure mode.

## Consequences
- Forgetting becomes reviewable: the tombstone ledger is the "what did I lose
  and why" audit trail, symmetric with provenance on the way in (ADR-0002).
- `proof_count` (already persisted, incremented by `Memory.mark_used`) gains
  its consumer; the was-it-used loop closes end-to-end.
- The floor means a scope can refuse to shrink below its directives +
  raw facts; that is deliberate — budget pressure escalates to a human, not
  past one.
- Implementation lands with the P3 consolidation phase; this ADR is its
  contract. Any interim eviction feature must follow it or supersede it here.
