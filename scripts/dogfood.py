#!/usr/bin/env python
"""Dogfood: Rekoll uses Rekoll on its own repository.

Indexes this repo's code + docs into a local Rekoll store at ``.rekoll/rekoll.db``
(gitignored — a *rebuildable* index, not source of truth) so an agent or a human
can recall project context. Uses structure-aware chunking and hybrid (vector +
keyword) search. Prefers the real local embedder (fastembed) when the
``embeddings`` extra is installed, and falls back to the stub otherwise.

    python scripts/dogfood.py ingest
    python scripts/dogfood.py recall "how does scope isolation work?"
    python scripts/dogfood.py status
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from rekoll import Kind, MemoryRecord, Provenance, Scope, StubEmbedder, TrustTier
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.chunking import chunk_file
from rekoll.retrieval import hybrid_search

REPO = Path(__file__).resolve().parents[1]
DB = REPO / ".rekoll" / "rekoll.db"
SCOPE = Scope(tenant="rekoll", project="rekoll", agent="dev")

INCLUDE_EXT = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini"}
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".rekoll", "node_modules",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


def _embedder():
    """Prefer the real local model; fall back to the stub if the extra is absent."""
    try:
        from rekoll.embedding import FastEmbedEmbedder

        embedder = FastEmbedEmbedder()
        _ = embedder.dim  # force the model to load now so failures fall back cleanly
        return embedder
    except Exception as exc:  # ImportError, or model-download/runtime failure
        print(
            f"(using StubEmbedder — fastembed unavailable: {type(exc).__name__}; "
            f'`pip install -e ".[embeddings]"` for semantic recall)'
        )
        return StubEmbedder()


def _reranker():
    """Optional cross-encoder reranker for precision; None if the extra is absent."""
    try:
        from rekoll.reranking import CrossEncoderReranker

        return CrossEncoderReranker()
    except Exception:
        return None


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in INCLUDE_EXT:
                yield path


def ingest() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    # Full rebuild: the store is a disposable index. Removing it first avoids
    # stale chunks lingering when chunking or the embedder changes. (A real
    # incremental sync — re-embed only changed files, prune deleted — is a later
    # feature; for a small dogfood corpus a clean rebuild is simplest and correct.)
    for suffix in ("", "-wal", "-shm"):
        stale = Path(str(DB) + suffix)
        if stale.exists():
            stale.unlink()
    embedder = _embedder()
    db = SQLiteAdapter(str(DB))
    db.set_embedder_identity(scope=SCOPE, identity=embedder.identity())
    files = 0
    chunks = 0
    batch: list[MemoryRecord] = []
    for path in _iter_files(REPO):
        rel = path.relative_to(REPO).as_posix()
        try:
            text = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        pieces = chunk_file(rel, text)
        if not pieces:
            continue
        files += 1
        vectors = embedder.embed(pieces)
        for idx, (piece, vector) in enumerate(zip(pieces, vectors)):
            record = MemoryRecord.create(
                scope=SCOPE,
                kind=Kind.RAW_FACT,
                content=piece,
                provenance=Provenance(
                    source_uri=f"file://{rel}",
                    adapter_name="dogfood",
                    source_file=rel,
                    chunk_index=idx,
                ),
                trust_tier=TrustTier.OWNER,
                metadata={"path": rel, "ext": path.suffix.lower()},
            )
            record.with_embedding(vector, name=embedder.identity().name, dim=embedder.dim)
            batch.append(record)
            chunks += 1
        if len(batch) >= 200:
            db.upsert(records=batch)
            batch = []
    if batch:
        db.upsert(records=batch)
    total = db.count(scope=SCOPE)
    db.close()
    print(f"embedder: {embedder.identity().name}")
    print(f"ingested {files} files -> {chunks} chunks; store holds {total} memories "
          f"at {DB.relative_to(REPO).as_posix()}")


def recall(query: str, k: int = 5) -> None:
    if not DB.exists():
        print("no store yet — run: python scripts/dogfood.py ingest")
        return
    embedder = _embedder()
    reranker = _reranker()
    db = SQLiteAdapter(str(DB))
    stored = db.get_embedder_identity(scope=SCOPE)
    if stored is not None and stored != embedder.identity():
        print(f"(note: store built with {stored.name}, current embedder is "
              f"{embedder.identity().name} — re-run `ingest` for best vector recall; "
              f"keyword recall still works)")
    try:
        hits = hybrid_search(db, scope=SCOPE, query=query, embedder=embedder, k=k, reranker=reranker)
        if reranker is not None:
            print(f"(reranked with {reranker.model_name})")
    except Exception as exc:
        print(f"(reranker failed: {type(exc).__name__}; using RRF order)")
        hits = hybrid_search(db, scope=SCOPE, query=query, embedder=embedder, k=k)
    if not hits.hits:
        print("(no results)")
    for i, hit in enumerate(hits.hits, 1):
        record = hit.record
        loc = record.metadata.get("path", record.provenance.source_file)
        snippet = " ".join(record.content.split())[:160]
        print(f"[{i}] {loc}  (chunk {record.provenance.chunk_index}, score {hit.score:.4f})")
        print(f"     {snippet}")
    db.close()


def status() -> None:
    if not DB.exists():
        print("no store yet — run: python scripts/dogfood.py ingest")
        return
    db = SQLiteAdapter(str(DB))
    identity = db.get_embedder_identity(scope=SCOPE)
    print(f"store:    {DB.relative_to(REPO).as_posix()}")
    print(f"memories: {db.count(scope=SCOPE)}  (scope {SCOPE.key()})")
    print(f"embedder: {identity.name if identity else 'unknown'}")
    db.close()


def main() -> None:
    try:  # the indexed corpus contains box-drawing/emoji; force UTF-8 stdout
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Rekoll dogfooding itself on this repo.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="(re)index this repo into the Rekoll store")
    rec = sub.add_parser("recall", help="hybrid search over the store")
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
