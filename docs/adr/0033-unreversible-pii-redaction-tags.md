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

Store a value fingerprint ONLY when the matched value is **provably
high-entropy**; otherwise store the **class name alone**
(`firewall._redaction_tag`, gated by `_HIGH_ENTROPY_SECRET_NAMES`):

- **Provably-high-entropy, format-specific secrets** — the cloud/API-key,
  token, JWT, and PEM-body patterns (listed in `_HIGH_ENTROPY_SECRET_NAMES`) —
  keep the correlation fingerprint `name:sha256:<12 hex>`. Their FORMAT
  guarantees ≥ 128 bits, so the digest is not enumerable and cross-record
  correlation ("the same credential leaked here and there") is real audit value
  the store can carry safely.
- **Everything else stores the class name ALONE**, with no value-derived token:
  all **PII** (`email` / `us_ssn` / `phone`), AND the two **generic credential
  catch-alls** `credential_assignment` / `connection_string`. Those match an
  arbitrary `key=value` / `user:pass@host` whose captured value is user-supplied
  and may be low-entropy — a phone written as `password: 555-123-4567`, or a weak
  DSN password — so a hash of the match is reversible just like a bare SSN.

This is information-theoretic, not a tuning choice: ANY deterministic token of a
low-entropy input is brute-forceable, so truncating or re-hashing cannot help.
The class-only tag preserves the one signal the product actually consumes (how
many values, of what class, were redacted) while storing nothing an attacker can
reverse. The allowlist is **safe-by-default**: a newly-added pattern is
class-only until it is explicitly proven high-entropy and added to the set (a
test pins that every name in it is a real secret pattern and none is a PII name).

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

An adversarial re-audit of the fix surfaced three follow-ups, folded in here:

- **Generic credential catch-alls are class-only too (F1).** `password:
  555-123-4567` was caught by `credential_assignment` *before* the PII pass and
  stored `credential_assignment:sha256:…` — a reversible hash of a phone (verified
  repro). Reclassifying the two catch-alls closes this; a discriminating test
  (`test_generic_credential_catchalls_never_store_a_reversible_fingerprint`) pins
  it. The format-specific secret patterns are unaffected.
- **Redaction is content-scoped (F2).** It rewrites the stored *content* only —
  not the caller-supplied `source` / `metadata` or an ingested file's path, which
  are structural provenance stored verbatim (auto-scrubbing a path like
  `src/jane/util.py` would corrupt "which file did this come from?"). Documented
  in `Memory.__init__`, `docs/QUICKSTART.md`, and `docs/MCP.md`; callers must keep
  PII out of those fields. A future opt-in to scrub provenance is possible but out
  of scope — it is a distinct design decision with real provenance-quality costs.
- **`screen=False` + `redact_pii=True` now warns (F3).** Redaction runs inside the
  firewall screen, so disabling the screen silently no-ops redaction. Rather than
  block (project posture: warn loudly, never restrict), the constructor warns that
  redaction has no effect, with a test proving the warning is honest.

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
