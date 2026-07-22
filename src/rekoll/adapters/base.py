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
from ..model import Kind, MemoryRecord, Scope, Status, TrustTier

__all__ = [
    "StorageAdapter",
    "QueryHit",
    "QueryResult",
    "GetResult",
    "BoardSnapshot",
    "UnsupportedCapabilityError",
    "CAP_VECTOR",
    "CAP_LEXICAL",
    "CAP_RELATIONAL",
    "CAP_VECTOR_INDEX",
    "BOARD_LIMIT_CEILING",
    "BOARD_METADATA_KEY",
    "BOARD_TAG_MAJOR",
    "BOARD_TAG_PENDING",
    "BOARD_TRUST_FLOOR",
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


#: Hard ceiling on every live-project-board read limit (``recent_records`` /
#: ``board_entries`` and both ``board_snapshot`` legs) — an ADR-0018-style
#: resource bound. The board is a BOUNDED read by contract (ADR-0035 sells a
#: cheap, predictable payload); a runaway/hostile limit must not turn it into a
#: table scan any caller pays on every poll. Validation follows the
#: ``max_pinned_directives`` shape: negative and over-ceiling limits raise
#: ``ValueError`` loudly (never a silent clamp), and ``0`` disables that leg.
BOARD_LIMIT_CEILING = 50

#: The metadata key and values that make a record a CURATED (Tier-2) board
#: entry (ADR-0035). Membership is a metadata tag on purpose — the Kind
#: vocabulary is frozen (ADR-0004), and importance must stay orthogonal to
#: provenance-trust (ADR-0002), so neither a new kind nor a trust tier may
#: encode "this is a major". Defined once here so the reference adapter, the
#: payload builder, and the conformance suite never restate the strings.
BOARD_METADATA_KEY = "board"
BOARD_TAG_MAJOR = "major"
BOARD_TAG_PENDING = "pending"

#: The Tier-2 (curated leg + open-pending count) trust floor, as an int. This
#: IS ``firewall.BOARD_FLOOR``, spelled here via ``TrustTier`` because
#: ``firewall`` imports this module (importing it back would cycle). Every
#: STORAGE-SIDE Tier-2 floor reads this name — the contract defaults below and
#: the reference adapter's ``board_snapshot`` internals alike — so a sideways
#: edit at one call site is no longer possible. That leaves exactly ONE
#: restatement in the codebase (this constant vs ``firewall.BOARD_FLOOR``), and
#: ``test_every_tier2_floor_reads_the_one_shared_constant`` pins it, including a
#: behavioral check that the adapter internals really do read this name.
BOARD_TRUST_FLOOR: int = int(TrustTier.TRUSTED_SOURCE)


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


@dataclass(frozen=True)
class BoardSnapshot:
    """One consistent read of the live project board (ADR-0035).

    ``majors`` is the curated Tier-2 leg (records tagged ``board=major`` or
    ``board=pending`` at/above the board floor, oldest first); ``recent`` is the
    Tier-1 activity feed (effective-active records, newest first);
    ``pending_open`` counts the ``board=pending`` rows passing the Tier-2 gates
    (a full count, not capped by the legs' limits). Adapters must produce all
    three from ONE read snapshot so a concurrent writer can never yield tiers
    that contradict each other (a torn board).
    """

    majors: tuple[MemoryRecord, ...]
    recent: tuple[MemoryRecord, ...]
    pending_open: int


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

    # --- optional: live project board (ADR-0035) ---------------------------
    def recent_records(
        self, *, scope: Scope, limit: int = 10, min_trust: int = int(TrustTier.UNVERIFIED)
    ) -> GetResult:
        """Tier 1 of the live project board: the EFFECTIVE-ACTIVE records in
        ``scope`` at ``trust_tier >= min_trust``, newest first (``created_at``
        DESC, ``id`` DESC as the tiebreak), capped at ``limit``.

        This is deliberately NOT :meth:`newest`: ``newest`` enumerates by
        recency with no status or trust gate (``health()``/``reindex()`` depend
        on seeing every row, including quarantined ones), so a forged raw
        ``status='active'`` row at trust 0 — effectively quarantined — comes
        back from it. A board that every concurrent session replays must gate on
        the EFFECTIVE status exactly like every other surfaced read leg, and
        label trust, which is why this is its own contract.

        The default floor is ``UNVERIFIED`` (owner decision, ADR-0035): the feed
        shows ALL effective-active writes, trust-labeled, so a session sees what
        its peers did; the *text* amplification gate lives higher up (the
        payload builder nulls excerpts below ``firewall.BOARD_FLOOR``).

        Limits follow the ``max_pinned_directives`` shape: ``0`` disables the
        leg (empty result, no read), negative or over
        ``BOARD_LIMIT_CEILING`` raises ``ValueError`` — never a silent clamp.
        Optional like :meth:`lexical_query` / :meth:`newest`: a backend that
        cannot serve it raises ``UnsupportedCapabilityError``.
        """
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support the live project board "
            "(recent_records enumeration)"
        )

    def board_entries(
        self,
        *,
        scope: Scope,
        limit: int = 10,
        min_trust: int = BOARD_TRUST_FLOOR,
    ) -> GetResult:
        """Tier 2 of the live project board: the CURATED records in ``scope`` —
        metadata ``board`` in {``major``, ``pending``} (``BOARD_METADATA_KEY`` /
        ``BOARD_TAG_*``) — that are effective-active at ``trust_tier >=
        min_trust``, OLDEST first (``created_at`` ASC, ``id`` ASC), capped at
        ``limit``.

        Oldest-first is the ADR-0034 §4 rationale verbatim: under the cap the
        FOUNDATIONAL items survive, and appending a new major never disturbs the
        rendered prefix, so a host's prompt cache stays warm as the board grows.

        The default floor is the board floor policy (``BOARD_TRUST_FLOOR``, the
        one name every Tier-2 floor in the codebase reads; it equals
        ``firewall.BOARD_FLOOR``, restated there only because ``firewall``
        imports this module — a test pins the two equal): a tag is data any
        writer can attach, so curated status = tag AND trust floor, never the
        tag alone. Scope isolation is on the RECORD row —
        ``record_metadata`` carries no scope column, so implementations must
        gate scope/status/trust on the kind-table side (a metadata-first read
        would leak tags across scopes).

        Same limit validation and optionality as :meth:`recent_records`.
        """
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support the live project board "
            "(board_entries enumeration)"
        )

    def board_snapshot(
        self,
        *,
        scope: Scope,
        recent_limit: int = 10,
        major_limit: int = 10,
        min_trust: int = int(TrustTier.UNVERIFIED),
    ) -> BoardSnapshot:
        """Both board tiers AND the open-pending count from ONE read snapshot.

        Implementations MUST produce ``majors`` (the :meth:`board_entries` leg,
        capped at ``major_limit``), ``recent`` (the :meth:`recent_records` leg,
        capped at ``recent_limit``), and ``pending_open`` (the FULL count of
        ``board=pending`` rows passing the Tier-2 gates) inside one read
        transaction, so a concurrent writer can never produce a torn snapshot —
        tiers that contradict each other or a count that disagrees with the
        entries it summarizes.

        ``min_trust`` gates the Tier-1 ``recent`` leg only (its default is the
        Tier-1 floor, UNVERIFIED). The curated leg and ``pending_open`` always
        apply the Tier-2 board floor policy (``firewall.BOARD_FLOOR``): the
        Tier-2 floor is an owner-locked policy, not a per-read preference.

        Same limit validation (each leg independently; ``0`` disables that leg)
        and optionality as :meth:`recent_records`.
        """
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support the live project board "
            "(board_snapshot)"
        )

    def set_status(self, *, scope: Scope, record_id: str, status: str) -> bool:
        """Atomically transition one EFFECTIVE-ACTIVE record's ``status`` to
        ``status``; return whether a row actually transitioned.

        This is the board's resolve verb (ADR-0035) and the first implemented
        slice of ADR-0025's lifecycle: it MARKS a record (typically ACTIVE →
        ``superseded``), it never evicts bytes. The gate is the record's
        EFFECTIVE status (the ``_effective_status`` rule): only an
        effective-active row may transition, so a quarantined, forged
        (raw-active-at-trust-0), proposed, or already-transitioned row is left
        untouched and the call reports ``False``. Because the gate rejects
        everything non-active, this verb can never RESURRECT a superseded or
        quarantined record either.

        Adapters MUST implement the gate IN the update statement — the
        :meth:`bump_proof_count` concurrency pattern — so two racing callers
        yield exactly one transition and no read-modify-write window. ``status``
        is data here (a valid ``Status`` value; garbage raises ``ValueError``);
        policy about WHICH transitions a product verb allows belongs to the
        facade, not the storage contract. Optional like the other board reads.
        """
        raise UnsupportedCapabilityError(
            f"adapter '{self.name}' does not support the live project board "
            "(set_status)"
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
