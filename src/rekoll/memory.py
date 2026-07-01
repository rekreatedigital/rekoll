"""The high-level ``Memory`` facade — the drop-in SDK (Door 2).

Ties the whole engine together behind two verbs so a user never wires adapters,
embedders, the firewall, retrieval, and the reranker by hand::

    from rekoll import Memory
    mem = Memory(project="myapp")                 # local, private, firewall on
    mem.remember("we chose Postgres over BigQuery for cost")
    print(mem.recall("why postgres?").context())  # LLM-ready, safe data envelope

Defaults: local SQLite store, real local embeddings + reranker if the
``embeddings`` extra is installed (else the stub), firewall ON, reads call no LLM.
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from .adapters.base import QueryHit, StorageAdapter
from .adapters.registry import get_adapter
from .chunking import chunk_file
from .consolidation import Consolidator
from .embedding import Embedder, StubEmbedder
from .firewall import ContextEnvelope, build_envelope, sanitize_unicode, screened_record
from .model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from .retrieval import hybrid_search

__all__ = [
    "Memory",
    "RecallResult",
    "DEFAULT_INGEST_TRUST",
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
]

# Files and bulk documents are third-party by nature: ingestion defaults to
# UNVERIFIED so the firewall can quarantine injection markers (quarantine only
# fires at trust <= UNVERIFIED). Only first-person ``remember()`` follows the
# constructor's ``default_trust``. Pass ``trust=`` to vouch for a source you
# control (ADR-0015).
DEFAULT_INGEST_TRUST = TrustTier.UNVERIFIED

# Resource limits (ADR-0017). A single un-chunked memory: past ~100k chars it is
# a document, not a fact — chunk it via ingest_text/ingest_path instead. A single
# ingested file/document: 10 MiB of TEXT (~2 500 pages) — bigger inputs are
# almost never prose and reading them unbounded is a memory-exhaustion vector.
DEFAULT_MAX_CONTENT_CHARS = 100_000
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024

DEFAULT_INCLUDE_EXT = {
    ".py", ".md", ".markdown", ".txt", ".rst", ".toml", ".yml", ".yaml",
    ".json", ".cfg", ".ini", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
}
DEFAULT_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".rekoll", "node_modules",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}


def _auto_embedder() -> Embedder:
    try:
        from .embedding import FastEmbedEmbedder

        embedder = FastEmbedEmbedder()
        _ = embedder.dim  # force model load now so a failure falls back cleanly
        return embedder
    except Exception:
        return StubEmbedder()


def _auto_reranker():
    try:
        from .reranking import CrossEncoderReranker

        return CrossEncoderReranker()
    except Exception:
        return None


@dataclass(frozen=True)
class RecallResult:
    """What ``Memory.recall`` returns: ranked hits + helpers to use them safely."""

    hits: tuple[QueryHit, ...]

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)

    def texts(self) -> list[str]:
        return [h.record.content for h in self.hits]

    def ids(self) -> list[str]:
        """Record ids in rank order — e.g. ``mem.forget(*mem.recall(q).ids())``."""
        return [h.record.id for h in self.hits]

    def records(self) -> list[MemoryRecord]:
        return [h.record for h in self.hits]

    def envelope(self) -> ContextEnvelope:
        return build_envelope(self.hits)

    def context(self) -> str:
        """LLM-ready string: memories framed as DATA, never as instructions."""
        return self.envelope().render()


class Memory:
    def __init__(
        self,
        path: str = "./.rekoll/memory.db",
        *,
        project: str = "default",
        tenant: str = "default",
        agent: str = "default",
        backend: str = "sqlite",
        embedder: Optional[Union[Embedder, str]] = None,
        reranker: object = "auto",
        screen: bool = True,
        default_trust: TrustTier = TrustTier.OWNER,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
    ) -> None:
        """``default_trust`` applies to first-person ``remember()`` calls ONLY.

        Bulk ingestion (``ingest_text`` / ``ingest_path``) always defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) regardless of this setting, so a
        high default can never silently exempt third-party files from the
        firewall's quarantine (ADR-0015).

        ``max_content_chars`` caps one ``remember()`` record; ``max_file_bytes``
        caps one ingested file/document (ADR-0017). Both overridable, never
        disable-able to zero.
        """
        if max_content_chars <= 0 or max_file_bytes <= 0:
            raise ValueError("max_content_chars and max_file_bytes must be positive")
        self.scope = Scope(tenant=tenant, project=project, agent=agent)
        self._screen = screen
        self._default_trust = default_trust
        self._max_content_chars = max_content_chars
        self._max_file_bytes = max_file_bytes

        if backend == "sqlite" and path and path != ":memory:":
            Path(path).expanduser().parent.mkdir(parents=True, exist_ok=True)
            self.adapter: StorageAdapter = get_adapter(backend, path=str(path))
        elif backend == "sqlite":
            self.adapter = get_adapter(backend, path=":memory:")
        else:
            self.adapter = get_adapter(backend)

        if isinstance(embedder, str):
            # Spec string, e.g. "openai:text-embedding-3-small" — the explicit
            # opt-in that may reach rekoll.providers. The default (None) never does.
            from .embedders import get_embedder

            self.embedder = get_embedder(embedder)
        else:
            self.embedder = embedder or _auto_embedder()
        self.reranker = _auto_reranker() if reranker == "auto" else reranker

        existing = self.adapter.get_embedder_identity(scope=self.scope)
        current = self.embedder.identity()
        if existing is None:
            self.adapter.set_embedder_identity(scope=self.scope, identity=current)
        elif existing != current:
            # Show the FULL identity (name + dim + config) — a dim/config-only swap
            # under the same model name would otherwise print an identical-looking
            # message. Routed through warnings so hosts can filter/capture it.
            warnings.warn(
                f"[rekoll] this scope was embedded with {existing.name!r} "
                f"(dim={existing.dim}, config={existing.config_hash}), but the current embedder "
                f"is {current.name!r} (dim={current.dim}, config={current.config_hash}). "
                f"Vector recall across the mismatch is degraded (incompatible-dim vectors are "
                f"skipped); keyword recall still works. Re-ingest this scope or use a separate one.",
                stacklevel=2,
            )

    # -- write --------------------------------------------------------------
    def remember(
        self,
        content: str,
        *,
        kind: Kind = Kind.RAW_FACT,
        source: str = "user",
        trust: Optional[TrustTier] = None,
        metadata: Optional[dict] = None,
    ) -> MemoryRecord:
        """Store one memory (screened by default). Returns the stored record.

        ``metadata`` values must be flat scalars (str/int/float/bool/None);
        nested or list values are rejected (ADR-0001, no unbounded JSON).

        ``kind=Kind.DIRECTIVE`` requires an explicit ``trust=``: directives at
        or above ``TrustTier.TRUSTED_SOURCE`` render in the recall envelope's
        *instruction* channel, so minting one must be a conscious act of
        vouching, never an inherited default (ADR-0016).
        """
        if kind is Kind.DIRECTIVE and trust is None:
            raise ValueError(
                "kind=DIRECTIVE writes to the instruction channel of the recall "
                "envelope and must carry an explicit trust= (e.g. "
                "trust=TrustTier.OWNER for a rule you authored). Directives "
                "below TrustTier.TRUSTED_SOURCE are stored but render as "
                "evidence, never as instructions (ADR-0016)."
            )
        if len(content) > self._max_content_chars:
            raise ValueError(
                f"content is {len(content):,} chars, over the "
                f"max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a document belongs in ingest_text()/ingest_path() "
                "(which chunk it), or raise max_content_chars (ADR-0017)."
            )
        record = self._make_record(
            content=content,
            kind=kind,
            provenance=Provenance(source_uri=source, adapter_name="memory"),
            trust=self._default_trust if trust is None else trust,
            metadata=metadata,
        )
        self._embed_and_store([record])
        return record

    def ingest_text(
        self,
        text: str,
        *,
        name: str = "doc.txt",
        source: Optional[str] = None,
        kind: Kind = Kind.RAW_FACT,
        trust: Optional[TrustTier] = None,
    ) -> int:
        """Chunk a document and store it. Returns the number of chunks stored.

        Ingested text is third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0015). Pass
        ``trust=`` explicitly to vouch for a source you control.
        """
        if len(text) > self._max_file_bytes:
            raise ValueError(
                f"document is {len(text):,} chars, over the "
                f"max_file_bytes={self._max_file_bytes:,} ingestion limit; "
                "split it or raise max_file_bytes (ADR-0017)."
            )
        src = source or f"text://{name}"
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        records = []
        for i, piece in enumerate(chunk_file(name, text)):
            if self._screen and not sanitize_unicode(piece):
                continue  # nothing survives screening (e.g. only zero-width chars)
            records.append(
                self._make_record(
                    content=piece,
                    kind=kind,
                    provenance=Provenance(
                        source_uri=src, adapter_name="memory", source_file=name, chunk_index=i
                    ),
                    trust=trust,
                    metadata={"path": name},
                )
            )
        self._embed_and_store(records)
        return len(records)

    def ingest_path(
        self,
        path: str,
        *,
        include_ext: Optional[Iterable[str]] = None,
        skip_dirs: Optional[Iterable[str]] = None,
        trust: Optional[TrustTier] = None,
        batch: int = 256,
    ) -> dict:
        """Index a file or directory (code + docs).

        Returns ``{files, chunks, skipped, total}`` — ``skipped`` counts files
        passed over (over ``max_file_bytes``, undecodable, or unreadable).

        Files on disk are third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0015). Pass
        ``trust=`` explicitly to vouch for a tree you control.
        """
        include = set(include_ext) if include_ext else DEFAULT_INCLUDE_EXT
        skip = set(skip_dirs) if skip_dirs else DEFAULT_SKIP_DIRS
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        root = Path(path).expanduser()
        targets = [root] if root.is_file() else list(self._walk(root, include, skip))
        files = 0
        chunks = 0
        skipped = 0
        pending: list[MemoryRecord] = []
        for fp in targets:
            try:
                if fp.stat().st_size > self._max_file_bytes:
                    skipped += 1  # never read an oversized file into memory (ADR-0017)
                    continue
                text = fp.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                skipped += 1
                continue
            rel = fp.name if root.is_file() else fp.relative_to(root).as_posix()
            pieces = chunk_file(rel, text)
            if not pieces:
                continue
            files += 1
            for i, piece in enumerate(pieces):
                if self._screen and not sanitize_unicode(piece):
                    continue  # nothing survives screening (e.g. only zero-width chars)
                pending.append(
                    self._make_record(
                        content=piece,
                        kind=Kind.RAW_FACT,
                        provenance=Provenance(
                            source_uri=f"file://{rel}", adapter_name="memory",
                            source_file=rel, chunk_index=i,
                        ),
                        trust=trust,
                        metadata={"path": rel},
                    )
                )
                chunks += 1
                if len(pending) >= batch:
                    self._embed_and_store(pending)
                    pending = []
        if pending:
            self._embed_and_store(pending)
        return {"files": files, "chunks": chunks, "skipped": skipped, "total": self.count()}

    def forget(self, *ids: str) -> int:
        """Delete memories by id; returns how many were removed."""
        return self.adapter.delete(scope=self.scope, ids=list(ids))

    # -- write-side learning (explicit opt-in; never on the read path) -------
    def consolidate(
        self,
        ids: Optional[Sequence[str]] = None,
        *,
        query: Optional[str] = None,
        k: int = 20,
        consolidator: Consolidator,
        min_source_trust: TrustTier = TrustTier.TRUSTED_SOURCE,
        metadata: Optional[dict] = None,
    ) -> MemoryRecord:
        """Merge existing memories into ONE derived observation via YOUR LLM.

        Explicit opt-in, write-side only: ``recall()`` never calls a
        consolidator (reads stay LLM-free, ADR-0007), and ``Memory`` holds no
        ambient consolidator — you pass one per call. Select sources with
        ``ids=[...]`` or ``query="..."`` (top-``k``). The consolidator's text
        flows through the ingest firewall and is stored with:

         - ``kind=OBSERVATION``,
         - ``provenance.derived_from`` = the source record ids,
         - ``declared_transformations=("llm_summary",)``,
         - trust capped at the MINIMUM trust of the sources — the LLM never
           chooses trust, so low-trust input can't launder itself (ADR-0002).

        Quarantined records are never fed to the model. Sources below
        ``min_source_trust`` are skipped (DESIGN §L3: trusted-tier facts only);
        loosen deliberately with ``min_source_trust=TrustTier.UNVERIFIED``.
        """
        if not callable(getattr(consolidator, "summarize", None)):
            raise TypeError(
                "consolidator must provide .summarize(texts) — e.g. "
                "rekoll.providers.OpenAICompatibleConsolidator('gpt-4o-mini')"
            )
        if (ids is None) == (query is None):
            raise ValueError("pass exactly one of ids=[...] or query='...'")
        if ids is not None:
            wanted = list(dict.fromkeys(ids))
            found = {r.id: r for r in self.adapter.get(scope=self.scope, ids=wanted).records}
            missing = [i for i in wanted if i not in found]
            if missing:
                raise KeyError(f"no such memory in this scope: {missing}")
            records = [found[i] for i in wanted]
        else:
            records = [hit.record for hit in self.recall(query, k=k)]
        floor = max(min_source_trust, TrustTier.UNVERIFIED)  # quarantined NEVER reaches the LLM
        sources = [
            r for r in records
            if r.status is not Status.QUARANTINED and r.trust_tier >= floor
        ]
        if not sources:
            raise ValueError(
                "no consolidation-eligible sources (need status != quarantined and "
                f"trust >= {floor.name.lower()})"
            )
        summary = consolidator.summarize([r.content for r in sources])
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("consolidator returned no text")
        name = str(getattr(consolidator, "name", type(consolidator).__name__))
        record = self._make_record(
            content=summary.strip(),
            kind=Kind.OBSERVATION,
            provenance=Provenance(
                source_uri=f"consolidator://{name}",
                adapter_name="memory",
                derived_from=tuple(r.id for r in sources),
            ),
            trust=min(r.trust_tier for r in sources),
            metadata={**(metadata or {}), "consolidator": name, "source_count": len(sources)},
            declared_transformations=("llm_summary",),
        )
        self._embed_and_store([record])
        return record

    # -- read ---------------------------------------------------------------
    def recall(
        self, query: str, *, k: int = 5, kind: Optional[Kind] = None, rerank: bool = True
    ) -> RecallResult:
        """Hybrid + reranked search. Quarantined memory is excluded; reads call no LLM."""
        result = hybrid_search(
            self.adapter, scope=self.scope, query=query, embedder=self.embedder,
            k=k, kind=kind, reranker=self.reranker if rerank else None,
        )
        return RecallResult(hits=tuple(result.hits))

    def context(self, query: str, *, k: int = 5) -> str:
        """Shortcut: the LLM-ready, firewall-framed context string for a query."""
        return self.recall(query, k=k).context()

    def count(self) -> int:
        return self.adapter.count(scope=self.scope)

    def close(self) -> None:
        self.adapter.close()

    # -- internals ----------------------------------------------------------
    def _make_record(self, *, content, kind, provenance, trust, metadata, **kwargs):
        if self._screen:
            return screened_record(
                scope=self.scope, kind=kind, content=content,
                provenance=provenance, trust_tier=trust, metadata=metadata, **kwargs,
            )
        return MemoryRecord.create(
            scope=self.scope, kind=kind, content=content,
            provenance=provenance, trust_tier=trust, metadata=metadata or {}, **kwargs,
        )

    def _embed_and_store(self, records: list[MemoryRecord]) -> None:
        if not records:
            return
        to_embed = [r for r in records if r.embedding is None]
        if to_embed:
            vectors = self.embedder.embed([r.content for r in to_embed])
            name, dim = self.embedder.identity().name, self.embedder.dim
            for record, vector in zip(to_embed, vectors):
                record.with_embedding(vector, name=name, dim=dim)
        self.adapter.upsert(records=records)

    @staticmethod
    def _walk(root: Path, include: set, skip: set):
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if d not in skip and not d.endswith(".egg-info")]
            for name in filenames:
                fp = Path(dirpath) / name
                if fp.suffix.lower() in include:
                    yield fp
