"""The core memory record and its value types.

Design decisions embodied here (see docs/adr/):
 - Four-kind *logical* vocabulary, FROZEN at v0 (ADR-0004): raw_fact, observation,
   directive, episode. The canonical schema stores these in SEPARATE physical
   tables (ADR-0001) — kind is the discriminator, not a JSONB blob.
 - Provenance + trust are first-class, NOT-NULL, set at the ingestion boundary,
   and immutable to LLM output (ADR-0002).
 - Metadata is FLAT SCALARS only — no nested/unbounded JSON anywhere (ADR-0001).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum, IntEnum
from typing import Mapping, Optional, Sequence, Union

from .ids import content_hash as _content_hash
from .ids import record_id

__all__ = [
    "Kind",
    "TrustTier",
    "Status",
    "Scope",
    "Provenance",
    "MemoryRecord",
    "Scalar",
]

Scalar = Union[str, int, float, bool, None]


class Kind(str, Enum):
    """The frozen, lifecycle-distinct memory kinds (ADR-0004)."""

    RAW_FACT = "raw_fact"
    OBSERVATION = "observation"
    DIRECTIVE = "directive"
    EPISODE = "episode"


class TrustTier(IntEnum):
    """Ordered trust. Set at ingest by the source/firewall; immutable to LLMs."""

    QUARANTINED = 0
    UNVERIFIED = 1
    TRUSTED_SOURCE = 2
    CURATED = 3
    OWNER = 4


class Status(str, Enum):
    PROPOSED = "proposed"
    ACTIVE = "active"
    QUARANTINED = "quarantined"
    SUPERSEDED = "superseded"
    INVALIDATED = "invalidated"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class Scope:
    """Isolation key present on every row and every query (ADR-0003).

    A coarse-to-fine triple. ``key()`` is the canonical string used by adapters
    to partition data; cross-scope reads are forbidden by the adapter contract.
    """

    tenant: str = "default"
    project: str = "default"
    agent: str = "default"

    def __post_init__(self) -> None:
        for part in (self.tenant, self.project, self.agent):
            if not part or "/" in part or "\x00" in part:
                raise ValueError("scope parts must be non-empty and contain no '/' or NUL")

    def key(self) -> str:
        return f"{self.tenant}/{self.project}/{self.agent}"


@dataclass(frozen=True)
class Provenance:
    """Where a record came from. ``source_uri`` is required (NOT NULL)."""

    source_uri: str
    adapter_name: str = "unknown"
    adapter_version: str = "0"
    ingest_run_id: Optional[str] = None
    source_file: Optional[str] = None
    chunk_index: Optional[int] = None
    derived_from: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.source_uri:
            raise ValueError("provenance.source_uri is required")


@dataclass
class MemoryRecord:
    """A single unit of memory. Construct via :meth:`create` (computes id/hash)."""

    id: str
    scope: Scope
    kind: Kind
    content: str
    content_hash: str
    provenance: Provenance
    trust_tier: TrustTier
    human_id: Optional[str] = None
    source_id: Optional[str] = None
    embedding: Optional[tuple[float, ...]] = None
    embedder_name: Optional[str] = None
    embedder_dim: Optional[int] = None
    created_at: datetime = field(default_factory=_utcnow)
    seen_at: datetime = field(default_factory=_utcnow)
    valid_from: Optional[datetime] = None
    valid_until: Optional[datetime] = None
    proof_count: int = 0
    declared_transformations: tuple[str, ...] = ()
    privacy_class: str = "unknown"
    status: Status = Status.ACTIVE
    metadata: Mapping[str, Scalar] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, Kind):
            self.kind = Kind(self.kind)
        if not isinstance(self.trust_tier, TrustTier):
            self.trust_tier = TrustTier(int(self.trust_tier))
        if not isinstance(self.status, Status):
            self.status = Status(self.status)
        if self.trust_tier <= TrustTier.QUARANTINED and self.status is Status.ACTIVE:
            # Quarantine-level trust must never surface. An ACTIVE status at
            # QUARANTINED trust made the read-path filters diverge: the
            # envelope's trust floor dropped the record while the surfacing
            # filter (status-only) let it reach the raw accessors
            # (.texts()/.ids()/.records()). Rewriting at construction makes
            # the divergent state unrepresentable — for records minted via the
            # public API AND rows reconstructed by adapters. Other lifecycle
            # states (superseded/invalidated/...) are preserved.
            self.status = Status.QUARANTINED
        if not self.content:
            raise ValueError("content must be non-empty")
        if self.embedding is not None:
            self.embedding = tuple(float(x) for x in self.embedding)
        _validate_metadata(self.metadata)

    @classmethod
    def create(
        cls,
        *,
        scope: Scope,
        kind: Kind,
        content: str,
        provenance: Provenance,
        trust_tier: TrustTier,
        **kwargs: object,
    ) -> "MemoryRecord":
        kind = Kind(kind)  # coerce BEFORE addressing: kind is part of the id (ADR-0026)
        chash = _content_hash(content)
        rid = record_id(scope.key(), provenance.source_uri, kind.value, chash)
        return cls(
            id=rid,
            scope=scope,
            kind=kind,
            content=content,
            content_hash=chash,
            provenance=provenance,
            trust_tier=trust_tier,
            **kwargs,  # type: ignore[arg-type]
        )

    def verify(self) -> bool:
        """True iff the stored content_hash matches the content (tamper check)."""
        return self.content_hash == _content_hash(self.content)

    def with_embedding(self, vector: Sequence[float], *, name: str, dim: int) -> "MemoryRecord":
        self.embedding = tuple(float(x) for x in vector)
        self.embedder_name = name
        self.embedder_dim = dim
        return self


def _validate_metadata(metadata: Mapping[str, Scalar]) -> None:
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise TypeError("metadata keys must be strings")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError(
                f"metadata['{key}'] must be a flat scalar (str/int/float/bool/None); "
                f"got {type(value).__name__}. Nested/unbounded structures are forbidden (ADR-0001)."
            )
