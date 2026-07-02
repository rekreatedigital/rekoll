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
]

CAP_VECTOR = "vector"
CAP_LEXICAL = "lexical"
CAP_RELATIONAL = "relational"


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
    def count(self, *, scope: Scope, kind: Optional[Kind] = None) -> int:
        """Count records in ``scope`` (optionally filtered to one kind)."""

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
