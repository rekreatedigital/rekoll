# ADR-0040 — A silently split store must be loud (the scope-split detector)

**Status:** Accepted · **Date:** 2026-07-25 · **Implements:** issue #83 (detector half) · **Reaffirms:** ADR-0035 §6 (no discovery), ADR-0037 §2 (no repo-committed config) · **Interacts with:** ADR-0023 (effective-status read gate), ADR-0028 (abstain), ADR-0034 (directive floor)

## Context

The CLI and SDK default to `project="default"`; the MCP server derives its
project from the launch folder's name. Both defaults are documented and each
is defensible — but together they mean an AI writing through MCP and a human
typing bare CLI commands **in the same repo** read different scopes of the
same store file. Field report #82 hit it in production; the conductor
reproduced it from scratch (2026-07-24 and again 2026-07-25): one MCP-scope
write, then bare `rekoll recall` → "No memories found", `rekoll status` →
"Memories: 0", and `rekoll doctor` → **"All checks passed."** It also bit
Rekoll's own repo: 366 memories sat under a non-default scope while `status`
said 0.

The disease is not the split — scope isolation is a feature, and both scopes'
data is intact. The disease is the **silence**: every diagnostic actively
reassures while the operator's memories sit invisible in the very file being
read.

An earlier, larger fix was fact-checked and rejected:

- **A scope config file in the repo** (`.rekoll/scope.json` etc.) is banned by
  ADR-0035 §6 verbatim ("Every discovery mechanism is REJECTED for v1 — no
  config file in the repo, no upward directory search, no environment
  auto-detection"). `.gitignore` does not protect against it: gitignore has no
  effect on already-tracked files, so a hostile repo can commit one and every
  cloner receives it — the exact attack the ban exists for. Unifying the
  doors' defaults or adding any discovery needs a superseding ADR plus a
  threat analysis; that design lane is **not** this ADR.
- **Changing either door's default project** would re-scope existing users'
  stored memories into invisibility — the bug, inflicted deliberately — and
  breaks the wizard test suite in ways that would partly go false-green.

## Decision

Detector only. When a **read finds its scope empty** while the same store
holds records under other scopes, say so and hand over the exact command that
shows them:

1. **`rekoll status`** (empty scope only) and **bare `rekoll recall`** (empty
   result, empty scope, not an abstain — an abstain proves the scope is
   non-empty, ADR-0028) print an ASCII note on **stderr**: the other scopes
   with their counts (largest first, capped at five), then a runnable
   `rekoll status --tenant .. --project .. --agent ..` for the largest, with a
   custom `--path` echoed so the hint works verbatim.
2. **`rekoll doctor`** gains a `scopes` check: **WARN** on a split (not FAIL —
   nothing is broken; warn-don't-restrict), `ok` otherwise, honest in both
   directions (`ok` still names how many other scopes hold memories).
3. The census behind it is a new **optional** adapter read,
   `StorageAdapter.scope_counts()` → `{"tenant/project/agent": n}`:
   - It is the ONE bounded exception to "every read carries a Scope": scope
     keys and **counts** cross the boundary, record content never does.
   - It counts **effective-active** rows only (the ADR-0023 gate): a scope
     holding only quarantined or forged-active-at-trust-0 rows is never
     advertised. This is the same predicate `status` prints, so the two
     numbers always agree; it is deliberately NOT a promise that every counted
     row would rank into a given recall (an unembedded or unindexed row still
     counts, exactly as it does in `status`).
   - It is concrete-with-raising-default, never abstract: out-of-tree
     adapters keep instantiating, and every caller fails soft — an adapter
     that cannot answer just means no note, never a failed read.

## What this deliberately does not do

No scope discovery, no config file, no CLI env vars, no default changes, no
auto-switching, no merging, and no new keys on any machine payload
(`recall --json` and the MCP payloads are key-pinned by tests). The note
informs the operator; the operator decides. The default-unification /
operator-local-config question stays open as a future design lane with its
own threat analysis, superseding ADR-0035 §6 only if and when it lands.

## Consequences

- The #83 demo-killer flow now names the split at all three human surfaces
  and hands over the fix command; a brand-new (genuinely empty) store prints
  nothing extra.
- Backends without the census silently keep the old behavior — degraded
  honesty, never an error.
- The census is a per-kind-table `GROUP BY scope_key` over an indexed column:
  cheap, and only run when the scope in front of the operator is empty.
- Tests pin: fires on a split store, quiet on a fresh store, quiet on
  no-match-in-populated-scope, quarantined-only scopes never advertised,
  machine payloads byte-unchanged, silent degradation without the census,
  and the suggested command actually works.
