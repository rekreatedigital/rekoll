# Security Policy

Rekoll ships memory-poisoning / prompt-injection defenses **on by default**. We
do **not** claim it is unbreakable — we claim defenses are on, documented, and
continuously tested against a public attack corpus. If you find a way to bypass
them, we want to hear about it privately first.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

- Preferred: GitHub **Private Vulnerability Reporting** (the "Report a vulnerability"
  button on the Security tab).
- Or email **security@rekreatedigital.com** with steps to reproduce.

We aim to acknowledge within **3 business days** and to agree on a disclosure
timeline with you. We credit reporters unless you ask us not to.

## Scope

In scope: memory-poisoning / injection bypasses, cross-scope (tenant) data
leakage, secret/PII exposure through stored memory, and supply-chain integrity
of released artifacts.

Out of scope (documented, not bugs): an attacker who already has **write access
to the user's own database** can bypass ingest-time screening. Recall
content-hash-verifies every candidate and withholds mismatches with a warning
(ADR-0019) — that catches *naive* tampering, but an attacker who rewrites
content can also recompute the unkeyed hash, and the digest covers **content
only**, so a direct-DB `UPDATE` of `trust_tier` or `status` is not detected at
all (it needs no recompute); a fully compromised backend is
the user's responsibility. See the threat model in
[docs/DESIGN.md](docs/DESIGN.md).

## What we promise

- **No telemetry.** The default install makes no outbound network calls on
  read/search. The privacy claim is architectural, not a policy we could quietly
  change.
- Reads never call an LLM. Local-and-private is the default path.
