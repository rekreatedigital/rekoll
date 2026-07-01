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
degradation via ``RecallResult.mode`` (ADR-0015).
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
from .ledger import RecallLedger
from .model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from .retrieval import hybrid_search

__all__ = [
    "Memory",
    "RecallResult",
    "HealthReport",
    "DEFAULT_INGEST_TRUST",
    "DEFAULT_MAX_CONTENT_CHARS",
    "DEFAULT_MAX_FILE_BYTES",
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
    ) -> None:
        """``default_trust`` applies to first-person ``remember()`` calls ONLY.

        Bulk ingestion (``ingest_text`` / ``ingest_path``) always defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) regardless of this setting, so a
        high default can never silently exempt third-party files from the
        firewall's quarantine (ADR-0016).

        ``max_content_chars`` caps one ``remember()`` record; ``max_file_bytes``
        caps one ingested file/document (ADR-0018). Both overridable, never
        disable-able to zero.
        """
        if max_content_chars <= 0 or max_file_bytes <= 0:
            raise ValueError("max_content_chars and max_file_bytes must be positive")
        self.scope = Scope(tenant=tenant, project=project, agent=agent)
        self._screen = screen
        self._default_trust = default_trust
        self._redact_pii = redact_pii
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
            # Refuse-and-degrade (ADR-0015): a silent model/config swap is the
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
                f"The vector leg is REFUSED for this scope (ADR-0015): recall degrades to "
                f"lexical-only (see RecallResult.mode) and new writes are stored without "
                f"vectors. Re-ingest this scope with one embedder, or use a separate scope.",
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
        chunk — NOT to the constructor's ``default_trust`` (ADR-0016). Pass
        ``trust=`` explicitly to vouch for a source you control.
        """
        n_bytes = len(text.encode("utf-8"))  # a BYTES limit — measure bytes, not chars
        if n_bytes > self._max_file_bytes:
            raise ValueError(
                f"document is {n_bytes:,} bytes, over the "
                f"max_file_bytes={self._max_file_bytes:,} ingestion limit; "
                "split it or raise max_file_bytes (ADR-0018)."
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
        follow_symlinks: bool = False,
    ) -> dict:
        """Index a file or directory (code + docs).

        Returns ``{files, chunks, skipped, total}`` — ``skipped`` counts files
        passed over (symlink, over ``max_file_bytes``, undecodable, or
        unreadable).

        Symlinked files are skipped unless ``follow_symlinks=True``: a planted
        link in a third-party tree can point anywhere on disk (e.g.
        ``~/.ssh/id_rsa``), and a bulk walk must not read outside the tree it
        was pointed at. Directory symlinks are never descended.

        Files on disk are third-party by nature, so ``trust`` defaults to
        ``DEFAULT_INGEST_TRUST`` (UNVERIFIED) — injection markers quarantine the
        chunk — NOT to the constructor's ``default_trust`` (ADR-0016). Pass
        ``trust=`` explicitly to vouch for a tree you control.
        """
        include = set(include_ext) if include_ext else DEFAULT_INCLUDE_EXT
        skip = set(skip_dirs) if skip_dirs else DEFAULT_SKIP_DIRS
        trust = DEFAULT_INGEST_TRUST if trust is None else trust
        root = Path(path).expanduser()
        targets = [root] if root.is_file() else list(self._walk(root, include, skip))
        if root.is_file() and root.is_symlink() and not follow_symlinks:
            # A directly-pointed symlink is skipped, not read — say so, since the
            # caller almost certainly expected it to be ingested.
            warnings.warn(
                f"[rekoll] ingest_path was pointed at a symlink ({path!r}); it "
                "was skipped because a link can point outside the intended tree. "
                "Pass follow_symlinks=True to read it.",
                stacklevel=2,
            )
        files = 0
        chunks = 0
        skipped = 0
        pending: list[MemoryRecord] = []
        for fp in targets:
            try:
                if not follow_symlinks and fp.is_symlink():
                    skipped += 1  # a planted link can point outside the tree
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
        """
        records = [
            r
            for r in self.adapter.get(scope=self.scope, ids=list(ids)).records
            if r.status is not Status.QUARANTINED
        ]
        if not records:
            return 0
        for record in records:
            record.proof_count += 1
        self.adapter.upsert(records=records)
        return len(records)

    def informed_by(self, call_id: Optional[str] = None, *, limit: int = 5) -> list:
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
        surfaces them in the top ``k``). The check runs store-vs-index, not
        index-only — an index-only "is the corpus healthy?" query reads green
        forever over dead ingestion, because it can only see what already made
        it in. Also reports the embedder-identity state and the exact recall
        mode, so a degraded scope can't look healthy.

        Never raises for an empty/unsupported store — it reports ``ok=None``
        with a note instead (fail-soft: health must never take the host down).
        """
        notes: list[str] = []
        mode = self._mode()
        total = self.count()
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
        for record in active:
            has_vector = record.embedding is not None
            embedded += int(has_vector)
            # Retrievability probe: search the record's own content through the
            # real read path (no reranker — membership in top-k is the check,
            # not order; no ledger — probes must not claim usage credit).
            probe = self._search(record.content[:256], k=max(k, n), rerank=False)
            found = record.id in {h.record.id for h in probe.hits}
            retrievable += int(found)
            if not (has_vector and found):
                stale.append(record.id)
        if self._identity_state == "mismatch":
            notes.append(
                "embedder identity mismatch — vector leg refused (ADR-0015); "
                "re-ingest this scope with one embedder"
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
        # Under an identity mismatch we store WITHOUT vectors rather than write a
        # second vector family into the scope (ADR-0015): those vectors would be
        # unqueryable (the leg is refused) and would deepen the very corruption
        # the guard exists to stop. Lexical indexing still covers the content;
        # health() flags the scope until it is re-ingested with one embedder.
        to_embed = (
            [r for r in records if r.embedding is None]
            if self._identity_state != "mismatch"
            else []
        )
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
