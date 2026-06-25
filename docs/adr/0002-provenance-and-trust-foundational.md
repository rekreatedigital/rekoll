# ADR-0002 — Provenance + trust are foundational and immutable to LLMs

**Status:** Accepted · **Date:** 2026-06-23

## Context
Memory poisoning (OWASP ASI06; MINJA >95%, AgentPoison >80% against undefended
stores) is the field-wide blind spot. Trust cannot be bolted on later — it must
be assignable at the moment data enters and must survive every transformation.

## Decision
- Every record carries **NOT-NULL** provenance and a `trust_tier`, set at the
  ingestion boundary, **never written or raised by LLM output**.
- Trust is an ordered enum: `QUARANTINED(0) < UNVERIFIED(1) < TRUSTED_SOURCE(2)
  < CURATED(3) < OWNER(4)`.
- Trust is assigned by the **ingestion source** (and later the firewall), not
  inferred from content. A derived memory inherits `min(parents)` — a poisoned
  low-trust chunk can never launder itself into a higher tier.
- The storage contract refuses to persist a record without these fields, and the
  conformance suite checks trust round-trips losslessly.

## Consequences
- A later firewall/graduation layer can rely on trust being present and honest.
- Elevation to a trusted directive only ever happens through an explicit,
  human-reviewable step (a future phase) — never automatically.
