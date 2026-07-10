# ADR-0030 — the SQLite vector scan is cached and vectorized, never approximate

**Status:** Accepted · **Date:** 2026-07-10 · **Amends:** [ADR-0005](0005-storage-adapter-contract.md), [ADR-0009](0009-local-embeddings-and-hybrid-retrieval.md)

## Context

At a 1,000-record corpus a no-rerank hybrid read spent ~152–203 ms p50 in
retrieve+fuse — ~97% of end-to-end latency (issue #35, measured by the efficacy
program's Lane 1c/4 ablation). The query embedding was ~4 ms. Everything else
was `SQLiteAdapter.vector_query`.

Profiling that query at N=1000, dim=384 located the cost precisely, and it was
**not** where the issue assumed:

| cost | ms/query | share |
| --- | ---: | ---: |
| `json.loads` of each stored embedding | 85.7 | 67% |
| pure-Python `cosine()` | 29.3 | 23% |
| `SELECT *` + `fetchall` | 6.0 | 5% |
| everything else | 6.3 | 5% |

The scan re-decoded **every** stored vector from JSON on **every** query, then
`SELECT *`-ed every candidate row only to discard all but `k`. Both costs are
pure waste, and both scale linearly: at 10k the same query took ~1.2 s.

The obvious answer — an ANN index (sqlite-vec / vss) — buys sublinear reads at
the price of *approximate* recall and a new dependency. Before paying either, it
is worth noticing that an exact scan doing no redundant work is ~100× faster
than the one we had, which moves the ceiling far enough that approximation is
not yet needed.

## Decision

**Make the exact scan cheap. Do not make it approximate.** Top-k is unchanged —
same ids, same order, same scores — at every store size.

1. **Cache the decoded vectors.** `_ScanCache` holds `id → (status, trust,
   vector, norm, dim)` per `(kind_table, scope_key)`. `json.loads` and the norm
   are paid once per row per write, not once per row per query.

2. **The cache's order *is* rowid order, maintained rather than re-derived.**
   `rows` is an insertion-ordered dict, and the three mutations SQLite performs
   map exactly onto the three dict mutations: `INSERT` appends a new rowid at
   the end; `INSERT OR REPLACE` deletes then re-inserts, so the rowid *moves to
   the end*; `DELETE` removes. That correspondence is what lets a write update
   the cache **in place** instead of dropping it — so `remember()` then
   `recall()` never pays a rebuild. It is asserted directly by a randomized
   add/upsert/delete property test that diffs cached order against `ORDER BY
   rowid`.

   Exact score ties break on that order, so this is a correctness property, not
   just a performance one. (A tie's order visibly changes after an upsert,
   because the row really does move. It did so before this ADR too.)

3. **Vectorize with numpy only if numpy is already resident.** `vector_backend`
   is `"auto"` (default), `"numpy"`, or `"python"`. `auto` reads
   `sys.modules.get("numpy")` — it *never imports* it. Under the `[embeddings]`
   extra, fastembed has already imported numpy by the time any real vector
   exists, so the fast path is free; on the zero-dependency default path numpy
   is absent and the pure-Python fold runs. `import rekoll` + a `StubEmbedder`
   recall still imports nothing (`tests/test_invariants.py`).

4. **The pure-Python path is bit-exact, not merely close.** The dot product is a
   naive left fold (`reduce(add, map(mul, q, v), 0.0)`), matching `cosine()`'s
   `dot += x*y` accumulation order, and the cached norm is accumulated in the
   same order as `cosine()`'s `nb`. Notably it may **not** use the builtin
   `sum()`: since Python 3.12 `sum()` applies Neumaier compensated summation to
   floats, making it *more* accurate than — and therefore unequal to — the score
   this adapter previously returned. The numpy path sums pairwise and may differ
   in the last ulp; tests assert it within `1e-9`.

5. **Bounded memory.** Vectors are stored as `array('d')` (8 B/element) rather
   than lists of boxed floats (32 B/element): 125 MB → 32 MB at 10k×384. The
   cache is capped at `DEFAULT_VECTOR_CACHE_MAX_VECTORS` (50,000 vectors,
   ~150 MB at dim=384) with LRU eviction of whole `(table, scope)` entries. A
   scope larger than the entire budget is never cached: it re-decodes per query,
   i.e. falls back to exactly the pre-0030 cost model. **Correctness never
   depends on the budget** — only latency does.

6. **Foreign writes invalidate.** Our own commits do not move `PRAGMA
   data_version`; another connection's or process's commits do. A live entry is
   valid only while `data_version` is unchanged, so a second process writing the
   same `.db` forces a full rebuild instead of being served stale vectors
   forever.

7. **Only the winners are reconstructed.** `vector_query` ranks on cached
   scalars and then re-reads the `k` winning rows from SQLite, so every column a
   caller sees is live rather than cached — the cache holds *nothing* a record
   exposes except the vector it was ranked by. `lexical_query` was doing the same
   thing wrong in a different way (it built a full `MemoryRecord` — two child
   queries and a `json.loads` — for every FTS match, to keep `k`), and now gates
   on two scalar columns and reconstructs only the `k` survivors.

   `lexical_query`'s gate must reapply `MemoryRecord.__post_init__`'s rule that
   quarantine-level trust forces `status=quarantined`: the legacy tail filtered
   on the *reconstructed* status, so a row whose stored `status` column says
   `active` at trust 0 (written by an older Rekoll, or by hand) has to be gated
   as quarantined. Reading the raw column would surface a quarantined memory
   through a `status='active'` filter. This is pinned by a test that forges that
   exact row.

8. **`CAP_VECTOR_INDEX` is the seam, unadvertised here.** A backend served by a
   real vector index (HNSW/IVF/sqlite-vec/pgvector) advertises it; the flag says
   "reads are sublinear" *and* "recall is approximate". `SQLiteAdapter` does not
   advertise it, because it is an exact full scan and claiming either would be a
   lie. `Memory.health()`'s retrievability probe already widens to a membership
   window so an approximate self-match outside top-1 does not read as dead
   ingestion — the architecture is ready for an ANN backend behind this flag.

## Consequences

Measured on Ryzen 7 7800X3D (8C/16T), 32 GB, Win 11, Python 3.12.6, numpy 2.5.0,
dim=384, k=8, warmup=10, reps=60 — reproduce with
`python benchmarks/vector_scan_bench.py --n 1000 10000`:

| N | arm | p50 ms | p95 ms | cold ms | speedup |
| ---: | --- | ---: | ---: | ---: | ---: |
| 1,000 | legacy (brute force) | 119.25 | 129.95 | 118.70 | 1.0× |
| 1,000 | python (cached) | 16.38 | 20.46 | 16.09 | 7.3× |
| 1,000 | numpy (cached) | **1.29** | 1.40 | 2.94 | **92.7×** |
| 10,000 | legacy (brute force) | 1206.15 | 1242.78 | 1211.93 | 1.0× |
| 10,000 | python (cached) | 156.81 | 177.60 | 163.91 | 7.7× |
| 10,000 | numpy (cached) | **4.51** | 6.61 | 8.88 | **267.7×** |

`cold` is the first query after a write. It is ~equal to `p50` for the
pure-Python path (the surgical cache update means no rebuild) and slightly
higher for numpy (the per-dim matrix is rebuilt from the cached arrays).

End-to-end `Memory.recall()` at 1k with fastembed/bge-small, no reranker:
**171.9 ms → 24.1 ms p50** (7.1×); recall-immediately-after-a-write is 25.9 ms,
so the write/read cycle carries no cache penalty.

**What this does not fix.** Reads are still O(N·dim). At 100k the numpy scan is
~45 ms and the pure-Python one ~1.5 s, and beyond the cache budget both revert
to re-decoding. Sublinear reads need an actual index — that is what
`CAP_VECTOR_INDEX` exists to let a heavier backend advertise, and it is a
follow-on (it needs a new optional dependency, hence a `pyproject.toml` change).

The next latency lever is no longer the scan: at 1k, ~50% of what remains in
`Memory.recall()` is `_row_to_record` hydrating the ~96-record fusion pool,
where each record `json.loads` an embedding that RRF never reads and
`MemoryRecord.__post_init__` re-coerces it element-by-element. Making the
embedding lazy (or letting the adapter hydrate without it) is a `model.py` /
`retrieval.py` change, tracked separately.

The cache trades memory for latency, which the pre-0030 scan did not. The bound
in (5) is what makes that trade safe to ship by default rather than a footgun in
a long-lived MCP server serving many scopes.
