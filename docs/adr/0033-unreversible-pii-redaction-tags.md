# ADR-0033 — PII redaction stores a class-only, non-reversible audit tag

**Status:** Accepted · **Date:** 2026-07-14 · **Amends:** ADR-0022 (PII redaction opt-in)

## Context

When redaction runs, the firewall records an audit tag per redaction in
`metadata['redactions']` (`firewall.screen` / `screened_record`). The tag was a
truncated SHA-256 "fingerprint" of the **raw** value — `name:sha256:<12 hex>` —
for both secrets and PII (ADR-0022: *"PII redactions are fingerprinted … identical
machinery to secrets"*).

For high-entropy **secrets** (API keys, tokens, DSNs, private-key bodies — ≥ 128
bits) that fingerprint is safe: the digest is not enumerable, so it works as a
stable correlation token ("the same credential leaked here and there") without
the store ever holding the secret.

For low-entropy **PII** it is not. The US SSN space is ~1e9 and a NANP phone
~1e10. Anyone with **DB read access** can hash every candidate offline in seconds
and match the stored digest — recovering the exact value. Verified against the
shipped code: a stored `us_ssn:sha256:…` tag was brute-forced back to the raw SSN
from its 12-hex prefix. A "fingerprint" of an SSN simply *is* the SSN, and a
targeted email is a confirmable guess. So the redaction that was supposed to
protect PII leaked it to exactly the party redaction defends against, and the
field comment ("fingerprints, never the raw secret") was a latent lie for PII.

Fingerprints are, in practice, **audit-only**: the sole consumer in the codebase
is the CLI redaction note, which counts them and names the class. Nothing looks a
record up by fingerprint; nothing dedups on it.

## Decision

Split the audit tag by class (`firewall._redaction_tag`):

- **Secrets** keep the correlation fingerprint `name:sha256:<12 hex>` — safe
  because the space is not enumerable, and cross-record correlation has real
  audit value.
- **PII** (every class in `_PII_PATTERNS`: `email` / `us_ssn` / `phone`) stores
  the **class name ALONE**, with **no value-derived token**.

This is information-theoretic, not a tuning choice: ANY deterministic token of a
low-entropy input is brute-forceable, so truncating or re-hashing cannot help.
The class-only tag preserves the one signal the product actually consumes (how
many values, of what class, were redacted) while storing nothing an attacker can
reverse. The split is derived from the existing `_SECRET_PATTERNS` /
`_PII_PATTERNS` seam, so a new PII class is covered automatically.

## Consequences

- **The redaction promise is now honest for PII.** `metadata['redactions']` holds
  `email` / `us_ssn` / `phone` — no reversible material. A discriminating test
  (`test_pii_redaction_tag_is_not_a_reversible_fingerprint`) reconstructs the old
  tag and asserts it is absent; the prior `email:sha256:` assertion is inverted.
- **Secret audit correlation is unchanged.** Secret fingerprints stay
  `name:sha256:…` (consolidation/tamper/firewall tests unaffected).
- **Cross-record PII correlation is dropped** — you can no longer tell "the same
  SSN appears in N records" from the audit trail. No code used that, and it is
  not worth leaking the SSN to keep.
- The CLI redaction note is reworded from "audit fingerprints kept" to "an audit
  tag is kept, never the value" — accurate for both classes now.
- ADR-0022's "PII fingerprinted, identical machinery to secrets" line is
  **superseded** by this ADR.

## Alternatives rejected

- **Keyed / salted HMAC of the PII value.** Only helps if the key is secret from
  the attacker. In a local-first single-file store the key would live right beside
  the data it "protects" (or in a backup/export that travels with it), and a
  per-process salt would destroy the cross-record correlation that is the
  fingerprint's only purpose — added key-management burden for a zero-config tool,
  no real protection under the stated threat (DB read access), and still no
  protection under full-machine access. Rejected.
- **Drop only the acutely-enumerable classes (SSN, phone), keep an email
  fingerprint.** A targeted email hash is still a confirmable guess, and the clean
  seam is "PII vs secret", not "SSN/phone vs email". Uniform class-only for all
  PII is simpler and strictly safer.
- **Drop the audit trail for PII entirely.** Loses the count/class signal the CLI
  surfaces for no security gain — the class name alone is already non-reversible.
