"""The live-project-board payload builder (ADR-0035).

ONE function every door (Lane B's ``Memory`` facade method, then the CLI and
MCP surfaces) will call, so the board a session sees is byte-identical no
matter which door served it — the ADR-0031 cross-door-parity discipline,
applied from day one instead of retrofitted.

Deliberately a LEAF module: it imports the adapter contract, the firewall, and
the model — never ``Memory``, an embedder, or the ledger. The board is a plain,
scoped, zero-LLM, zero-embedding read (ADR-0007), and the payload is a PURE
FUNCTION of stored rows: no clock, no randomness, fixed key order — two calls
with no intervening commit ``json.dumps`` byte-identically, so hosts can cheap
byte-compare payloads to detect change.
"""

from __future__ import annotations

import warnings
from typing import Optional

from .adapters.base import (
    BOARD_LIMIT_CEILING,
    BOARD_METADATA_KEY,
    BOARD_TAG_MAJOR,
    BOARD_TAG_PENDING,
    StorageAdapter,
)
from .firewall import BOARD_FLOOR, DIRECTIVE_FLOOR, _neutralize_delimiters
from .model import MemoryRecord, Scope, TrustTier

__all__ = [
    "build_board_payload",
    "BOARD_TEXT_MAX_CHARS",
    "DEFAULT_BOARD_RULES_LIMIT",
]

#: Cap on a board entry's one-line text excerpt. The board rides ahead of every
#: session's real work, so its worst case must stay small and predictable
#: (ADR-0035 states the resulting payload bound numerically).
BOARD_TEXT_MAX_CHARS = 200

#: Default cap on the board's rules leg — deliberately the SAME number as
#: ``rekoll.memory.DEFAULT_MAX_PINNED_DIRECTIVES`` (5), because the board's
#: rules and recall's pinned directives are the same records by construction
#: (same read, same floor, same order, same cap ⇒ same five). Restated here
#: rather than imported because this module must never import ``memory`` (a
#: test pins the two numbers equal so they cannot drift). 0 disables the leg,
#: like every other cap.
DEFAULT_BOARD_RULES_LIMIT = 5


def _entry(record: MemoryRecord) -> dict:
    """One board entry — CONSTANT key set, every key always present.

    ``trust`` uses the ``trust_tier.name.lower()`` spelling the MCP ``remember``
    response and the CLI's human recall line already use. ``created_at`` is the
    STORED timestamp verbatim (its canonical ISO-8601 serialization) — never a
    computed age, never a read-time clock. ``board`` is normalized to the fixed
    vocabulary ``"major" | "pending" | null`` (an unknown tag value reads as
    untagged rather than leaking new vocabulary into the payload).

    ``text`` is TRUST-GATED: at ``trust_tier >= BOARD_FLOOR`` it is the first
    line of the content pushed through the firewall's delimiter neutralizer
    (so a stored string cannot forge envelope headers or role tags into every
    concurrent session), capped at ``BOARD_TEXT_MAX_CHARS``. BELOW the floor it
    is null — the entry still appears (id/kind/trust/created_at awareness), but
    the board never amplifies untrusted text to every session. The key set does
    not vary with trust (``text: string | null``).
    """
    board = record.metadata.get(BOARD_METADATA_KEY)
    if board not in (BOARD_TAG_MAJOR, BOARD_TAG_PENDING):
        board = None
    if int(record.trust_tier) >= int(BOARD_FLOOR):
        # Neutralize FIRST, then take the first line: the neutralizer converts
        # vertical separators (U+2028/U+2029, lone CR) to '\n', so splitting
        # before it could keep a "line" that still contains hidden breaks.
        text: Optional[str] = _neutralize_delimiters(record.content).split("\n", 1)[0][
            :BOARD_TEXT_MAX_CHARS
        ]
    else:
        text = None
    return {
        "id": record.id,
        "kind": record.kind.value,
        "trust": record.trust_tier.name.lower(),
        "created_at": record.created_at.isoformat() if record.created_at else None,
        "board": board,
        "text": text,
    }


def build_board_payload(
    adapter: StorageAdapter,
    scope: Scope,
    *,
    recent_limit: int = 10,
    major_limit: int = 10,
    rules_limit: int = DEFAULT_BOARD_RULES_LIMIT,
    min_trust: int = int(TrustTier.UNVERIFIED),
) -> dict:
    """Build the board payload: ``{"rules", "majors", "recent", "pending_open",
    "latest"}`` — the CONSTANT key set, all keys always present.

    * ``rules`` — the standing directives, via ``adapter.active_directives``
      with the SAME floor (``DIRECTIVE_FLOOR``), order (oldest-first, from the
      adapter contract), cap, tamper-verification, and fail-soft discipline as
      ``Memory._pinned_directives``: the board's rules and recall's pinned
      directives are the same records by construction. Neutralized full-text
      strings, exactly the shape of ``RecallResult.directives()``. An adapter
      without the capability (or any read error) degrades this leg to ``[]`` —
      a board without rules beats no board, mirroring recall's degradation.
    * ``majors`` / ``recent`` — entry dicts (see :func:`_entry`); the curated
      leg oldest-first, the activity feed newest-first, both from ONE adapter
      read snapshot (``board_snapshot``) so the tiers cannot contradict each
      other. ``min_trust`` gates the recent leg only (owner default:
      UNVERIFIED, trust-labeled); the curated leg is floored at ``BOARD_FLOOR``
      by the adapter contract.
    * ``pending_open`` — the FULL count of open ``board=pending`` items passing
      the Tier-2 gates (not capped by ``major_limit``).
    * ``latest`` — the max stored ``created_at`` across the rows returned in
      ``majors``/``recent`` (else null): a cheap freshness hint a consumer can
      re-derive from the entries. It does NOT move on resolves/forgets — the
      authoritative change check is byte-comparing the payload (ADR-0035).

    Every surfaced record (both tiers AND rules) is content-hash verified
    (ADR-0019); a mismatch is WITHHELD with ONE ``UserWarning`` naming the
    withheld ids — a tampered row must not fan out to every concurrent session.

    Unsupported storage (no ``board_snapshot``) raises
    ``UnsupportedCapabilityError`` honestly — without the storage capability
    there is no board to degrade to; the facade (Lane B) owns any softer
    policy. Limits are validated at every layer to the same rule: 0 disables a
    leg, negative or over ``BOARD_LIMIT_CEILING`` raises ``ValueError``.
    """
    rules_limit = int(rules_limit)
    if rules_limit < 0 or rules_limit > BOARD_LIMIT_CEILING:
        raise ValueError(
            f"rules_limit must be between 0 and {BOARD_LIMIT_CEILING} "
            f"(0 disables the rules leg), got {rules_limit!r}"
        )
    snapshot = adapter.board_snapshot(
        scope=scope,
        recent_limit=recent_limit,
        major_limit=major_limit,
        min_trust=min_trust,
    )

    tampered: list[str] = []

    rules: list[str] = []
    if rules_limit > 0:
        try:
            rule_records = adapter.active_directives(
                scope=scope, limit=rules_limit, min_trust=int(DIRECTIVE_FLOOR)
            ).records
        except Exception:
            rule_records = ()  # unsupported adapter or read error: rules degrade
        for record in rule_records:
            if record.verify():
                rules.append(_neutralize_delimiters(record.content))
            else:
                tampered.append(record.id)

    majors: list[dict] = []
    recent: list[dict] = []
    latest: Optional[str] = None
    for records, leg in ((snapshot.majors, majors), (snapshot.recent, recent)):
        for record in records:
            if not record.verify():
                tampered.append(record.id)
                continue
            entry = _entry(record)
            leg.append(entry)
            created = entry["created_at"]
            if created is not None and (latest is None or created > latest):
                latest = created  # ISO-8601 UTC: lexicographic == chronological

    if tampered:
        warnings.warn(
            f"[rekoll] {len(tampered)} board record(s) failed content-hash "
            "verification and were withheld from the board payload (possible "
            "direct-DB tampering; re-ingest or delete them): "
            f"{', '.join(sorted(set(tampered)))}",
            stacklevel=2,
        )

    return {
        "rules": rules,
        "majors": majors,
        "recent": recent,
        "pending_open": snapshot.pending_open,
        "latest": latest,
    }
