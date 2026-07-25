"""Measure what ``Memory.recall`` pays to hydrate the RRF fusion pool (issue #43).

The retrieval pool is fetched from BOTH legs at ``candidates`` deep (default
``6*k``), so a ``k=8`` recall reconstructs ~96 records to return 8. Every
reconstruction used to json-decode and box a 384-float embedding that RRF
fusion never reads — decoded, boxed, ranked away, discarded.

Three things are reported, and all three matter:

  decodes   ``_decode_embedding`` calls per WARM recall, split by caller. This is
            the mechanism, and it is machine-independent: a wall-clock win that
            does not show up here is measuring something else.
  latency   steady-state p50/p95 of ``Memory.recall`` (warm scan cache, ADR-0030).
  ranking   a fingerprint of every query's ``(id, score)`` list. Ranking quality
            is a ratchet in this repo, so a perf change must prove it moved
            NOTHING. Compare fingerprints across two runs; they must be equal.

The stub embedder is used deliberately: it is deterministic and needs no model
download, so the fingerprint is comparable across machines and CI arms. Only the
DIM matters to hydration cost, and ``--dim 384`` matches the issue's profile.

Usage:
    python benchmarks/pool_hydration_bench.py --n 1000 --dim 384 --json before.json
    # ... apply a change ...
    python benchmarks/pool_hydration_bench.py --n 1000 --dim 384 --json after.json
    python benchmarks/pool_hydration_bench.py --compare before.json after.json
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import platform
import random
import statistics
import sys
import tempfile
import time
import traceback
from pathlib import Path

import rekoll.adapters.sqlite as sqlite_mod
from rekoll.embedding import StubEmbedder
from rekoll.memory import Memory

WORDS = (
    "postgres pooling latency index vacuum replica shard cursor migration schema "
    "widget alpha beta gamma delta epsilon retrieval embedding fusion rerank "
    "deploy rollback canary staging cache invalidate quorum consensus ledger"
).split()


def _corpus(n: int, rng: random.Random) -> list[str]:
    """Deterministic pseudo-documents with enough token overlap that the lexical
    leg actually returns a full pool (an empty leg would understate the cost)."""
    out = []
    for i in range(n):
        body = " ".join(rng.choice(WORDS) for _ in range(18))
        out.append(f"note {i}: {body}")
    return out


def _queries(docs: list[str], count: int, rng: random.Random) -> list[str]:
    """Queries drawn from real document text, so both legs hit."""
    return [" ".join(rng.choice(docs).split()[2:7]) for _ in range(count)]


class _DecodeSpy:
    """Count ``_decode_embedding`` calls, attributed to the calling function.

    Patched at module scope because both callers (``_scan`` and the record
    reconstruction path) look the name up in module globals at call time — so
    this counts the real thing and cannot be bypassed by a stale local binding.
    """

    def __init__(self) -> None:
        self.counts: collections.Counter = collections.Counter()
        self._real = sqlite_mod._decode_embedding

    def __enter__(self) -> "_DecodeSpy":
        def spy(raw):
            self.counts[traceback.extract_stack()[-2].name] += 1
            return self._real(raw)

        sqlite_mod._decode_embedding = spy
        return self

    def __exit__(self, *exc) -> None:
        sqlite_mod._decode_embedding = self._real


def run(n: int, dim: int, k: int, nq: int, warmup: int, embedder: str = "stub") -> dict:
    rng = random.Random(20260725)
    docs = _corpus(n, rng)
    queries = _queries(docs, nq, rng)

    tmp = tempfile.mkdtemp(prefix="rekoll-pool-bench-")
    db = str(Path(tmp) / "bench.db")
    if embedder == "fastembed":
        from rekoll.embedding import FastEmbedEmbedder

        emb = FastEmbedEmbedder()  # the issue's env: bge-small-en-v1.5, dim 384
        dim = emb.dim
    else:
        emb = StubEmbedder(dim=dim)
    mem = Memory(path=db, embedder=emb, reranker=None)
    for doc in docs:
        mem.remember(doc)

    # Warm the ADR-0030 scan cache: a cold first query re-decodes the whole
    # scope and would swamp the pool-hydration term this bench is about.
    for q in queries[:warmup] or queries[:1]:
        mem.recall(q, k=k)

    # -- mechanism: decodes per warm recall ---------------------------------
    with _DecodeSpy() as spy:
        mem.recall(queries[0], k=k)
        per_recall = dict(spy.counts)

    # -- ranking fingerprint ------------------------------------------------
    ranking = []
    for q in queries:
        res = mem.recall(q, k=k)
        ranking.append([[h.record.id, repr(h.score)] for h in res.hits])
    fingerprint = hashlib.sha256(
        json.dumps(ranking, sort_keys=True).encode("utf-8")
    ).hexdigest()

    # -- latency ------------------------------------------------------------
    samples = []
    for q in queries:
        t0 = time.perf_counter()
        mem.recall(q, k=k)
        samples.append((time.perf_counter() - t0) * 1000.0)
    mem.close()

    samples.sort()
    return {
        "n": n,
        "dim": dim,
        "embedder": embedder,
        "k": k,
        "queries": nq,
        "decodes_per_warm_recall": per_recall,
        "decodes_total": sum(per_recall.values()),
        "p50_ms": round(statistics.median(samples), 3),
        "p95_ms": round(samples[min(len(samples) - 1, int(len(samples) * 0.95))], 3),
        "mean_ms": round(statistics.fmean(samples), 3),
        "ranking_fingerprint": fingerprint,
        "ranking": ranking,
        "env": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
        },
    }


def _compare(before: Path, after: Path) -> int:
    b = json.loads(before.read_text(encoding="utf-8"))
    a = json.loads(after.read_text(encoding="utf-8"))
    print(f"decodes/warm recall  {b['decodes_total']:>6}  ->  {a['decodes_total']:>6}")
    print(f"  by caller          {b['decodes_per_warm_recall']}  ->  {a['decodes_per_warm_recall']}")
    for key in ("p50_ms", "p95_ms", "mean_ms"):
        ratio = b[key] / a[key] if a[key] else float("inf")
        print(f"{key:<20} {b[key]:>8.3f}  ->  {a[key]:>8.3f}   ({ratio:.2f}x)")
    same = b["ranking"] == a["ranking"]
    print(f"\nranking fingerprint  {b['ranking_fingerprint'][:16]}  vs  {a['ranking_fingerprint'][:16]}")
    print("RANKING BIT-IDENTICAL:", "YES" if same else "NO -- QUALITY MOVED, STOP")
    if not same:
        for i, (bl, al) in enumerate(zip(b["ranking"], a["ranking"])):
            if bl != al:
                print(f"  first divergence at query {i}:\n    before={bl}\n    after ={al}")
                break
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=1000, help="corpus size")
    ap.add_argument("--dim", type=int, default=384, help="embedding width")
    ap.add_argument("--k", type=int, default=8, help="hits requested")
    ap.add_argument("--queries", type=int, default=20, help="timed recalls")
    ap.add_argument("--warmup", type=int, default=10)
    ap.add_argument(
        "--embedder",
        choices=("stub", "fastembed"),
        default="stub",
        help="stub (default, deterministic, no download) or fastembed (the issue's env)",
    )
    ap.add_argument("--json", type=Path, help="write the full result here")
    ap.add_argument("--compare", nargs=2, type=Path, metavar=("BEFORE", "AFTER"))
    args = ap.parse_args(argv)

    if args.compare:
        return _compare(*args.compare)

    result = run(args.n, args.dim, args.k, args.queries, args.warmup, args.embedder)
    printable = {kk: vv for kk, vv in result.items() if kk != "ranking"}
    print(json.dumps(printable, indent=2))
    if args.json:
        args.json.write_text(json.dumps(result, indent=2), encoding="utf-8")
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
