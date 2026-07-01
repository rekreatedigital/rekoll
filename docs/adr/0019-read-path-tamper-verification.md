# ADR-0019 — Recall verifies content hashes; mismatches are withheld, not repaired

**Status:** Accepted · **Date:** 2026-07-02

## Context

SECURITY.md and DESIGN §13.12 name content-hash verification as the
*detection layer* against an attacker with direct write access to the user's
own database (who bypasses ingest screening entirely). The primitive existed —
`MemoryRecord.verify()` — but the round-2 audit confirmed (P2-7) that nothing
called it: a direct `UPDATE` of a record's content was served on recall,
verbatim, at its original trust.

## Decision

`hybrid_search` verifies every fused candidate before surfacing:

- **Always on, no knob.** SHA-256 over ≤ pool-size (~60) records of ≤ ~2k
  chars is microseconds per query; the recall benchmark gate stayed green.
  A toggle would only exist to be wrong.
- **Withhold + warn, never repair.** A mismatched record is demoted to
  `Status.QUARANTINED` **in memory** and falls out through the existing
  quarantine filter; one `UserWarning` per query lists the withheld ids with
  the remediation (re-ingest or delete). `include_quarantined=True` (debug)
  still surfaces it — flagged — for forensics.
- **No write-back on the read path.** Reads stay side-effect free: a read-only
  replica must not fail recall, and persisting a demotion is meaningless
  against an attacker who can rewrite it anyway.

## Consequences

- Detection, not prevention: an attacker who can write content can also
  recompute `content_hash` (it is not keyed). The honest claim — made in
  SECURITY.md — is that *naive* tampering is caught; a keyed/signed trusted
  tier (DESIGN §13.10) is the escalation path and out of scope here.
- Tampered records silently drop out of results (plus a warning); callers who
  pinned exact hit counts against a hand-tampered store will notice — that is
  the point.
