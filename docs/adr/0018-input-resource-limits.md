# ADR-0018 — Bounded inputs everywhere: content, file, query, FTS expression

**Status:** Accepted · **Date:** 2026-07-02

## Context

The round-2 audit (P0-3) confirmed three unbounded input paths:

1. `ingest_path()` called `fp.read_text()` with no size check — a single huge
   file (or a planted one in a cloned repo) loads fully into memory.
2. `remember()` accepted arbitrarily large content — one 5 MB "fact" was
   accepted in ~1 s of firewall regex time, stored un-chunked, and would be
   embedded whole.
3. `_fts_query()` turned every word of the query into an OR term — a 50k-word
   query produced a ~500 KB FTS5 MATCH expression.

None of these is hypothetical for a memory layer that ingests third-party
trees and receives agent-generated queries.

## Decision

Sane, documented, overridable limits — enforced at the `Memory` boundary so
the underlying pure functions stay composable:

- `Memory(max_content_chars=100_000)` — one `remember()` record. Past ~100k
  chars it is a document, not a fact; the error says to use `ingest_text`.
  **Raises** (caller's own content; loud beats truncated-silently).
- `Memory(max_file_bytes=10 MiB)` — one ingested file (`stat()` checked
  BEFORE reading) or one `ingest_text` document. `ingest_text` **raises**
  (single explicit document); `ingest_path` **skips and counts** (a bulk walk
  must not die on one artifact), reported via a new `skipped` key.
- `_MAX_FTS_TERMS = 32` — lexical query terms are de-duplicated
  (order-preserving) and capped. Natural questions have <20 distinct terms;
  BM25 over more OR-arms adds noise, not recall.
- Recall queries: sanitized and truncated to `MAX_QUERY_CHARS = 8_192` before
  embedding/lexical search (read path must degrade, never DoS — truncation
  over raising). Shipped with the read-path hardening commit (P2-8, this ADR
  governs the value).

Chunk sizes are already bounded by the chunkers (≤ ~2 000 chars), so ingest
chunks need no separate cap. Limits are overridable (power users, tests) but
not disable-able to zero/negative.

## Consequences

- New `skipped` key in the `ingest_path` return dict (additive).
- Behavior change: a >100k-char `remember()` or >10 MiB `ingest_text` now
  raises with a pointed message; oversized files in a tree are skipped and
  counted instead of ingested.
- De-duplicating FTS terms removes the (accidental) double-weighting of
  repeated query words; the recall regression gate stayed green.
