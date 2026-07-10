# ADR-0031 — Abstain-gate door parity: `min_score` and the abstain envelope across MCP + CLI

**Status:** Accepted · **Date:** 2026-07-11 · **Extends:** ADR-0028 (abstain on weak recall), ADR-0024 (honest degradation)

## Context

ADR-0028 added the opt-in abstain gate: `Memory.recall(query, min_score=...)`
returns zero hits with `abstained=True` and an honest `mode` when no memory is
close enough, instead of `k` confident-looking hits for a question the store
cannot answer. It shipped in PR #44 **on the SDK only** — `mcp_server.py` and
`cli.py` were out of that lane's write scope.

That left the honest-degradation contract stronger on one door than the other
two: an agent reaching Rekoll over MCP, or a script using `rekoll recall`, could
not abstain and could not see `abstained` in the recall envelope. `mode` already
crosses every door (ADR-0024 / issue #25); the abstain half did not. This is
exactly the asymmetry `tests/test_three_doors_parity.py` exists to prevent
(issue #47). This ADR records the door-parity decisions; the retrieval design is
settled in ADR-0028 and is not re-litigated here.

## Decision

The abstain gate is reachable, and readable, through all three doors identically.

1. **`min_score` threads through every door.** MCP `recall` gains an optional
   `min_score`; CLI `recall` gains `--min-score`. Both validate it exactly as the
   SDK does — a **cosine in [-1.0, 1.0]**, not a fused/RRF score — and reject an
   out-of-range value at the boundary (a clean door/parse error, never a
   traceback). It is a cosine because that is what the gate compares against,
   pre-fusion (ADR-0028's central trap).

2. **The recall payload carries the abstain envelope, always.** Both machine
   doors (MCP result, CLI `--json`) return `abstained` (bool) and
   `top_vector_score` (the top-1 vector cosine the gate compared against, or
   `null` when no cosine leg produced a candidate) alongside the existing
   `context` / `ids` / `mode` / `count`. The keys are present on *every* recall
   (`abstained: false` on an ordinary one), so the payload shape is constant and
   `test_cli_json_and_mcp_recall_hand_back_one_payload_shape` still pins one
   shape across both doors — now a six-key shape.

   These are **scores, not filesystem names**, so the counts-not-names door rule
   (L-mcp-rootleak, which governs `secrets_*` / `filtered`) does not bear on
   them: `top_vector_score` is a number an agent needs in order to calibrate a
   threshold, and leaks nothing about the operator's environment.

3. **Exit codes: an abstain keeps the existing exit-1 no-results convention.**
   An abstain is zero hits, so `rekoll recall` exits 1 exactly as it does for a
   genuine miss (`recall --json || handle` keeps working). What distinguishes the
   two is the payload/message, not the code: `--json` carries `abstained: true`
   and the gated `mode`; the human line reads `Abstained: … (this is not an empty
   store; …)` instead of `No memories found`. Overloading the exit code to carry
   "abstained vs empty" would break the grep convention for no benefit the
   payload doesn't already provide.

4. **The parity suite pins abstain across all three doors.** A new test asserts
   that a gated recall reads as `abstained` — zero hits, `abstained: true`, a
   `mode` that names the gate, and the same `top_vector_score` — through SDK,
   CLI, and the real MCP stdio server, and that the same query **without** the
   gate is a normal non-empty recall. Abstain is never confusable with an empty
   store, on any door.

## Consequences

- **One contract, three doors.** The abstain half of honest degradation now has
  the same reach as `mode`. The asymmetry issue #47 flagged is closed.
- **Payload shape grew by two keys** on both machine doors, documented in
  `docs/MCP.md` (which `tests/test_docs_consistency.py` pins key-by-key) and in
  the CLI `--json` help. `README.md` advertising of `min_score` remains a
  conductor decision and is out of this lane's scope.
- **No behavior change when `min_score` is omitted** — the default recall is
  byte-for-byte what it was, plus two always-present envelope keys.

## Alternatives rejected

- **Sniff the SDK warning at the door.** Same anti-pattern ADR-0028 rejected for
  the count: couples the door to another module's prose. The flag belongs in the
  return value.
- **A dedicated abstain exit code (e.g. 2) on the CLI.** Breaks the documented
  `recall || handle` grep convention; the `--json` payload and the human message
  already say *why* the result is empty.
- **Omit `top_vector_score` from the wire** (ship only `abstained`). Then an
  agent can see *that* it abstained but has no measured quantity to pick a
  threshold from — the SDK's documented calibration recipe would stop at the
  door, re-creating a smaller version of the same asymmetry.
