# ADR-0034 — The standing-directive channel: saved rules ALWAYS surface on recall

**Status:** Accepted · **Date:** 2026-07-16 · **Extends:** ADR-0013 (injection firewall / data-vs-instructions envelope), ADR-0017 (directive explicit trust), ADR-0007 (zero-LLM read path), ADR-0001 (separate physical tables) · **Interacts with:** ADR-0028 (abstain), ADR-0031 (door parity), ADR-0019 (read-path tamper verification), ADR-0024 (honest degradation)

## Context

The recall envelope (ADR-0013) has two channels: `# Trusted directives (rules to
follow)` and `# Retrieved memory (DATA)`. A saved OWNER directive — an onboarding
preference like *"always explain simply"* — is supposed to ride the directive
channel and shape every session.

It did not. `firewall.build_envelope(hits)` only **partitioned** the hits that
`hybrid_search` had already ranked — it never fetched directives from the store.
So a standing directive appeared **only if it happened to rank into the query's
top-k**. On any unrelated query it silently vanished, and the abstain gate
(ADR-0028), which withholds all hits before fusion, dropped it entirely.

This is the exact "my 'explain simply' preference got ignored" failure, and it is
reproducible. Bury one OWNER directive under 25 unrelated fillers (stub embedder,
`:memory:`) and measure the **applied-consistency rate** — the fraction of
unrelated queries whose envelope contains the rule:

| Recall | main | this ADR |
|---|---|---|
| ordinary (`min_score` off) | **0.125** (1/8, a spurious stub-hash rank-in) | **1.000** (8/8) |
| gated (`min_score=0.99`, abstains) | **0.000** (0/8) | **1.000** (8/8) |

The rule reappeared only when the query shared its words — proof the behavior was
purely rank-driven. A *preference* that fires only when you ask about the
preference is not a standing rule.

The signal was already sitting in a dedicated place. Directives live in their own
physical table (`directives`, ADR-0001), the trust floor for the instruction
channel is fixed (`TRUSTED_SOURCE`, ADR-0017), and reads are zero-LLM by contract
(ADR-0007). Nothing about surfacing a saved rule needs ranking, embeddings, or an
LLM — it needs a plain scoped read.

## Decision

On **every** recall, deterministically fetch the standing directives and ALWAYS
include them in the envelope's instruction channel — independent of the query,
the `kind` filter, and the abstain gate.

1. **The standing-directive channel.** `Memory.recall` fetches the ACTIVE,
   in-scope `Kind.DIRECTIVE` records at/above the directive floor
   (`TRUSTED_SOURCE`), bounded and deterministically ordered, and attaches them to
   `RecallResult.pinned_directives`. `RecallResult.envelope()` passes them to
   `build_envelope(hits, pinned=...)`, which lists them FIRST in the directive
   channel, then any ranked directives not already pinned (deduped by record id).
   `build_envelope(hits)` with no `pinned` is byte-for-byte the old behavior, so
   every existing direct caller is unaffected.

2. **A plain scoped DB read — zero-LLM (ADR-0007).** The fetch is
   `StorageAdapter.active_directives(scope, limit, min_trust)`: a single scoped
   `SELECT` over the dedicated `directives` table in the reference adapter. No
   query embedding, no vector leg, no fusion, no reranker — the read path stays
   free and bounded (the product's cheap-read promise).

3. **Bounded and configurable.** Capped at `max_pinned_directives` (default **5**,
   `Memory.__init__` knob). Unbounded pinning would re-introduce token cost on
   every read, which is exactly what a memory layer that "saves tokens" must not
   do. `0` disables the channel (recall reverts to rank-only). Raise it if you
   keep more than five standing rules.

4. **Deterministic, prefix-stable order: oldest-first** (`created_at` ASC, `id`
   ASC as the tiebreak). Two properties follow, both load-bearing:
   - **Cache-stability.** `ContextEnvelope.render()` must stay a pure,
     byte-identical function of its inputs so a host's prompt-prefix cache is not
     busted on every recall (ADR-0013). Standing directives enter that render, so
     their order must be a deterministic function of the store, never of scores,
     read-time, or SQLite row layout. `created_at`/`id` are stored and stable, so
     identical stores render identically. The existing byte-identity tests stay
     green (no directives in those stores → `pinned=()` → unchanged output).
   - **The cap keeps the FOUNDATIONAL rules.** Oldest-first means the onboarding
     rule survives the cap, and appending a new rule never disturbs the pinned
     *prefix* — the rendered directive block grows at the end, so the cached
     prefix stays warm as rules accrue.

5. **Same gate as the ranked channel — no new trust surface.** The pinned read
   filters `trust_tier >= DIRECTIVE_FLOOR` and effective-status ACTIVE (the same
   `_effective_status` rule every other read leg uses), and `build_envelope`
   re-checks the floor + quarantine on each pinned record as defense in depth.
   QUARANTINED never surfaces; a below-floor directive still renders as evidence,
   never as an instruction (ADR-0017 is untouched). The floor lives in ONE place:
   `firewall.DIRECTIVE_FLOOR`, shared by the ranked partition and the pinned read.

6. **Tamper-verified (ADR-0019).** The pinned read bypasses `hybrid_search`'s
   `_verify_hits`, and a directive is the highest-stakes thing to surface as an
   *instruction*. So `Memory._pinned_directives` content-hash-verifies each record
   and withholds any mismatch (one warning names the ids), exactly as the ranked
   path does for hits.

7. **Abstain-proof.** The channel is fetched independently of the abstain gate, so
   a standing rule appears even when `min_score` forces a zero-hit abstain. This
   **supersedes** the ADR-0028 caveat "an abstained `context()` is an empty
   envelope": an abstained `context()` now contains the standing directives (but
   still no *evidence*). Abstain-vs-empty is still told apart by `abstained` /
   `mode`, exactly as before — the distinction never rode on the envelope.

8. **Cross-door parity (ADR-0031).** A new `directives` key joins the machine
   payload on the SDK (`RecallResult.directives()`), the CLI (`recall --json`),
   and MCP (`recall`) — the same neutralized, deduped list rendered into
   `context`'s directive block, byte-identical across all three doors. The
   three-doors parity suite now pins a seven-key shape and asserts the standing
   directives are non-empty and identical at every door.

9. **A separate channel, not a new result row.** `pinned_directives` rides ONLY
   the directive channel. It deliberately does **not** enter `.ids()` /
   `.records()` / `.texts()` or `len(result)` — those stay the ranked hits, so
   `forget(*recall(q).ids())` can never delete a standing rule, and `count` stays
   the number of ranked results.

### Decisions this ADR owns (called out in the mission)

- **`recall(kind=...)` is independent of the standing channel.** A `kind` filter
  narrows the *ranked* results; the standing directives still surface. Rationale:
  they are rules to follow, not query results — filtering the evidence you asked
  for must not silence the rules you set. (If you filter `kind=DIRECTIVE`, ranked
  directives and pinned directives simply dedup by id.)
- **Where the read lives:** a new optional adapter method `active_directives`
  (base raises `UnsupportedCapabilityError`, like `newest`/`lexical_query`; the
  SQLite adapter reads the `directives` table), NOT a reuse of `newest` (whose
  newest-first order would evict the onboarding rule under the cap) and NOT a
  generic filter primitive (none exists, and directives are their own table).
- **Ordering key:** `created_at` ASC, `id` ASC. **Cap default:** 5, configurable,
  0 disables. (See §4 for why.)
- **Fail-soft.** `max_pinned_directives == 0`, an adapter without
  `active_directives`, or any read error degrades to the pre-ADR-0034 rank-only
  behavior — a standing rule surfacing is a best-effort enhancement that must
  never break (or slow) a recall.

## Consequences

- Agents behind any door are told the standing rules on every recall, for the
  first time — the feature the onboarding wave is built on.
- **No behavior change when there are no directives:** `pinned=()` renders exactly
  the old envelope, so every byte-identity / cache-stability test stays green, and
  the existing three-doors corpus (no directives) is unperturbed.
- **The default recall does one extra bounded scoped read.** It is zero-LLM, hits
  a single indexed table, and is capped at `max_pinned_directives`. `health()` and
  `self_test()` do **not** pay it (they read `.hits` ids, never the envelope, so
  they skip the fetch).
- **Payload shape grew by one key** (`directives`) on both machine doors,
  documented in `docs/MCP.md` (pinned key-by-key by `test_docs_consistency.py`)
  and the CLI `--json` help, and pinned across doors by the parity suite.
- **MCP remains read-only for directives.** This lane touches only the read side;
  `WRITABLE_KINDS` still forbids directive writes over MCP (ADR-0008), so the
  channel only ever surfaces the operator's own trusted-tier rules — nothing a
  calling model could have planted.

## Alternatives rejected

- **Rank the directive higher / boost its score.** Still query-dependent, still
  fails on an unrelated query, and it corrupts the ranking to smuggle in an
  always-on concern. The rule is not a *search result*; it should not compete for
  top-k.
- **Newest-first cap (reuse `newest`).** Evicts the onboarding rule — the exact
  rule the feature exists to keep — the moment a sixth directive is added.
- **Render the directives inside `render()` from a store handle.** Breaks the
  purity/cache-stability contract: `render()` would depend on live DB state and
  read-time, busting prompt caches. The fetch happens once at recall and is
  threaded in as data.
- **Fetch inside `build_envelope` itself.** `build_envelope`/`render` are pure
  functions of their inputs by contract (and are called directly in unit tests
  with no adapter). The `Memory` facade owns the scoped read and the product
  policy (cap, order, verify, degrade); the envelope stays pure.
- **Unbounded pinning.** Re-introduces token cost on every read — the opposite of
  the bounded-read promise. Bounded + configurable is the honest default.
