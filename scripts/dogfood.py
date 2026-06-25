#!/usr/bin/env python
"""Dogfood: Rekoll uses Rekoll on its own repository.

Indexes this repo's code + docs into a local Rekoll store at ``.rekoll/rekoll.db``
(gitignored — it is a *rebuildable* index, not source of truth) so an agent or a
human can recall project context by searching.

Honest status: P0 ships a NON-SEMANTIC stub embedder, so recall today is
word-overlap quality. P1's local embeddings make it semantic — just re-run
``ingest`` (it is idempotent / content-addressed). Eventually the ``rekoll-mcp``
server replaces this script so a coding agent recalls automatically.

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

REPO = Path(__file__).resolve().parents[1]
DB = REPO / ".rekoll" / "rekoll.db"
SCOPE = Scope(tenant="rekoll", project="rekoll", agent="dev")

INCLUDE_EXT = {".py", ".md", ".toml", ".yml", ".yaml", ".txt", ".cfg", ".ini"}
SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".rekoll", "node_modules",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}
CHUNK = 800
OVERLAP = 100


def _iter_files(root: Path):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.endswith(".egg-info")]
        for name in filenames:
            path = Path(dirpath) / name
            if path.suffix.lower() in INCLUDE_EXT:
                yield path


def _chunk(text: str) -> list[str]:
    text = text.strip()
    n = len(text)
    out: list[str] = []
    i = 0
    while i < n:
        end = min(i + CHUNK, n)
        if end < n:
            window = text[i:end]
            br = window.rfind("\n\n")
            if br < CHUNK // 2:
                br = window.rfind("\n")
            if br >= CHUNK // 2:
                end = i + br
        piece = text[i:end].strip()
        if piece:
            out.append(piece)
        if end >= n:
            break
        i = end - OVERLAP if end - OVERLAP > i else end
    return out


def ingest() -> None:
    DB.parent.mkdir(parents=True, exist_ok=True)
    embedder = StubEmbedder()
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
        pieces = _chunk(text)
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
                trust_tier=TrustTier.OWNER,  # our own repo: highest trust
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
    print(f"ingested {files} files -> {chunks} chunks; store holds {total} memories "
          f"at {DB.relative_to(REPO).as_posix()}")


def recall(query: str, k: int = 5) -> None:
    if not DB.exists():
        print("no store yet — run: python scripts/dogfood.py ingest")
        return
    embedder = StubEmbedder()
    db = SQLiteAdapter(str(DB))
    hits = db.vector_query(scope=SCOPE, embedding=embedder.embed([query])[0], k=k)
    if not hits.hits:
        print("(no results)")
    for i, hit in enumerate(hits.hits, 1):
        record = hit.record
        loc = record.metadata.get("path", record.provenance.source_file)
        snippet = " ".join(record.content.split())[:160]
        print(f"[{i}] {loc}  (chunk {record.provenance.chunk_index}, score {hit.score:.3f})")
        print(f"     {snippet}")
    db.close()


def status() -> None:
    if not DB.exists():
        print("no store yet — run: python scripts/dogfood.py ingest")
        return
    db = SQLiteAdapter(str(DB))
    print(f"store:    {DB.relative_to(REPO).as_posix()}")
    print(f"memories: {db.count(scope=SCOPE)}  (scope {SCOPE.key()})")
    db.close()


def main() -> None:
    try:  # the indexed corpus contains box-drawing/emoji; force UTF-8 stdout
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    parser = argparse.ArgumentParser(description="Rekoll dogfooding itself on this repo.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ingest", help="(re)index this repo into the Rekoll store")
    rec = sub.add_parser("recall", help="search the store")
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
