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

import unicodedata
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
    "RECALLABLE_STATUSES",
    "Scope",
    "Provenance",
    "MemoryRecord",
    "DeferredEmbedding",
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


#: The ONE definition of "recallable", shared by every read surface (the
#: hybrid_search surfacing filter and the MCP status count): only ACTIVE
#: records are memories a read may return. Proposed/superseded/invalidated are
#: lifecycle states, and quarantined never surfaces anywhere. Kept in one place
#: so a future supersede/propose loop cannot make the surfaces disagree.
RECALLABLE_STATUSES: frozenset = frozenset({Status.ACTIVE})


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
            try:
                part.encode("utf-8")
            except UnicodeEncodeError:
                # A lone UTF-16 surrogate (e.g. '\ud800') passes the checks above
                # but is not UTF-8 encodable — it used to construct fine and then
                # crash EVERY adapter call with a deferred UnicodeEncodeError when
                # scope.key() bound to SQLite. Fail loudly at construction instead.
                raise ValueError(
                    "scope parts must be UTF-8 encodable (no lone surrogates)"
                ) from None

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
        # Require at least one VISIBLE char. `not self.source_uri` misses '   ',
        # and `.strip()` misses zero-width / format (Cf) / control (Cc) strings
        # (e.g. '​​', which sanitize_unicode reduces to '') — all of
        # which are truthy but carry no provenance and round-tripped as a blank
        # origin through the public facade.
        if not any(
            not ch.isspace() and unicodedata.category(ch) not in ("Cf", "Cc", "Cn")
            for ch in self.source_uri
        ):
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
        # NB: `embedding` is coerced/validated by its property setter (below),
        # which the generated __init__ has already run by the time we get here.
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
        # Drop lone surrogates (Cs): they are invalid UTF-8 and would crash the
        # SQLite write / embedder / hash with a deferred UnicodeEncodeError. This
        # is the universal content choke point, so it covers the screen=False path
        # too (the firewall strips them on the screened path). A no-op for all
        # valid content; normalize_content strips them for the hash regardless, so
        # the stored content and its content_hash stay consistent.
        if any(unicodedata.category(ch) == "Cs" for ch in content):
            content = "".join(ch for ch in content if unicodedata.category(ch) != "Cs")
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

    # -- pickling -----------------------------------------------------------
    # A deferred vector (see DeferredEmbedding below) is a closure, and a closure
    # cannot be pickled — so a record read from a store and never materialized
    # would have become unpicklable, breaking multiprocessing/caching callers for
    # a change that is supposed to be invisible. These two hooks keep the wire
    # format EXACTLY what it was before deferral existed: the vector is decoded
    # on the way out, and it travels under its public field name, so pickles
    # round-trip in both directions across this change.
    def __getstate__(self) -> dict:
        materialized = self.embedding  # decode now; a thunk must not cross a process
        state = dict(self.__dict__)
        state.pop("_embedding", None)
        state["embedding"] = materialized
        return state

    def __setstate__(self, state: Mapping) -> None:
        state = dict(state)
        # Accept either key, and always end up with exactly one: ``embedding``
        # is what __getstate__ writes AND what a pre-deferral pickle carries;
        # ``_embedding`` is what a raw __dict__ copy would carry. Setting the
        # slot unconditionally means the property's getter can never meet a
        # half-restored record.
        state["_embedding"] = state.pop("embedding", state.pop("_embedding", None))
        self.__dict__.update(state)

    def with_embedding(self, vector: Sequence[float], *, name: str, dim: int) -> "MemoryRecord":
        self.embedding = vector  # the property setter coerces to a float tuple
        self.embedder_name = name
        self.embedder_dim = dim
        return self


# -- deferred embeddings (issue #43) ------------------------------------------
# Storage adapters reconstruct a MemoryRecord for every candidate in the
# retrieval fusion pool — ~96 records for a k=8 recall, because the pool is
# fetched from BOTH the vector and lexical legs at `candidates` (6*k) deep. RRF
# fusion ranks on `id` and `score` and never touches `.embedding`, so ~88 of
# those stored vectors were json-decoded, validated and boxed only to be ranked
# away and discarded — about half of what remained in read latency once the
# ADR-0030 scan cache removed the scan itself.
#
# The fix is to defer, not to omit. `.embedding` MUST keep returning exactly the
# same value (a `tuple[float, ...]`) and raising exactly the same errors for
# every caller, because the pool records are also the records a caller receives
# from `recall()`. Handing back `embedding=None` for the pool would have made
# `recall(...).records()[0].embedding` a silent lie; deferring costs a caller
# who reads it nothing but the decode they were already paying for.
#
# An adapter passes `DeferredEmbedding(thunk)` in place of a vector; the first
# read of `.embedding` calls the thunk, caches the result and returns it. A
# vector nobody reads is never decoded at all.


class DeferredEmbedding:
    """A stored vector that has not been decoded yet (adapter read path).

    ``thunk`` must return the finished ``tuple[float, ...]`` — already decoded
    AND already validated by whatever path the adapter guarantees. The setter
    below deliberately does NOT re-coerce a deferred result: ``json.loads``
    already yields Python floats, so the element-by-element re-coercion the
    eager path performs is pure waste on the adapter path (the tail observation
    in issue #43). Adapters own that guarantee; everyone else gets coerced.
    """

    __slots__ = ("thunk",)

    def __init__(self, thunk) -> None:
        self.thunk = thunk


def _coerce_embedding(value):
    """Today's eager coercion, unchanged: a float tuple, or a clean error."""
    if value is None:
        return None
    return tuple(float(x) for x in value)


def _embedding_get(self: MemoryRecord):
    stored = self.__dict__["_embedding"]
    if type(stored) is DeferredEmbedding:
        stored = stored.thunk()
        self.__dict__["_embedding"] = stored  # decode once per record, not per read
    return stored


def _embedding_set(self: MemoryRecord, value) -> None:
    self.__dict__["_embedding"] = (
        value if type(value) is DeferredEmbedding else _coerce_embedding(value)
    )


#: Installed AFTER @dataclass has run, on purpose. `embedding` must stay a real
#: dataclass FIELD — it is a documented constructor keyword and it participates
#: in the generated __repr__/__eq__ — but it also needs to be a data descriptor
#: so that lookups reach the lazy getter instead of the instance __dict__.
#: Declaring a property inside the class body would instead make it the field's
#: *default* (dataclasses' descriptor-typed-field behaviour) and hand the
#: property object itself to the setter. Assigning here keeps the field, the
#: __init__ signature and its `None` default exactly as they were, and every
#: generated method routes through the property because a data descriptor on the
#: type takes precedence over the instance dict. Nothing else in the codebase
#: reads `record.__dict__` directly.
MemoryRecord.embedding = property(  # type: ignore[assignment]
    _embedding_get,
    _embedding_set,
    doc="The record's vector, or None. Decoded on first read if an adapter deferred it.",
)


def _validate_metadata(metadata: Mapping[str, Scalar]) -> None:
    for key, value in metadata.items():
        if not isinstance(key, str):
            raise TypeError("metadata keys must be strings")
        if not isinstance(value, (str, int, float, bool, type(None))):
            raise TypeError(
                f"metadata['{key}'] must be a flat scalar (str/int/float/bool/None); "
                f"got {type(value).__name__}. Nested/unbounded structures are forbidden (ADR-0001)."
            )
