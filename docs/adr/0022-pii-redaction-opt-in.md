# ADR-0022 — PII redaction (email/SSN/phone) is opt-in; secrets stay default-on

**Status:** Accepted · **Date:** 2026-07-02

## Context

DESIGN §6.6 / §14 park a v1 decision: should the write-path scrub redact PII
(email, SSN, phone) by default, or only on request? Secrets (API keys, tokens,
private keys, DSNs) are already redacted unconditionally — a live credential in
the index is never wanted.

PII is different. Rekoll's core job-to-be-done is *"understand my whole
codebase"*. Code and git history are saturated with legitimate emails — author
lines, `CODEOWNERS`, `mailto:` links, changelog attributions, commit trailers —
and with number sequences a phone/SSN regex will happily mangle. Default-on PII
redaction would corrupt exactly the content users ingest Rekoll to search,
silently degrading recall and provenance ("who decided X?" → `[REDACTED:email]`).

## Decision

- **Secrets: always redacted** (unchanged) — defense in depth, never a live
  credential at rest.
- **PII: opt-in** via `Memory(redact_pii=True)` (threaded to
  `screen(..., redact_pii=...)` / `screened_record`). Default **False**.
- Patterns are conservative and separator-anchored to keep false positives low
  even when enabled: email (standard), US SSN (dashed `ddd-dd-dddd` only — a
  bare 9-digit run is too ambiguous), phone (two separators required, so
  version strings / ports / IPs / bare digit runs don't trip). PII redactions
  are recorded in the audit trail, never leaked raw. (**Superseded by ADR-0033:**
  this ADR originally fingerprinted PII with "identical machinery to secrets", but
  a truncated-hash fingerprint of a *low-entropy* value is reversible by brute
  force. PII now stores a **class-only** tag — `email`/`us_ssn`/`phone` — with no
  value-derived token; secrets keep their non-enumerable fingerprint.)

## Consequences

- Default behavior is unchanged: ingesting a repo keeps author emails intact,
  so provenance and recall stay whole. A test pins this (default-off leaves
  `dev@example.com` in stored content).
- Users handling PII-bearing corpora (support tickets, CRM exports) flip one
  flag; a test proves email/SSN/phone are then redacted while benign
  `1.2.3` / `192.168.1.100` / order numbers survive.
- The regex floor is intentionally basic (DESIGN §14 "basic scrub in v1"); a
  higher-recall local NER remains the documented future upgrade (DESIGN §6.6),
  and stays local-only — never a remote API (ADR-0007).
- This settles the parked decision without expanding scope into the AI-provider
  layer, CLI, or MCP (owned by other sessions). *(Update 2026-07-14: the
  operator-only CLI `--redact-pii` and MCP `--redact-pii` /
  `REKOLL_MCP_REDACT_PII` launch switches later exposed this flag on both doors —
  never as an LLM-settable tool argument — and the audit-tag format was corrected
  in ADR-0033.)*
