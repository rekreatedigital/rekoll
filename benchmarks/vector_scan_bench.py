"""Measure SQLiteAdapter.vector_query before/after ADR-0030.

Three arms, same process, same data, same machine:

  legacy  the pre-ADR-0030 brute-force scan, transcribed verbatim below
          (SELECT *, json.loads every row, pure-Python cosine, sort, slice)
  python  the current adapter, vector_backend="python"  (cached scan, no numpy)
  numpy   the current adapter, vector_backend="numpy"   (cached scan, vectorized)

Reports steady-state p50/p95 (the read path a served query actually walks) and,
separately, the COLD cost of the first query after a write — the one case the
cache makes slower, and the one an honest benchmark must not hide.

Usage:  python benchmarks/vector_scan_bench.py [--n 1000 10000] [--dim 384]
"""

from __future__ import annotations

import argparse
import json
import platform
import random
import sys
import time

from rekoll.adapters.sqlite import _KIND_TABLE, SQLiteAdapter
from rekoll.embedding import cosine
from rekoll.model import Kind, MemoryRecord, Provenance, Scope, TrustTier

SCOPE = Scope(tenant="bench", project="vector", agent="scan")
KINDS = [Kind.RAW_FACT, Kind.OBSERVATION, Kind.DIRECTIVE, Kind.EPISODE]


def _legacy_vector_query(adapter, *, scope, embedding, k=10, kind=None, where=None):
    query_vec = [float(x) for x in embedding]
    qdim = len(query_vec)
    skey = scope.key()
    tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
    status_filter = where.get("status") if where else None
    min_trust = where.get("min_trust") if where else None
    scored = []
    for table in tables:
        sql = f"SELECT * FROM {table} WHERE scope_key=? AND embedding IS NOT NULL"
        params = [skey]
        if status_filter is not None:
            sql += " AND status=?"
            params.append(status_filter)
        if min_trust is not None:
            sql += " AND trust_tier>=?"
            params.append(int(min_trust))
        for row in adapter._conn.execute(sql, params).fetchall():
            stored = json.loads(row["embedding"])
            if len(stored) != qdim:
                continue
            scored.append((cosine(query_vec, stored), row))
    scored.sort(key=lambda item: item[0], reverse=True)
    return [(s, adapter._row_to_record(r)) for s, r in scored[: max(0, k)]]


def _corpus(n: int, dim: int, rng: random.Random) -> list[MemoryRecord]:
    out = []
    for i in range(n):
        v = [rng.gauss(0, 1) for _ in range(dim)]
        norm = sum(x * x for x in v) ** 0.5
        out.append(
            MemoryRecord.create(
                scope=SCOPE,
                kind=KINDS[i % 4],
                content=f"benchmark record {i} concerning topic {i % 97}",
                provenance=Provenance(
                    source_uri=f"bench://record/{i}", adapter_name="bench", adapter_version="1"
                ),
                trust_tier=TrustTier(1 + i % 4),
                embedding=tuple(x / norm for x in v),
                embedder_name="bench",
                embedder_dim=dim,
            )
        )
    return out


def _percentiles(samples: list[float]) -> tuple[float, float]:
    s = sorted(samples)
    return s[len(s) // 2], s[min(len(s) - 1, int(len(s) * 0.95))]


def _time(fn, queries, warmup: int, reps: int) -> tuple[float, float]:
    for i in range(warmup):
        fn(queries[i % len(queries)])
    samples = []
    for i in range(reps):
        q = queries[i % len(queries)]
        t = time.perf_counter()
        fn(q)
        samples.append((time.perf_counter() - t) * 1000.0)
    return _percentiles(samples)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, nargs="+", default=[1000])
    ap.add_argument("--dim", type=int, default=384)
    ap.add_argument("--k", type=int, default=8)
    ap.add_argument("--reps", type=int, default=60)
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1729)
    args = ap.parse_args()

    try:
        import numpy

        numpy_version = numpy.__version__
    except ImportError:
        numpy_version = None

    print("# vector_scan_bench (ADR-0030)")
    print(f"machine   : {platform.processor() or platform.machine()}")
    print(f"platform  : {platform.platform()}")
    print(f"python    : {platform.python_version()}  numpy: {numpy_version or 'absent'}")
    print(f"params    : dim={args.dim} k={args.k} warmup={args.warmup} reps={args.reps} seed={args.seed}")
    print()
    header = f"{'N':>7} {'arm':<8} {'p50 ms':>9} {'p95 ms':>9} {'cold ms':>9} {'speedup':>8}"
    print(header)
    print("-" * len(header))

    for n in args.n:
        rng = random.Random(args.seed)
        records = _corpus(n, args.dim, rng)
        queries = [list(records[rng.randrange(n)].embedding) for _ in range(16)]

        baseline_p50 = None
        for arm in ("legacy", "python", "numpy"):
            if arm == "numpy" and numpy_version is None:
                continue
            adapter = SQLiteAdapter(":memory:", vector_backend="python" if arm == "legacy" else arm)
            adapter.add(records=records)
            if arm == "legacy":
                fn = lambda q: _legacy_vector_query(adapter, scope=SCOPE, embedding=q, k=args.k)
            else:
                fn = lambda q: adapter.vector_query(scope=SCOPE, embedding=q, k=args.k)

            p50, p95 = _time(fn, queries, args.warmup, args.reps)

            # cold = first query after a write invalidates the scan cache
            adapter.add(records=_corpus(n + 1, args.dim, random.Random(7))[n:])
            t = time.perf_counter()
            fn(queries[0])
            cold = (time.perf_counter() - t) * 1000.0

            if arm == "legacy":
                baseline_p50 = p50
            speed = f"{baseline_p50 / p50:.1f}x" if baseline_p50 else "-"
            print(f"{n:>7} {arm:<8} {p50:>9.2f} {p95:>9.2f} {cold:>9.2f} {speed:>8}")
            adapter.close()
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
