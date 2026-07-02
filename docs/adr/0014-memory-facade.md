# ADR-0014 — The `Memory` facade is the public drop-in SDK (Door 2)

**Status:** Accepted · **Date:** 2026-06-23 · **Amended by:** ADR-0024 (embedder-mismatch: refuse the vector leg, degrade honestly)

## Context
By P2 the engine had all the parts (storage, embeddings, chunking, firewall,
hybrid retrieval, reranking, envelope) but using them meant wiring ~50 lines by
hand. For "seamless integration into someone else's project," users need one
object with sensible defaults — the equivalent of Mem0's `Memory()` or Hindsight's
wrapper.

## Decision
A single `Memory` class with two core verbs and safe defaults:
- `remember(content, ...)` / `ingest_text(...)` / `ingest_path(...)` — screened by
  the firewall by default, embedded, stored.
- `recall(query) -> RecallResult` — hybrid + reranked, quarantined excluded, no
  LLM; `RecallResult.context()` returns the firewall-framed, LLM-ready string.
- Defaults: local SQLite at `./.rekoll/memory.db`, **auto** local embedder +
  reranker if the `embeddings` extra is present (else the stub), firewall ON,
  single-user scope. Embedder identity is recorded + checked (a model swap warns,
  not corrupts). *(Amended by ADR-0024: a mismatch now refuses the vector leg and
  degrades to lexical-only — the warn is kept but no longer the whole story;
  `Memory.reindex()` is the recovery.)*
- `forget`, `count`, `close`, and a `Scope` (tenant/project/agent) for multi-tenant.

## Consequences
- "Use it in your project" becomes `pip install rekoll` (or `rekoll[embeddings]`)
  → `from rekoll import Memory`. The dogfood script collapses to a few lines.
- This is the surface the MCP server and REST API will wrap (later phases), so the
  three "doors" stay byte-identical (one engine).
- The facade is the stable public contract (SemVer); lower-level pieces remain
  available for power users but the facade is what most people touch.
