# ADR-0003 — Scope is a tenant/project/agent triple on every row and query

**Status:** Accepted · **Date:** 2026-06-23

## Context
The same Rekoll instance may serve multiple tenants, projects, and agents. Memory
must never leak across these boundaries — a cross-tenant bleed is a critical bug
for a privacy-first product.

## Decision
- `Scope(tenant, project, agent)` (coarse → fine), defaulting to
  `default/default/default` for the single-user case.
- `Scope.key()` (`"tenant/project/agent"`) is stamped on **every row** and is a
  mandatory filter on **every read and write** in the adapter contract.
- Cross-scope reads are forbidden: `get`/`count`/`vector_query` return only
  same-scope rows; the conformance suite asserts isolation.

## Consequences
- Single-user usage needs zero scope ceremony (sensible defaults).
- Multi-tenant deployments get isolation for free; a future TenantExtension maps
  an auth principal → scope (P8), with the isolation already guaranteed here.
- Open question deferred: whether physical schema-per-tenant is offered in
  addition to row-level scoping (revisit when the Postgres adapter lands).
