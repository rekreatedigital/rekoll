# ADR-0013 — Injection firewall: deterministic ingest screen + read-time envelope

**Status:** Accepted · **Date:** 2026-06-23

## Context
Memory poisoning (OWASP ASI06; MINJA >95%, AgentPoison >80% against undefended
stores) is the field-wide blind spot and Rekoll's headline differentiator. The
defense must be deterministic (no LLM), on by default, and structural — not a
fragile keyword filter that an attacker tunes around.

## Decision
Two zero-LLM choke points, both building on the P0 provenance/trust spine.

**(1) Ingest screen** (`screen` / `screened_record`):
- Redact secrets/PII via prefix-anchored patterns; store a fingerprint, never the
  raw value — even from a trusted source (defense in depth).
- Sanitize unicode (NFKC + strip zero-width/bidi) so homoglyph/hidden tricks can't
  smuggle markers past the screen or the eye.
- Detect prompt-injection markers. **Trust decides the action:** markers from an
  UNTRUSTED source (≤ UNVERIFIED) quarantine the record (trust → QUARANTINED,
  status → QUARANTINED); a TRUSTED author may legitimately write about injection
  (these docs do), so markers don't quarantine trusted content.

**(2) Read envelope** (`build_envelope`): retrieved memory is framed as DATA, with
`directives` populated only from the trusted tier + DIRECTIVE kind and everything
else as `evidence`; quarantined memory never surfaces in any channel; and a
memory's attempt to forge the envelope's own section markers / role tags is
neutralized. Retrieval (`hybrid_search`) excludes quarantined by default.

## Consequences
- The PRIMARY defense is structural: even an undetected injection lands as
  low-trust *evidence*, never in the instruction channel; trust only rises to a
  directive through a human-gated step (a later phase).
- A versioned attack corpus is the regression gate (mini version in tests now);
  the marker list can only grow, and ASR must only go down.
- The screen runs at the ingestion boundary; the SDK/MCP ingest path (later) calls
  `screened_record` so every external write is screened by default.
