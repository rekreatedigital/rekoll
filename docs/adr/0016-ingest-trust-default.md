# ADR-0016 — Bulk ingestion defaults to UNVERIFIED trust, never `default_trust`

**Status:** Accepted · **Date:** 2026-07-02

## Context

The firewall quarantines injection markers only when the source trust is
≤ UNVERIFIED (ADR-0013): a trusted author may legitimately write about
injection, so markers never quarantine trusted content. That design makes the
trust stamp the load-bearing input — and `Memory` stamped **every** write with
the constructor's `default_trust`, which defaults to OWNER.

The round-2 audit confirmed the consequence (P0-1): `ingest_path()` /
`ingest_text()` stamped third-party files as OWNER, so a file containing
"ignore all previous instructions…" was stored un-quarantined and recallable.
DESIGN §6 requires the opposite: *"Trust is assigned by the ingestion source …
so a db-row/email/web adapter is structurally incapable of minting a TRUSTED
memory."* Files on disk are exactly such a source — a cloned repo, a vendored
dependency, or a downloaded doc is third-party content the user never wrote.

## Decision

Trust defaults are **per verb**, matching who authored the content:

- `remember(content)` — a first-person statement typed by the user/agent
  developer — keeps the constructor's `default_trust` (OWNER by default).
- `ingest_text(...)` and `ingest_path(...)` — bulk third-party content —
  default to a new module constant `DEFAULT_INGEST_TRUST = TrustTier.UNVERIFIED`,
  **regardless of the constructor's `default_trust`**. A constructor-wide
  default must never silently exempt file ingestion from quarantine; vouching
  for a tree is a per-call, explicit act: `ingest_path(p, trust=TrustTier.CURATED)`.

Quarantine remains quarantine-not-drop: flagged chunks are stored with
`status=QUARANTINED` (auditable), count toward the returned chunk count, and
never surface in recall or the envelope.

## Consequences

- Ingesting a repo that *documents* injection (like Rekoll itself) at the
  default now quarantines those chunks. That is the correct fail-closed
  behavior: the system cannot distinguish docs-about-attacks from attacks.
  Owners opt their own trees up explicitly (`scripts/dogfood.py` now passes
  `trust=TrustTier.CURATED`).
- `Memory(default_trust=...)` is scoped to `remember()`; documented in the
  constructor and both ingest docstrings.
- Breaking behavior change from v0.x defaults, accepted pre-1.0: the previous
  behavior was the vulnerability.
