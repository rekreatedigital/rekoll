"""The process-local "was-it-used" recall ledger.

Closing the loop between *recalling* a memory and that memory actually
*informing* an action is the signal no major memory layer measures: retrieval
metrics say "we surfaced it", the ledger lets the host say "we acted on it".

How the loop closes:

1. Every ``Memory.recall`` records WHICH record ids it surfaced here
   (``RecallLedger.record``), tagged with the query and an optional ``call_id``.
2. When the host finishes an action (a tool call, a reply, a commit), it asks
   ``Memory.informed_by(call_id=...)`` for the recalls that plausibly fed that
   action and attaches them to its own receipt/log — or, when it knows exactly
   which memories mattered, calls ``Memory.mark_used(*ids)`` directly.
3. ``mark_used`` increments ``MemoryRecord.proof_count`` — the promotion-only
   usage signal future consolidation/forgetting consumes (ADR-0016 design
   note): usage may only EXTEND a memory's standing, never shorten another's.

Discipline (ported from a production memory system):
 - Process-local, in-memory ring buffer: capped with a TTL. No persistence —
   the durable usage record is ``proof_count`` on the record itself.
 - Best-effort everywhere: ``record`` and ``entries`` swallow all errors.
   A ledger failure must never break a recall read or a host's receipt write.
 - Zero dependencies, zero I/O, thread-safe.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Optional, Sequence

__all__ = ["RecallLedger", "LedgerEntry"]

#: Ring-buffer cap — far more recalls than one agent session plausibly produces.
DEFAULT_MAX_ENTRIES = 50
#: Entry TTL — a recall older than a long session can't plausibly have informed
#: the current action.
DEFAULT_TTL_SECONDS = 2 * 60 * 60
#: How many entries a single receipt may claim when it carries no call_id
#: (the "most recent N within TTL" session-scope fallback).
DEFAULT_RECENT_LIMIT = 5

_MAX_IDS_PER_ENTRY = 20
_MAX_QUERY_CHARS = 200


class LedgerEntry(dict):
    """A slim, JSON-safe view of one recall: {ts, call_id, query, ids}."""


class RecallLedger:
    """Ring buffer of recent recalls: which ids each recall surfaced, and when.

    Instance-based (each ``Memory`` owns one) rather than module-global, so two
    Memory objects in one process never cross-contaminate usage attribution.
    """

    def __init__(
        self,
        *,
        max_entries: int = DEFAULT_MAX_ENTRIES,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
    ) -> None:
        if max_entries <= 0:
            raise ValueError("max_entries must be positive")
        if ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be positive")
        self.ttl_seconds = float(ttl_seconds)
        self._lock = threading.Lock()
        self._entries: deque = deque(maxlen=max_entries)

    def record(
        self,
        ids: Sequence[str],
        *,
        query: Optional[str] = None,
        call_id: Optional[str] = None,
        ts: Optional[float] = None,
    ) -> None:
        """Record one recall's surfaced record ids. Best-effort — never raises.

        ``call_id`` scopes the entry to one host action/conversation turn when
        present; None means process/session scope. ``ts`` is injectable for
        tests (defaults to now).
        """
        try:
            if isinstance(ids, (str, bytes)):
                ids = [ids]  # a lone id, not a sequence of characters
            clean_ids = [str(i) for i in (ids or []) if i][:_MAX_IDS_PER_ENTRY]
            if not clean_ids:
                return
            entry = {
                "ts": float(ts if ts is not None else time.time()),
                "call_id": str(call_id) if call_id else None,
                "query": str(query)[:_MAX_QUERY_CHARS] if query else None,
                "ids": clean_ids,
            }
            with self._lock:
                self._entries.append(entry)
        except Exception:
            # A ledger failure must never break the recall that fed it.
            return

    def entries(
        self,
        call_id: Optional[str] = None,
        *,
        limit: int = DEFAULT_RECENT_LIMIT,
        now: Optional[float] = None,
    ) -> list[LedgerEntry]:
        """The recalls that plausibly informed an action being receipted now.

        With a ``call_id``: ONLY entries recorded under that same call_id (no
        cross-call contamination — a stale recall from another conversation
        never gets credit). Without one: the most recent ``limit`` live entries.

        Expired entries are pruned as a side effect. Returns newest-first,
        JSON-safe entries (``ts`` as ISO-8601 UTC). Best-effort — returns []
        on any failure.
        """
        try:
            t_now = float(now if now is not None else time.time())
            with self._lock:
                live = [e for e in self._entries if (t_now - e["ts"]) <= self.ttl_seconds]
                self._entries.clear()
                self._entries.extend(live)
            if call_id:
                matched = [e for e in live if e.get("call_id") == str(call_id)]
            else:
                matched = list(live)
            matched.sort(key=lambda e: e["ts"], reverse=True)
            out: list[LedgerEntry] = []
            for e in matched[: max(1, int(limit))]:
                out.append(
                    LedgerEntry(
                        ts=datetime.fromtimestamp(e["ts"], tz=timezone.utc).isoformat(),
                        call_id=e["call_id"],
                        query=e["query"],
                        ids=list(e["ids"]),
                    )
                )
            return out
        except Exception:
            return []

    def clear(self) -> None:
        """Drop all entries (tests / explicit reset)."""
        with self._lock:
            self._entries.clear()

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)
