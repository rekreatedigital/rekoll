"""The high-level ``Memory`` facade — the drop-in SDK (Door 2).

Ties the whole engine together behind two verbs so a user never wires adapters,
embedders, the firewall, retrieval, and the reranker by hand::

    from rekoll import Memory
    mem = Memory(project="myapp")                 # local, private, firewall on
    mem.remember("we chose Postgres over BigQuery for cost")
    print(mem.recall("why postgres?").context())  # LLM-ready, safe data envelope

Defaults: local SQLite store, real local embeddings + reranker if the
``embeddings`` extra is installed (else the stub), firewall ON, reads call no LLM.

Beyond the two verbs, the facade carries the memory-quality loop:
``mark_used``/``informed_by`` (the was-it-used usage signal), ``health()``
(source-vs-index freshness), ``self_test()`` (golden probe), and honest
degradation via ``RecallResult.mode`` (ADR-0024).
"""

from __future__ import annotations

import os
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional, Sequence, Union

from .adapters.base import CAP_LEXICAL, QueryHit, StorageAdapter, UnsupportedCapabilityError
from .adapters.registry import get_adapter
from .chunking import chunk_file
from .consolidation import Consolidator
from .embedding import Embedder, StubEmbedder, compare_identity
from .firewall import ContextEnvelope, build_envelope, sanitize_unicode, screened_record
from .ledger import LedgerEntry, RecallLedger
from .model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from .retrieval import hybrid_search

__all__ = [
    "Memory",
    "RecallResult",
    "HealthReport",
    "DEFAULT_INGEST_TRUST",
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
    "DEFAULT_MAX_CHUNKS_PER_DOC",
]

# Files and bulk documents are third-party by nature: ingestion defaults to
# UNVERIFIED so the firewall can quarantine injection markers (quarantine only
# fires at trust <= UNVERIFIED). Only first-person ``remember()`` follows the
# constructor's ``default_trust``. Pass ``trust=`` to vouch for a source you
# control (ADR-0016).
DEFAULT_INGEST_TRUST = TrustTier.UNVERIFIED

# Resource limits (ADR-0018). A single un-chunked memory: past ~100k chars it is
# a document, not a fact — chunk it via ingest_text/ingest_path instead. A single
# ingested file/document: 10 MiB of TEXT (~2 500 pages) — bigger inputs are
# almost never prose and reading them unbounded is a memory-exhaustion vector.
# The byte cap bounds BYTES, not WORK: a heading-per-line markdown document
# chunks at ~0.25 chunks/byte (~2.6M chunks at the byte cap), so one document's
# CHUNK COUNT is capped too. 25k clears the largest legitimate yield (a 10 MiB
# plain-text file at the default stride is ~15k chunks) with headroom; past it
# the ingest is REJECTED (ingest_text raises; ingest_path skips + counts the
# file) — never silently truncated.
DEFAULT_MAX_CONTENT_CHARS = 100_000
DEFAULT_MAX_FILE_BYTES = 10 * 1024 * 1024
DEFAULT_MAX_CHUNKS_PER_DOC = 25_000

DEFAULT_INCLUDE_EXT = {
    ".py", ".md", ".markdown", ".txt", ".rst", ".toml", ".yml", ".yaml",
    ".json", ".cfg", ".ini", ".js", ".ts", ".tsx", ".jsx", ".go", ".rs", ".java",
}
DEFAULT_SKIP_DIRS = {
    ".git", ".venv", "venv", "__pycache__", ".rekoll", "node_modules",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
}

# health() retrievability probe. FULL content (capped) — a short head slice made
# the probe blind on near-duplicate corpora (e.g. a repo ingest where chunks
# share a license-header prefix): the discriminating tail tokens never entered
# the query and healthy stores read stale. 2048 covers every chunker output
# (MD_MAX=1500 / CODE_MAX=2000) whole. The membership window is widened to
# ≥20 so near-ties — and, later, approximate ANN backends whose self-match
# isn't guaranteed top-5 — don't read as dead ingestion.
_PROBE_MAX_CHARS = 2048
_PROBE_MIN_POOL = 20


# -- filesystem containment (a walk must never read outside its root) ----------
# A planted link inside an ingested tree can point anywhere on disk. The load-
# bearing guard is REAL-PATH CONTAINMENT: resolve every directory we would
# descend and every file we would read, and refuse anything whose real location
# is not inside the resolved root. ``os.path.realpath`` resolves symlinks AND
# NTFS junctions (a directory reparse point; ``is_symlink()`` returns False for
# one, so a walk that only trusted it would descend a junction and leak), so
# containment catches symlinks, junctions, and any redirecting reparse point on
# every OS — not a hard-coded list.

def _real_path(path) -> Path:
    """The fully link-resolved real path (``os.path.realpath`` resolves symlinks
    AND Windows junctions/reparse points; on a missing path it just absolutizes)."""
    return Path(os.path.realpath(os.fspath(path)))


def _within(root_real: Path, path) -> bool:
    """True iff ``path`` resolves (link-followed) to inside ``root_real`` (which
    must already be a real path). Fail-safe: an unresolvable path reads as
    outside, so the caller skips/refuses it rather than reading it."""
    try:
        real = _real_path(path)
    except OSError:  # pragma: no cover - resolution failure is treated as escape
        return False
    return real == root_real or real.is_relative_to(root_real)


def _redirects_out(path) -> bool:
    """True iff ``path`` is a link that RESOLVES ELSEWHERE than where it sits — a
    symlink, an NTFS junction, or a mount point. Used only to decide whether a
    directly-pointed target should warn+skip.

    Deliberately NOT "is this a reparse point": a non-redirecting reparse point
    (a OneDrive Files-On-Demand placeholder, a Windows Dedup stub) has its real
    path equal to its own location and is a legitimate in-tree file to read —
    flagging those on the reparse attribute alone would silently drop real source
    files. We instead ask whether resolving the leaf lands somewhere other than
    ``<resolved parent>/<name>``. Comparing against the resolved PARENT (not the
    literal abspath) means a symlinked ANCESTOR — e.g. macOS ``/tmp`` ->
    ``/private/tmp`` — does not false-positive the leaf."""
    p = Path(os.path.abspath(os.fspath(path)))
    try:
        if p.is_symlink():  # POSIX symlinks (and the monkeypatched skip tests)
            return True
        here = os.path.join(os.path.realpath(p.parent), p.name)
        return os.path.normcase(os.path.realpath(p)) != os.path.normcase(here)
    except OSError:  # pragma: no cover - unresolvable: treat as a redirect (skip)
        return True


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
    """What ``Memory.recall`` returns: ranked hits + helpers to use them safely.

    ``mode`` names exactly what ran to produce these hits — the honest-
    degradation contract ("don't bluff a broken index"): a caller or agent can
    always tell a full hybrid ranking (``"vector+lexical+rerank"``) from a
    degraded one (``"lexical-only: embedder mismatch"``) or a semantics-free
    one (``"vector+lexical (stub-embedder)"``), instead of treating every
    result list as equally trustworthy. ``mode`` is deliberately NOT rendered
    into :meth:`context` — the envelope stays a pure function of the hits so
    agent prompt caches aren't busted (see ``ContextEnvelope.render``).
    """

    hits: tuple[QueryHit, ...]
    mode: str = "unspecified"

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


@dataclass(frozen=True)
class HealthReport:
    """What ``Memory.health`` returns: source-of-truth-vs-index freshness.

    ``ok`` is True when the newest checked records are both embedded and
    retrievable AND the embedder identity matches; False when anything is
    stale/degraded; None when there was nothing checkable (empty scope, or the
    adapter can't enumerate newest records).
    """

    ok: Optional[bool]
    identity: str  # "match" | "mismatch" | "unknown"
    mode: str  # what recall() runs right now (honest-degradation string)
    total: int  # records in scope
    checked: int  # newest ACTIVE records actually checked
    embedded: int  # of checked, how many carry a vector
    retrievable: int  # of checked, how many an actual search surfaces
    stale_ids: tuple[str, ...] = ()  # checked records that failed either leg
    notes: tuple[str, ...] = ()

    def to_dict(self) -> dict:
        """JSON-safe view — the seam ``rekoll doctor`` (CLI) renders from."""
        return {
            "ok": self.ok,
            "identity": self.identity,
            "mode": self.mode,
            "total": self.total,
            "checked": self.checked,
            "embedded": self.embedded,
            "retrievable": self.retrievable,
            "stale_ids": list(self.stale_ids),
            "notes": list(self.notes),
        }


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
        redact_pii: bool = False,
        max_content_chars: int = DEFAULT_MAX_CONTENT_CHARS,
        max_file_bytes: int = DEFAULT_MAX_FILE_BYTES,
        max_chunks_per_doc: int = DEFAULT_MAX_CHUNKS_PER_DOC,
    ) -> None:
        """``default_trust`` applies to first-person ``remember()`` calls ONLY.

        Bulk ingestion (``ingest_text`` / ``ingest_path``) always defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) regardless of this setting, so a
        high default can never silently exempt third-party files from the
        firewall's quarantine (ADR-0016).

        ``max_content_chars`` caps one ``remember()`` record; ``max_file_bytes``
        caps one ingested file/document's bytes; ``max_chunks_per_doc`` caps how
        many chunks one document may yield (bytes alone don't bound work — a
        heading-per-line document chunks at ~0.25 chunks/byte). All ADR-0018:
        overridable, never disable-able to zero.
        """
        if not str(path).strip():
            # An empty path used to fall through to the ':memory:' branch: the
            # store LOOKED fine but was ephemeral, and every write evaporated
            # on close. Ephemeral must be an explicit opt-in, never a typo.
            raise ValueError(
                "path is empty; pass a real database file path, or ':memory:' "
                "to explicitly opt into an ephemeral in-memory store"
            )
        if max_content_chars <= 0 or max_file_bytes <= 0 or max_chunks_per_doc <= 0:
            raise ValueError(
                "max_content_chars, max_file_bytes and max_chunks_per_doc must be positive"
            )
        self.scope = Scope(tenant=tenant, project=project, agent=agent)
        self._screen = screen
        self._default_trust = default_trust
        self._redact_pii = redact_pii
        self._max_content_chars = max_content_chars
        self._max_file_bytes = max_file_bytes
        self._max_chunks_per_doc = max_chunks_per_doc

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
        #: Process-local was-it-used ledger: which ids each recall surfaced.
        self.ledger = RecallLedger()

        existing = self.adapter.get_embedder_identity(scope=self.scope)
        current = self.embedder.identity()
        if existing is None:
            # Fresh scope: the current embedder claims it — a match from here on.
            self.adapter.set_embedder_identity(scope=self.scope, identity=current)
            self._identity_state = "match"
        else:
            self._identity_state = compare_identity(existing, current)
        if self._identity_state == "mismatch":
            # Refuse-and-degrade (ADR-0024): a silent model/config swap is the
            # classic silent recall killer — vectors from two embedders are not
            # comparable, so ranking across them returns confidently-wrong
            # results. We refuse the vector leg (reads go lexical-only, writes
            # store no vector) instead of bluffing, and instead of hard-failing
            # the whole store. Show the FULL identity (name + dim + config) — a
            # dim/config-only swap under the same model name would otherwise
            # print an identical-looking message. Routed through warnings so
            # hosts can filter/capture it.
            warnings.warn(
                f"[rekoll] this scope was embedded with {existing.name!r} "
                f"(dim={existing.dim}, config={existing.config_hash}), but the current embedder "
                f"is {current.name!r} (dim={current.dim}, config={current.config_hash}). "
                f"The vector leg is REFUSED for this scope (ADR-0024): recall degrades to "
                f"lexical-only (see RecallResult.mode) and new writes are stored without "
                f"vectors. To restore vector recall, call Memory.reindex() to re-embed this "
                f"scope with the current embedder (do NOT just re-ingest: identical content is "
                f"content-addressed and re-ingesting it under the mismatch stores no vector). "
                f"Or open a separate scope for the new embedder.",
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
        vouching, never an inherited default (ADR-0017).
        """
        if kind is Kind.DIRECTIVE and trust is None:
            raise ValueError(
                "kind=DIRECTIVE writes to the instruction channel of the recall "
                "envelope and must carry an explicit trust= (e.g. "
                "trust=TrustTier.OWNER for a rule you authored). Directives "
                "below TrustTier.TRUSTED_SOURCE are stored but render as "
                "evidence, never as instructions (ADR-0017)."
            )
        if len(content) > self._max_content_chars:
            raise ValueError(
                f"content is {len(content):,} chars, over the "
                f"max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a document belongs in ingest_text()/ingest_path() "
                "(which chunk it), or raise max_content_chars (ADR-0018)."
            )
        record = self._make_record(
            content=content,
            kind=kind,
            provenance=Provenance(source_uri=source, adapter_name="memory"),
            trust=self._default_trust if trust is None else trust,
            metadata=metadata,
        )
        # The cap governs what is STORED. The raw-length check above is only a
        # fast fail: firewall sanitization NFKC-normalizes, and compatibility
        # codepoints can EXPAND (U+FDFA becomes 18 chars — an 18x amplifier), so
        # in-cap input can produce over-cap stored content. Enforce on the
        # post-sanitization record before it reaches storage (ADR-0018).
        if len(record.content) > self._max_content_chars:
            raise ValueError(
                f"content is {len(record.content):,} chars after firewall "
                f"sanitization (NFKC normalization expands some codepoints), over "
                f"the max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a document belongs in ingest_text()/ingest_path() "
                "(which chunk it), or raise max_content_chars (ADR-0018)."
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
        batch: int = 256,
    ) -> int:
        """Chunk a document and store it. Returns the number of chunks stored.

        Ingested text is third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0016). Pass
        ``trust=`` explicitly to vouch for a source you control.

        A document over ``max_file_bytes`` OR chunking into more than
        ``max_chunks_per_doc`` pieces raises (ADR-0018): bytes alone don't
        bound work — a heading-per-line document yields ~0.25 chunks/byte.
        Chunks are embedded + stored in bounded batches of ``batch``, so peak
        memory tracks the batch, never the document.
        """
        n_bytes = len(text.encode("utf-8"))  # a BYTES limit — measure bytes, not chars
        if n_bytes > self._max_file_bytes:
            raise ValueError(
                f"document is {n_bytes:,} bytes, over the "
                f"max_file_bytes={self._max_file_bytes:,} ingestion limit; "
                "split it or raise max_file_bytes (ADR-0018)."
            )
        pieces = chunk_file(name, text)
        if len(pieces) > self._max_chunks_per_doc:
            raise ValueError(
                f"document chunked into {len(pieces):,} pieces, over the "
                f"max_chunks_per_doc={self._max_chunks_per_doc:,} ingestion "
                "limit — rejected rather than silently truncated; split the "
                "document or raise max_chunks_per_doc (ADR-0018)."
            )
        src = source or f"text://{name}"
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        stored = 0
        pending: list[MemoryRecord] = []
        for i, piece in enumerate(pieces):
            if self._screen and not sanitize_unicode(piece):
                continue  # nothing survives screening (e.g. only zero-width chars)
            pending.append(
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
            stored += 1
            if len(pending) >= batch:
                self._embed_and_store(pending)
                pending = []
        if pending:
            self._embed_and_store(pending)
        return stored

    def ingest_path(
        self,
        path: str,
        *,
        include_ext: Optional[Iterable[str]] = None,
        skip_dirs: Optional[Iterable[str]] = None,
        trust: Optional[TrustTier] = None,
        batch: int = 256,
        follow_symlinks: bool = False,
    ) -> dict:
        """Index a file or directory (code + docs).

        Returns ``{files, chunks, skipped, total}`` — ``skipped`` counts files
        passed over (symlink, over ``max_file_bytes``, over
        ``max_chunks_per_doc``, undecodable, or unreadable).

        Linked files are skipped unless ``follow_symlinks=True``: a planted link
        in a third-party tree can point anywhere on disk (e.g. ``~/.ssh/id_rsa``),
        and a bulk walk must not read outside the tree it was pointed at.
        Containment is by REAL path (``os.path.realpath`` + ``is_relative_to``),
        so symlinks, NTFS junctions, and any other reparse point are all caught —
        ``is_symlink()`` alone misses a junction. Directory links (symlink or
        junction) are never descended, even under ``follow_symlinks=True``.

        Files on disk are third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0016). Pass
        ``trust=`` explicitly to vouch for a tree you control.
        """
        include = set(include_ext) if include_ext else DEFAULT_INCLUDE_EXT
        skip = set(skip_dirs) if skip_dirs else DEFAULT_SKIP_DIRS
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        root = Path(path).expanduser()
        if not follow_symlinks and _redirects_out(root):
            # A directly-pointed link (symlink OR junction) is skipped, not read
            # — it resolves outside the tree it names. Say so: the caller almost
            # certainly expected it to be ingested. (A directory symlink or
            # junction pointed at directly used to be walked silently; this
            # closes that.)
            warnings.warn(
                f"[rekoll] ingest_path was pointed at a symlink or junction "
                f"({path!r}); it was skipped because a link can point outside "
                "the intended tree. Pass follow_symlinks=True to read it.",
                stacklevel=2,
            )
            return {"files": 0, "chunks": 0, "skipped": 1, "total": self.count()}
        root_real = _real_path(root)
        targets = [root] if root.is_file() else list(self._walk(root, include, skip))
        files = 0
        chunks = 0
        skipped = 0
        pending: list[MemoryRecord] = []
        for fp in targets:
            try:
                if not follow_symlinks and (
                    fp.is_symlink() or not _within(root_real, fp)
                ):
                    # Skip a symlinked file (a planted link can point outside the
                    # tree — pinned regardless of where it resolves) AND, defense-
                    # in-depth, anything whose REAL path escapes root (a reparse
                    # file or a mid-walk TOCTOU swap). A non-redirecting reparse
                    # point that stays in-root (OneDrive/dedup) is read normally.
                    skipped += 1
                    continue
                if fp.stat().st_size > self._max_file_bytes:
                    skipped += 1  # fast-path skip for a known-oversized file
                    continue
                # Bounded read: the file may have GROWN since stat() (TOCTOU), so
                # never pull more than the limit (+1 to detect overflow) into
                # memory regardless of what stat reported.
                with fp.open("rb") as fh:
                    raw = fh.read(self._max_file_bytes + 1)
                if len(raw) > self._max_file_bytes:
                    skipped += 1
                    continue
                text = raw.decode("utf-8")
            except (UnicodeDecodeError, OSError):
                skipped += 1
                continue
            rel = fp.name if root.is_file() else fp.relative_to(root).as_posix()
            pieces = chunk_file(rel, text)
            if len(pieces) > self._max_chunks_per_doc:
                # Chunk-count explosion (bytes don't bound work, ADR-0018):
                # reject THIS document — whole, never truncated — and keep
                # walking, mirroring the max_file_bytes skip above.
                skipped += 1
                continue
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
        summary = summary.strip()
        # The third write door respects the same per-record bound as remember()
        # (ADR-0018): a summary should be SHORTER than its sources, so exceeding
        # the cap means the consolidator failed to condense — fail loud rather
        # than store an unbounded LLM output.
        if len(summary) > self._max_content_chars:
            raise ValueError(
                f"consolidator returned {len(summary):,} chars, over the "
                f"max_content_chars={self._max_content_chars:,} limit for one "
                "memory; a consolidation summary should be shorter than its "
                "sources — raise max_content_chars if this is intended (ADR-0018)."
            )
        name = str(getattr(consolidator, "name", type(consolidator).__name__))
        record = self._make_record(
            content=summary,
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
        self,
        query: str,
        *,
        k: int = 5,
        kind: Optional[Kind] = None,
        rerank: bool = True,
        call_id: Optional[str] = None,
    ) -> RecallResult:
        """Hybrid + reranked search. Quarantined memory is excluded; reads call no LLM.

        The query is firewall-sanitized and truncated to
        ``retrieval.MAX_QUERY_CHARS`` before embedding (DESIGN §7, ADR-0018).

        May return FEWER than ``k`` hits: quarantined memory is excluded, and any
        candidate that fails content-hash verification (direct-DB tampering) is
        withheld with a warning (ADR-0019). ``k`` is an upper bound, not a promise.

        ``RecallResult.mode`` names exactly what ran (honest degradation).
        The surfaced ids are recorded in the was-it-used ledger; pass
        ``call_id`` to attribute this recall to one host action so
        :meth:`informed_by` can join them later.
        """
        result = self._search(query, k=k, kind=kind, rerank=rerank)
        try:
            self.ledger.record(
                [h.record.id for h in result.hits], query=query, call_id=call_id
            )
        except Exception:
            pass  # even a host-swapped, raising ledger must never break a read
        return result

    def context(self, query: str, *, k: int = 5) -> str:
        """Shortcut: the LLM-ready, firewall-framed context string for a query."""
        return self.recall(query, k=k).context()

    def count(self) -> int:
        return self.adapter.count(scope=self.scope)

    def close(self) -> None:
        self.adapter.close()

    # -- was-it-used loop -----------------------------------------------------
    def mark_used(self, *ids: str) -> int:
        """Report that these memories actually informed an action; returns how
        many were credited.

        This is the loop-closing usage signal: recall metrics say "we surfaced
        it", ``mark_used`` lets the host say "we acted on it". Each credited
        record's ``proof_count`` is incremented — a PROMOTION-ONLY signal:
        usage may extend a memory's standing, it never shortens another's, and
        it never touches trust_tier or status (trust is set at the ingestion
        boundary and immutable to output, ADR-0002). Unknown / out-of-scope /
        quarantined ids are ignored.

        The increment is a TARGETED, atomic ``proof_count += 1`` at the adapter
        (``bump_proof_count``), not a read-modify-write of the whole row: two
        concurrent credits both land, and a concurrent change to any OTHER
        column is never reverted. (An adapter that hasn't specialized the bump
        falls back to the read-modify-write, correct under a single writer.)
        """
        return self.adapter.bump_proof_count(scope=self.scope, ids=list(ids))

    def informed_by(self, call_id: Optional[str] = None, *, limit: int = 5) -> list[LedgerEntry]:
        """The recent recalls (ids + query + ts) that plausibly informed an
        action being finished right now — for hosts that attach usage evidence
        to their own receipts/logs instead of calling :meth:`mark_used`
        directly. With ``call_id``, only recalls recorded under that call_id
        are returned (no cross-conversation credit). Best-effort: [] on any
        ledger failure.
        """
        return self.ledger.entries(call_id, limit=limit)

    # -- health -----------------------------------------------------------------
    # SEAM: the CLI's `rekoll doctor` calls Memory.health() (and Memory.self_test())
    # and renders HealthReport.to_dict() — keep these signatures stable.
    def health(self, *, n: int = 3, k: int = 5) -> HealthReport:
        """Source-of-truth-vs-index freshness check (read-only).

        Asserts the newest ``n`` ACTIVE records are (a) EMBEDDED (carry a
        vector) and (b) RETRIEVABLE (an actual search over their own content
        surfaces them within a membership window of ``max(k, 20)`` — widened so
        near-duplicate corpora and approximate vector indexes don't read as
        dead ingestion). The check runs store-vs-index, not index-only — an
        index-only "is the corpus healthy?" query reads green forever over dead
        ingestion, because it can only see what already made it in. Also
        reports the embedder-identity state and the exact recall mode, so a
        degraded scope can't look healthy.

        Fail-soft, always: health must never take the host down. An empty or
        unsupported store, a storage read that errors, or a retrievability probe
        that raises (a broken index leg) all degrade to an honest report (``ok``
        is ``None`` when nothing was checkable, ``False`` when a checked record
        failed) with a diagnostic note — never a propagated exception.
        """
        notes: list[str] = []
        mode = self._mode()
        try:
            total = self.count()
        except Exception as exc:  # a store that can't even be counted is not "ok"
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=0,
                checked=0, embedded=0, retrievable=0,
                notes=(f"could not read the store ({type(exc).__name__}) — health unknown",),
            )
        if total == 0:
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=0,
                checked=0, embedded=0, retrievable=0,
                notes=("empty scope — nothing to check",),
            )
        try:
            # Over-fetch so quarantined/superseded rows don't eat the sample.
            newest = self.adapter.newest(scope=self.scope, n=max(n * 3, n)).records
        except UnsupportedCapabilityError:
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=total,
                checked=0, embedded=0, retrievable=0,
                notes=(
                    f"adapter '{self.adapter.name}' cannot enumerate newest records — "
                    "freshness unknown",
                ),
            )
        except Exception as exc:
            # Fail-soft: ANY other storage error is reported, never raised — a
            # health check must never take the host down (the whole point).
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=total,
                checked=0, embedded=0, retrievable=0,
                notes=(f"could not enumerate records ({type(exc).__name__}) — health unknown",),
            )
        active_all = [r for r in newest if r.status is Status.ACTIVE]
        active = active_all[:n]
        skipped = len(newest) - len(active_all)
        if skipped:
            notes.append(f"skipped {skipped} non-active record(s) in the newest sample")
        if not active:
            return HealthReport(
                ok=None, identity=self._identity_state, mode=mode, total=total,
                checked=0, embedded=0, retrievable=0,
                notes=(*notes, "no active records in the newest sample — nothing checkable"),
            )
        embedded = 0
        retrievable = 0
        stale: list[str] = []
        probe_errors = 0
        for record in active:
            has_vector = record.embedding is not None
            embedded += int(has_vector)
            # Retrievability probe: search the record's own content through the
            # real read path (no reranker — membership in the window is the
            # check, not order; no ledger — probes must not claim usage credit).
            # Fail-soft per record: a probe that RAISES (a broken index leg) must
            # degrade this record to stale + not-ok, never propagate and take the
            # host down. That is exactly the "index is broken" state health exists
            # to surface honestly.
            try:
                probe = self._search(
                    record.content[:_PROBE_MAX_CHARS],
                    k=max(k, _PROBE_MIN_POOL),
                    rerank=False,
                )
                found = record.id in {h.record.id for h in probe.hits}
            except Exception:
                found = False
                probe_errors += 1
            retrievable += int(found)
            if not (has_vector and found):
                stale.append(record.id)
        if probe_errors:
            notes.append(
                f"{probe_errors} retrievability probe(s) raised — the search path "
                "may be broken; treating those records as not retrievable"
            )
        if self._identity_state == "mismatch":
            notes.append(
                "embedder identity mismatch — vector leg refused (ADR-0024); "
                "call Memory.reindex() to re-embed this scope with the current embedder"
            )
        ok = not stale and self._identity_state != "mismatch"
        if stale:
            notes.append(
                "newest record(s) not fully indexed — ingestion/embedding may be dead"
            )
        return HealthReport(
            ok=ok, identity=self._identity_state, mode=mode, total=total,
            checked=len(active), embedded=embedded, retrievable=retrievable,
            stale_ids=tuple(stale), notes=tuple(notes),
        )

    def self_test(self, *, k: int = 3) -> dict:
        """Golden-probe end-to-end self-test: store a known record, assert a
        known query returns it at rank 1, then remove it.

        Exercises the REAL write→embed→index→search path in whatever mode the
        scope is currently in (a lexical-only degraded scope still passes if
        lexical recall works — the probe tests the system you actually have,
        and ``mode`` in the result names it). Unlike :meth:`health` this
        WRITES (one sentinel record, removed afterwards; the id is
        content-addressed so a crashed probe re-run is idempotent).

        Returns ``{"ok", "rank", "mode"}`` — ``rank`` is 1-based or None when
        the sentinel didn't surface at all.
        """
        sentinel = (
            "Rekoll golden-probe sentinel: the amethyst lighthouse indexes "
            "maple syllables."
        )
        query = "amethyst lighthouse maple syllables"
        record = self.remember(sentinel, source="rekoll://self-test")
        try:
            result = self._search(query, k=k)  # no ledger: probes claim no usage
            ids = [h.record.id for h in result.hits]
            rank = ids.index(record.id) + 1 if record.id in ids else None
            return {"ok": rank == 1, "rank": rank, "mode": result.mode}
        finally:
            self.forget(record.id)

    # -- recovery ----------------------------------------------------------------
    def reindex(self, *, batch: int = 256) -> int:
        """Re-embed EVERY record in this scope with the current embedder, then
        rebind the scope's stored embedder identity to it. Returns how many
        records were re-embedded.

        This is the real recovery from an embedder-identity mismatch (ADR-0024):
        after a model/config swap the scope is refused the vector leg and reads
        go lexical-only. ``reindex()`` rewrites every stored vector with the
        embedder you are holding NOW and *then* claims the scope for it, so the
        vector leg comes back and :meth:`health` reads green again — WITHOUT the
        recovery trap of "just re-ingest" (re-ingesting identical content under
        the mismatch stores no vector; this method computes them).

        Order matters and is deliberate: vectors are written FIRST, the identity
        is rebound LAST. A crash midway leaves the scope still-mismatched (safe,
        degraded) rather than identity-clean over half-stale vectors — the write
        is re-runnable and idempotent (same content-addressed ids, unchanged
        trust, so the trust-monotonic upsert updates each row in place, ADR-0023).

        Re-embedding is only skipped when the embedder is already a match AND no
        record is missing a vector (a genuine no-op); otherwise every in-scope
        record — active, superseded, or quarantined — is refreshed so no stale
        vector family is left behind.
        """
        total = self.count()
        current = self.embedder.identity()
        if total == 0:
            # Empty scope: nothing to embed, just (re)claim it for this embedder.
            self.adapter.set_embedder_identity(scope=self.scope, identity=current)
            self._identity_state = "match"
            return 0
        try:
            records = list(self.adapter.newest(scope=self.scope, n=total).records)
        except UnsupportedCapabilityError as exc:
            raise UnsupportedCapabilityError(
                f"adapter '{self.adapter.name}' cannot enumerate its records, so "
                "reindex() cannot re-embed them; recover by re-ingesting this "
                "scope's sources with the current embedder into a fresh store"
            ) from exc
        name, dim = current.name, self.embedder.dim
        done = 0
        for start in range(0, len(records), batch):
            chunk = records[start : start + batch]
            vectors = self.embedder.embed([r.content for r in chunk])
            for record, vector in zip(chunk, vectors):
                record.with_embedding(vector, name=name, dim=dim)
            # Upsert re-embedded rows BEFORE rebinding identity: while the stored
            # identity still mismatches, the trust-monotonic same-id upsert
            # updates the embedding column in place (ADR-0023) — it neither drops
            # trust nor nulls the vector we just computed.
            self.adapter.upsert(records=chunk)
            done += len(chunk)
        # Vectors are all current: NOW claim the scope for this embedder so reads
        # use the vector leg again.
        self.adapter.set_embedder_identity(scope=self.scope, identity=current)
        self._identity_state = "match"
        return done

    # -- internals ----------------------------------------------------------
    def _search(
        self, query: str, *, k: int, kind: Optional[Kind] = None, rerank: bool = True
    ) -> RecallResult:
        """The one read path recall/health/self_test share (no ledger write)."""
        use_vector = self._identity_state != "mismatch"
        reranker = self.reranker if rerank else None
        result = hybrid_search(
            self.adapter, scope=self.scope, query=query, embedder=self.embedder,
            k=k, kind=kind, reranker=reranker, use_vector=use_vector,
        )
        return RecallResult(
            hits=tuple(result.hits), mode=self._mode(reranked=reranker is not None)
        )

    def _mode(self, *, reranked: Optional[bool] = None) -> str:
        """Compose the honest-degradation string: exactly what a read runs.

        ``reranked=None`` (health/introspection) describes the default recall
        configuration; a bool describes one concrete search.
        """
        lexical = self.adapter.supports(CAP_LEXICAL)
        rerank = (self.reranker is not None) if reranked is None else reranked
        if self._identity_state == "mismatch":
            base = "lexical-only" if lexical else "none"
            if rerank and lexical:
                base += "+rerank"
            return f"{base}: embedder mismatch"
        legs = ["vector"] + (["lexical"] if lexical else []) + (["rerank"] if rerank else [])
        mode = "+".join(legs)
        if isinstance(self.embedder, StubEmbedder):
            mode += " (stub-embedder)"  # deterministic hash vectors, no semantics
        return mode

    def _make_record(self, *, content, kind, provenance, trust, metadata, **kwargs):
        if self._screen:
            return screened_record(
                scope=self.scope, kind=kind, content=content,
                provenance=provenance, trust_tier=trust, metadata=metadata,
                redact_pii=self._redact_pii, **kwargs,
            )
        return MemoryRecord.create(
            scope=self.scope, kind=kind, content=content,
            provenance=provenance, trust_tier=trust, metadata=metadata or {}, **kwargs,
        )

    def _embed_and_store(self, records: list[MemoryRecord]) -> None:
        if not records:
            return
        if self._identity_state == "mismatch":
            # Under a mismatch we do NOT embed NEW content with the current
            # embedder (ADR-0024): a second vector family in the scope would be
            # unqueryable (the leg is refused) and deepen the very corruption the
            # guard exists to stop. Lexical indexing still covers the content;
            # health() flags the scope until reindex() re-embeds it.
            #
            # But ids are content-addressed, so re-ingesting IDENTICAL content
            # lands on a row that may ALREADY carry a good (pre-swap) vector. The
            # upsert would rewrite that row with embedding=NULL — silently
            # destroying the very vectors reindex() needs. Carry the stored
            # embedding forward so the in-place rewrite preserves it verbatim.
            self._preserve_existing_embeddings(records)
        else:
            to_embed = [r for r in records if r.embedding is None]
            if to_embed:
                vectors = self.embedder.embed([r.content for r in to_embed])
                name, dim = self.embedder.identity().name, self.embedder.dim
                for record, vector in zip(to_embed, vectors):
                    record.with_embedding(vector, name=name, dim=dim)
        self.adapter.upsert(records=records)

    def _preserve_existing_embeddings(self, records: list[MemoryRecord]) -> None:
        """Copy any already-stored vector onto an embedding-less incoming record
        so an in-place upsert never nulls a good vector (ADR-0024, the recovery
        trap). Only touches records that would otherwise write NULL; genuinely
        new content stays vector-free under the mismatch."""
        needing = [r for r in records if r.embedding is None]
        if not needing:
            return
        stored = {
            r.id: r
            for r in self.adapter.get(scope=self.scope, ids=[r.id for r in needing]).records
        }
        for record in needing:
            prior = stored.get(record.id)
            if prior is not None and prior.embedding is not None:
                record.embedding = prior.embedding
                record.embedder_name = prior.embedder_name
                record.embedder_dim = prior.embedder_dim

    @staticmethod
    def _walk(root: Path, include: set, skip: set):
        # os.walk(followlinks=False) already refuses to DESCEND directory
        # symlinks, but it happily descends a junction (is_symlink()==False). So
        # prune any directory whose REAL path escapes the root before os.walk
        # recurses into it — catching junctions, reparse points, and dir symlinks
        # alike. This is unconditional (like "dir symlinks are never descended"):
        # an out-of-tree directory is never walked, even under follow_symlinks.
        root_real = _real_path(root)
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [
                d for d in dirnames
                if d not in skip and not d.endswith(".egg-info")
                and _within(root_real, os.path.join(dirpath, d))
            ]
            for name in filenames:
                fp = Path(dirpath) / name
                if fp.suffix.lower() in include:
                    yield fp
