# ADR-0007 — Zero telemetry by default, as an architectural guarantee

**Status:** Accepted · **Date:** 2026-06-23

## Context
We will badly want usage metrics. But the entire pitch is "your data never leaves
your machine." The moment the default install phones home, that promise is dead
and the trust cannot be rebuilt. This is a one-time, irreversible decision.

## Decision
- The default install makes **no outbound network calls** on read/search and
  contains **no analytics**. The privacy claim is enforced by the *absence of
  code*, not by a policy that could change.
- Reads never call an LLM (a CI invariant in P1+). Any external call is the result
  of a capability the user explicitly turned on (a remote DB adapter, or the
  optional learning loop with their own key).
- Any future metric is **opt-in, locally visible, and off by default**.

## Consequences
- We fly partly blind on adoption analytics — accepted, deliberately.
- The claim is testable: a network-egress test in CI asserts zero outbound calls
  on the default path.
