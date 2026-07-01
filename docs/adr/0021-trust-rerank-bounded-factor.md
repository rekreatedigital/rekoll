# ADR-0021 — Trust-aware re-rank is a bounded (~±20%) factor, not a hard weight

**Status:** Accepted · **Date:** 2026-07-02 · **Implementation:** future work

## Context

DESIGN told two conflicting stories about how `trust_tier` influences read-time
ranking (the round-2 audit flagged it, DESIGN §0/§13.3):

- §6 item 4: hard tier weights **TRUSTED=1.0 / MEDIUM≈0.7 / LOW≈0.4** — a ~60%
  spread that heavily suppresses low-trust content.
- §3/§7: a small multiplicative factor **capped near ±20%**.

Both cannot hold. The re-rank is not yet implemented (DESIGN §0), so this ADR
picks the story before code exists — where the auditor's judgment and mine
agree the spec should land.

## Decision

Trust contributes a **bounded multiplicative factor, capped near ±20%**, as one
term in `CE_norm × recency × temporal × proof × trust`:

- OWNER/CURATED sit at the top of the band, UNVERIFIED is lightly penalized,
  and the factor never drops a legitimate low-trust hit by more than ~20%.
- **QUARANTINED is excluded before ranking** (existing behavior), not modeled as
  a weight of 0 — exclusion is categorical, not a nudge.
- The exact per-tier values within the band remain an empirical tuning task
  (DESIGN §13.3), but the *shape* is now fixed.

Rationale: the load-bearing anti-poisoning defense is **structural** — directives
render only from the trusted tier, and quarantined memory never surfaces (both
shipped, ADR-0013). A hard 0.4 low-trust weight would fight the very goal
DESIGN §13.3 states ("without crushing legitimate low-trust recall") for no
security gain the structure doesn't already provide. A bounded factor nudges an
embedding-optimized trigger toward evidence without gutting benign UNVERIFIED
recall.

## Consequences

- DESIGN §0, §6.4, §7, §13.3 updated to the single bounded-factor story; the
  1.0/0.7/0.4 triple is removed as a spec value.
- Implementation stays future work (no ranking weights ship in this change);
  when built, it must keep the trust factor inside the ±20% band and keep
  QUARANTINED exclusion categorical, with the attack corpus (ADR-0020) proving
  down-ranking does not become the *only* thing standing between a trigger and
  the directive channel.
