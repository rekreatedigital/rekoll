#!/usr/bin/env python
"""Dogfood: Rekoll uses Rekoll on its own repository — now via the `Memory` facade.

The whole script is a thin wrapper over `rekoll.Memory`; the engine does the work
(local embeddings + reranker if installed, firewall on, hybrid+reranked recall).
The store at `.rekoll/rekoll.db` is a gitignored, rebuildable index.

    python scripts/dogfood.py ingest
    python scripts/dogfood.py recall "how does scope isolation work?"
    python scripts/dogfood.py status
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from rekoll import Memory, TrustTier

REPO = Path(__file__).resolve().parents[1]
DB = REPO / ".rekoll" / "rekoll.db"
INCLUDE_EXT = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini"}


def _memory() -> Memory:
    return Memory(path=str(DB), tenant="rekoll", project="rekoll", agent="dev")


def ingest() -> None:
    for suffix in ("", "-wal", "-shm"):  # full rebuild — the store is a disposable index
        stale = Path(str(DB) + suffix)
        if stale.exists():
            stale.unlink()
    mem = _memory()
    # Our own repo: vouch for it explicitly. At the safe UNVERIFIED ingest
    # default (ADR-0015) the firewall docs/tests — which quote injection
    # phrases — would be quarantined and unrecallable.
    stats = mem.ingest_path(str(REPO), include_ext=INCLUDE_EXT, trust=TrustTier.CURATED)
    print(f"embedder: {mem.embedder.identity().name}")
    print(f"ingested {stats['files']} files -> {stats['chunks']} chunks; "
          f"store holds {stats['total']} memories at {DB.relative_to(REPO).as_posix()}")
    mem.close()


def recall(query: str, k: int = 5) -> None:
    if not DB.exists():
        print("no store yet — run: python scripts/dogfood.py ingest")
        return
    mem = _memory()
    result = mem.recall(query, k=k)
    if mem.reranker is not None:
        print(f"(reranked with {mem.reranker.model_name})")
    if not result.hits:
        print("(no results)")
    for i, hit in enumerate(result.hits, 1):
        record = hit.record
        loc = record.metadata.get("path", record.provenance.source_file)
        snippet = " ".join(record.content.split())[:160]
        print(f"[{i}] {loc}  (chunk {record.provenance.chunk_index}, score {hit.score:.4f})")
        print(f"     {snippet}")
    mem.close()


def status() -> None:
    if not DB.exists():
        print("no store yet — run: python scripts/dogfood.py ingest")
        return
    mem = _memory()
    print(f"store:    {DB.relative_to(REPO).as_posix()}")
    print(f"memories: {mem.count()}  (scope {mem.scope.key()})")
    print(f"embedder: {mem.embedder.identity().name}")
    mem.close()


def main() -> None:
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Rekoll dogfooding itself (via the Memory facade).")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="(re)index this repo into the Rekoll store")
    rec = sub.add_parser("recall", help="hybrid + reranked search over the store")
    rec.add_argument("query")
    rec.add_argument("-k", type=int, default=5)
    sub.add_parser("status", help="show store stats")
    args = parser.parse_args()
    if args.cmd == "ingest":
        ingest()
    elif args.cmd == "recall":
        recall(args.query, k=args.k)
    elif args.cmd == "status":
        status()


if __name__ == "__main__":
    main()
