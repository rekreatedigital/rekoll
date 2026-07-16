"""The ``StorageAdapter`` ABC — the single contract every backend implements.

Design notes (ADR-0005):
 - Methods are KEYWORD-ONLY so call sites are self-documenting and stable.
 - A REQUIRED vector+metadata core; lexical/relational are optional CAPABILITIES
   advertised in ``capabilities``. Calling an unsupported op raises
   ``UnsupportedCapabilityError`` — a backend never silently drops a feature.
 - Every read/write carries a ``Scope``; cross-scope reads are forbidden.
 - Results are TYPED dataclasses, never raw dicts.
 - The adapter persists/serves the per-scope embedder identity for the guard.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from ..embedding import EmbedderIdentity
from ..model import Kind, MemoryRecord, Scope, Status

__all__ = [
    "StorageAdapter",
    "QueryHit",
    "QueryResult",
    "GetResult",
    "UnsupportedCapabilityError",
    "CAP_VECTOR",
    "CAP_LEXICAL",
    "CAP_RELATIONAL",
    "CAP_VECTOR_INDEX",
]

CAP_VECTOR = "vector"
CAP_LEXICAL = "lexical"
CAP_RELATIONAL = "relational"

#: Advertised by a backend whose ``vector_query`` is served by a real vector
#: INDEX (HNSW/IVF/sqlite-vec/pgvector/...) rather than an exhaustive scan of
#: every stored vector (ADR-0030).
#:
#: This is a *cost* signal, not a correctness one. A backend that does not
#: advertise it still returns exact top-k; it just pays O(N·dim) per query, so
#: read latency grows linearly with the store. Callers that must stay within a
#: latency budget at large N can check ``supports(CAP_VECTOR_INDEX)`` and pick a
#: heavier backend, or size their store accordingly.
#:
#: Advertising it also means the caller should EXPECT APPROXIMATE recall: an ANN
#: index may omit a true nearest neighbour. ``Memory.health()`` already widens
#: its retrievability probe to a membership window for exactly this reason, so
#: an approximate self-match that is not top-1 does not read as dead ingestion.
#:
#: The reference SQLite adapter deliberately does NOT advertise it: it is an
#: exact, cached, vectorized full scan.
CAP_VECTOR_INDEX = "vector_index"


class UnsupportedCapabilityError(Exception):
    """Raised when an adapter is asked for a capability it does not advertise."""


@dataclass(frozen=True)
class QueryHit:
    record: MemoryRecord
    score: float


@dataclass(frozen=True)
class QueryResult:
    hits: tuple[QueryHit, ...]

    def __iter__(self):
        return iter(self.hits)

    def __len__(self) -> int:
        return len(self.hits)


@dataclass(frozen=True)
class GetResult:
    records: tuple[MemoryRecord, ...]

    def __iter__(self):
        return iter(self.records)

    def __len__(self) -> int:
        return len(self.records)


class StorageAdapter(ABC):
    #: Stable backend name (also the entry-point key).
    name: str = "abstract"
    #: Advertised capabilities beyond the required vector core.
    capabilities: frozenset[str] = frozenset({CAP_VECTOR})
    #: Vector distance metric this backend ranks by.
    distance_metric: str = "cosine"

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities

    # --- required: writes --------------------------------------------------
    @abstractmethod
    def add(self, *, records: Sequence[MemoryRecord]) -> None:
        """Insert records. Raises on an id/content-address collision."""

    @abstractmethod
    def upsert(self, *, records: Sequence[MemoryRecord]) -> None:
        """Insert-or-replace by content-addressed id. Idempotent."""

    @abstractmethod
    def delete(self, *, scope: Scope, ids: Sequence[str]) -> int:
        """Delete records in ``scope`` by id; return how many were removed."""

    # --- required: reads ---------------------------------------------------
    @abstractmethod
    def get(self, *, scope: Scope, ids: Sequence[str]) -> GetResult:
        """Fetch records by id within ``scope`` (cross-scope ids are ignored)."""

    @abstractmethod
    def count(
        self, *, scope: Scope, kind: Optional[Kind] = None, status: Optional[str] = None
    ) -> int:
        """Count records in ``scope`` (optionally filtered to one kind and/or a
        ``status`` value such as ``"active"`` / ``"quarantined"``; ``None`` counts
        every row regardless of status)."""

    @abstractmethod
    def vector_query(
        self,
        *,
        scope: Scope,
        embedding: Sequence[float],
        k: int = 10,
        kind: Optional[Kind] = None,
        where: Optional[Mapping[str, object]] = None,
    ) -> QueryResult:
        """Vector similarity search within ``scope`` (the required core op)."""

    # --- optional capability: lexical -------------------------------------
    def lexical_query(
        self,
        *,
        scope: Scope,
        text: str,
        k: int = 10,
        kind: Optional[Kind] = None,
        where: Optional[Mapping[str, object]] = None,
    ) -> QueryResult:
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support lexical search "
            f"(capability '{CAP_LEXICAL}' not advertised)"
        )

    # --- optional: newest-record enumeration (freshness checks) -----------
    def newest(self, *, scope: Scope, n: int = 3, kind: Optional[Kind] = None) -> GetResult:
        """The ``n`` most recently created records in ``scope``, newest first
        (ties broken by id for determinism).

        Consumed by ``Memory.health()``'s source-vs-index freshness check.
        Optional the same way ``lexical_query`` is: a backend that cannot
        enumerate by recency raises rather than silently returning a wrong
        sample, and health() reports freshness as unknown.
        """
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support newest-record enumeration"
        )

    # --- optional: standing-directive channel (always-surface rules) -------
    def active_directives(
        self, *, scope: Scope, limit: int, min_trust: int
    ) -> GetResult:
        """The ACTIVE ``Kind.DIRECTIVE`` records in ``scope`` at ``trust_tier >=
        min_trust``, in a STABLE, DETERMINISTIC order (oldest first: ``created_at``
        ascending, ``id`` ascending as the tiebreak), capped at ``limit``.

        This is the read behind Rekoll's standing-directive channel (ADR-0034):
        a saved rule must ALWAYS ride the recall envelope's instruction channel,
        not only when it happens to rank into a query's hits. It is a plain,
        scoped, deterministic DB read — **zero LLM, zero embedding** (ADR-0007) —
        and the ordering contract is load-bearing: ``Memory`` renders these in the
        order returned, and the envelope must be byte-stable for cache reuse, so
        the order may not depend on scores, timestamps-of-read, or row layout.

        Oldest-first is deliberate: under the ``limit`` cap the FOUNDATIONAL rules
        (the ones set at onboarding) survive, and appending a new rule never
        disturbs the pinned prefix — the rendered envelope's directive block stays
        prefix-stable as rules accrue, so a host's prompt cache is not busted.

        Optional the same way :meth:`lexical_query` / :meth:`newest` are: a backend
        that cannot serve it raises ``UnsupportedCapabilityError`` and ``Memory``
        degrades to the pre-ADR-0034 rank-only behavior (the rule still appears
        when it ranks in) — never a crash. ``limit <= 0`` returns an empty result.
        """
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support the standing-directive channel "
            "(active_directives enumeration)"
        )

    # --- was-it-used: proof_count increment -------------------------------
    def bump_proof_count(self, *, scope: Scope, ids: Sequence[str]) -> int:
        """Increment ``proof_count`` by one for each in-scope, non-quarantined
        record named in ``ids``; return how many were credited.

        This is the durable half of the was-it-used loop (``Memory.mark_used``).
        It is a TARGETED, additive update on purpose: a full-row upsert would
        (a) lose concurrent increments (read-modify-write races) and (b) revert
        any other column a concurrent writer changed between the read and the
        write. Adapters SHOULD implement this as an atomic ``proof_count =
        proof_count + 1`` so neither happens.

        The default here is a portable read-modify-write fallback for adapters
        that haven't specialized it; it is correct only under a single writer.
        The reference SQLite adapter overrides it with an atomic UPDATE.
        """
        wanted = list(ids)
        if not wanted:
            return 0
        records = [
            r for r in self.get(scope=scope, ids=wanted).records
            if r.status is not Status.QUARANTINED
        ]
        if not records:
            return 0
        for record in records:
            record.proof_count += 1
        self.upsert(records=records)
        return len(records)

    # --- embedder-identity guard (per scope) ------------------------------
    @abstractmethod
    def get_embedder_identity(self, *, scope: Scope) -> Optional[EmbedderIdentity]:
        """Return the embedder identity recorded for ``scope``, or None."""

    @abstractmethod
    def set_embedder_identity(self, *, scope: Scope, identity: EmbedderIdentity) -> None:
        """Record the embedder identity for ``scope`` (first writer wins)."""

    # --- lifecycle ---------------------------------------------------------
    def close(self) -> None:  # pragma: no cover - trivial default
        return None
