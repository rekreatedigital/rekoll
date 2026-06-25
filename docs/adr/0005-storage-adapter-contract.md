# ADR-0005 — One StorageAdapter ABC: keyword-only, typed, capability-honest

**Status:** Accepted · **Date:** 2026-06-23

## Context
"Storage-agnostic / bring-your-own-database" only works if there is one small,
strict contract every backend implements — and if backends can't lie about what
they support (MemPalace's fused collection let a backend silently no-op an
unsupported op). The contract is the load-bearing seam of the whole product.

## Decision
- A single `StorageAdapter` ABC with **keyword-only** methods and **typed
  dataclass results** (`QueryResult`/`GetResult`), never raw dicts.
- A **required vector + metadata core** (`add`/`upsert`/`get`/`delete`/`count`/
  `vector_query`); `lexical`/`relational` are optional **capabilities** advertised
  in `capabilities`. An unsupported call raises `UnsupportedCapabilityError` — never
  a silent degrade. Unknown query filters raise, never silently drop.
- A per-scope **embedder-identity guard** (name + dim + config_hash, three-state
  compare) prevents mixing vectors from different models in one scope.
- Discovery via the `rekoll.adapters` entry-point group + explicit registration.
- One **importable conformance suite** (`rekoll.conformance.run_all`) is the
  executable definition of correctness, run identically by first- and third-party
  adapters.

## Consequences
- A new backend is "drop a package that passes `run_all`" — no core changes.
- The reference SQLite adapter and the conformance suite ship together in P0.
