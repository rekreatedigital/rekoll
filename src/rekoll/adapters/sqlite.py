"""Reference storage adapter: local SQLite, zero-config, no daemon, no key.

This is the default backend and the executable example of the canonical schema:
 - SEPARATE physical tables per kind (verbatim_records / observations /
   directives / episodes) — ADR-0001, deliberately not one table + a type column.
 - Flat-scalar metadata and typed links live in BOUNDED CHILD TABLES
   (record_metadata, record_links) — never an unbounded JSON blob.
 - Per-scope embedder identity is persisted for the guard.

Vector search is an EXACT full scan over stored vectors — no index, no ANN, so
the foundation runs with zero native/ML dependencies and top-k is always exact.
The scan is made cheap rather than made approximate (ADR-0030): decoded vectors
and their norms are cached per (table, scope) and invalidated on write, and the
scoring inner loop is vectorized with numpy *only if numpy is already loaded*.
The zero-dependency pure-Python path remains the default and is bit-exact.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import sys
from array import array
from contextlib import contextmanager
from datetime import datetime
from functools import reduce
from operator import add, mul
from typing import Mapping, Optional, Sequence

from ..embedding import EmbedderIdentity
from ..model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from .base import (
    BOARD_LIMIT_CEILING,
    BOARD_METADATA_KEY,
    BOARD_TAG_MAJOR,
    BOARD_TAG_PENDING,
    CAP_LEXICAL,
    CAP_VECTOR,
    BoardSnapshot,
    GetResult,
    QueryHit,
    QueryResult,
    StorageAdapter,
)

_KIND_TABLE = {
    Kind.RAW_FACT: "verbatim_records",
    Kind.OBSERVATION: "observations",
    Kind.DIRECTIVE: "directives",
    Kind.EPISODE: "episodes",
}

_ALLOWED_WHERE_KEYS = {"status", "min_trust"}


def _validate_where(
    where: Optional[Mapping[str, object]]
) -> tuple[object, Optional[int]]:
    """Validate a query ``where`` filter and coerce ``min_trust`` to int at the door.

    A read must not crash on a caller's bad filter value. ``min_trust`` that is not
    int-coercible — a str, NaN, inf, or a list — used to raise a raw
    ValueError/OverflowError/TypeError from deep inside the vector scan
    (unconditionally) or mid-row in the lexical leg (data-dependent). Surface ONE
    clear ValueError at the entry instead. Returns ``(status_filter, min_trust)``.
    """
    if not where:
        return None, None
    if not isinstance(where, Mapping):
        # A list/tuple/set of key names passes the ``set(where) - allowed`` check
        # (its ELEMENTS are the allowed keys) then crashes on ``where.get(...)``
        # with an uncaught AttributeError. Reject non-Mapping filters cleanly.
        raise ValueError(f"where must be a mapping, got {type(where).__name__}")
    bad = set(where) - _ALLOWED_WHERE_KEYS
    if bad:
        raise ValueError(
            f"unsupported where keys {sorted(bad)}; "
            f"allowed: {sorted(_ALLOWED_WHERE_KEYS)}"
        )
    raw = where.get("min_trust")
    if raw is None:
        min_trust: Optional[int] = None
    else:
        try:
            min_trust = int(raw)  # bool/valid-float ok; str/NaN/inf/list -> raise
        except (TypeError, ValueError, OverflowError):
            raise ValueError(
                f"where['min_trust'] must be an int-coercible trust level, got {raw!r}"
            ) from None
    return where.get("status"), min_trust


def _effective_status(status: str, trust: int) -> str:
    """The status ``MemoryRecord`` reports for a stored ``(status, trust)`` pair.

    Quarantine-level trust forces ``status='quarantined'`` at construction
    (``model.MemoryRecord.__post_init__``): an ACTIVE row at QUARANTINED trust
    must never surface. A stored row whose status column still says ``active`` at
    trust 0 — written by an older Rekoll, by a caller that mutated ``.status``
    after ``create()``, or by hand — has the SAME effective status and must gate
    identically. This is the ONE Python definition of that rule, shared by every
    raw-column read gate: the vector scan (pure-Python + numpy legs), its live
    winner re-check, and the lexical ``_filter_columns``. ``_EFFECTIVE_STATUS_SQL``
    is the SQL twin used by ``count`` / ``bump_proof_count``.

    Reading the raw column instead let a forged row surface through a
    ``status='active'`` filter AND hide from a ``status='quarantined'`` audit —
    the divergence with the lexical leg (which was already effective-status) that
    this closes. It deliberately makes the vector and lexical legs bit-identical
    for forged rows and, as a consequence, breaks vector/lexical bit-equivalence
    ONLY for those forged rows; every row minted through the model is unaffected
    (the model already normalized it), so the scan-equivalence oracle — which
    never forges — stays green.
    """
    if trust <= int(TrustTier.QUARANTINED) and status == Status.ACTIVE.value:
        return Status.QUARANTINED.value
    return status


#: ``_effective_status`` as a SQL scalar over the raw ``(trust_tier, status)``
#: columns, so ``count`` / ``bump_proof_count`` gate on the EFFECTIVE status in a
#: single query rather than fetching + reconstructing every row. Bind the three
#: ``_EFFECTIVE_STATUS_SQL_PARAMS`` immediately before the value it is compared
#: to. Kept in lock-step with ``_effective_status`` above (a unit test pins the
#: Python and SQL forms equal across every (status, trust) pair).
_EFFECTIVE_STATUS_SQL = "CASE WHEN trust_tier <= ? AND status = ? THEN ? ELSE status END"
_EFFECTIVE_STATUS_SQL_PARAMS = (
    int(TrustTier.QUARANTINED),
    Status.ACTIVE.value,
    Status.QUARANTINED.value,
)

_INSERT_COLUMNS = (
    "id, human_id, scope_key, kind, content, content_hash, source_id, "
    "prov_source_uri, prov_adapter_name, prov_adapter_version, prov_ingest_run_id, "
    "prov_source_file, prov_chunk_index, trust_tier, embedding, embedder_name, "
    "embedder_dim, created_at, seen_at, valid_from, valid_until, proof_count, "
    "declared_transformations, privacy_class, status"
)


def _dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value is not None else None


def _parse_dt(value: Optional[str]) -> Optional[datetime]:
    return datetime.fromisoformat(value) if value else None


def _encode_scalar(value: object) -> tuple[str, Optional[str]]:
    if value is None:
        return ("none", None)
    if isinstance(value, bool):  # before int — bool is a subclass of int
        return ("bool", "1" if value else "0")
    if isinstance(value, int):
        return ("int", str(value))
    if isinstance(value, float):
        return ("float", repr(value))
    return ("str", str(value))


def _decode_scalar(vtype: str, value: Optional[str]):
    if vtype == "none":
        return None
    if vtype == "bool":
        return value == "1"
    if vtype == "int":
        return int(value)  # type: ignore[arg-type]
    if vtype == "float":
        return float(value)  # type: ignore[arg-type]
    return value


def _scope_from_key(key: str) -> Scope:
    tenant, project, agent = key.split("/", 2)
    return Scope(tenant=tenant, project=project, agent=agent)


def _validate_board_limit(name: str, value: int) -> int:
    """Validate a board-read limit at the door (ADR-0035 / ADR-0018 bound).

    ``0`` legitimately disables that leg; negative has no meaning and anything
    over ``BOARD_LIMIT_CEILING`` would break the board's bounded-read promise —
    both are refused loudly (the ``max_pinned_directives`` shape), never
    silently clamped.
    """
    limit = int(value)
    if limit < 0 or limit > BOARD_LIMIT_CEILING:
        raise ValueError(
            f"{name} must be between 0 and {BOARD_LIMIT_CEILING} "
            f"(0 disables that board leg), got {value!r}"
        )
    return limit


# Bound the MATCH expression: a hostile/runaway query must not inflate it
# without limit (ADR-0018). Past a few dozen distinct OR terms BM25 adds noise,
# not recall — 32 is far beyond any natural-language question.
_MAX_FTS_TERMS = 32


def _fts_query(text: str) -> Optional[str]:
    """Turn free text into a safe, bounded FTS5 MATCH expression.

    Terms are lowercased, quoted, de-duplicated (order-preserving — repeated
    words carry no extra intent), and capped at ``_MAX_FTS_TERMS``.
    """
    terms = re.findall(r"\w+", text.lower())
    if not terms:
        return None
    unique = list(dict.fromkeys(terms))[:_MAX_FTS_TERMS]
    return " OR ".join(f'"{t}"' for t in unique)


#: Upper bound on a stored embedding's dimension (ADR-0018 style resource bound).
#: No production text embedder exceeds a few thousand dims (OpenAI 3072, Voyage
#: 1536, large open models ~4096); 65536 is far beyond any real model yet still
#: bounds the O(dim) decode+norm cost of a tampered/corrupt cell.
_MAX_EMBEDDING_DIM = 65_536

VECTOR_BACKENDS = ("auto", "numpy", "python")

#: How many decoded vectors the scan cache may hold across all (table, scope)
#: entries before it starts evicting least-recently-scanned ones. Each cached
#: vector costs ``8 * dim`` bytes (an ``array('d')``, not a list of boxed
#: floats), so the default is ~150 MB at dim=384 — generous for a local-first
#: store, and BOUNDED, which an unbounded cache in a long-lived MCP server
#: serving many scopes would not be. A scope larger than the whole budget is
#: simply never cached: it re-decodes per query, exactly as the pre-ADR-0030
#: brute-force scan did. Correctness never depends on this number.
DEFAULT_VECTOR_CACHE_MAX_VECTORS = 50_000


def _norm(vec: Sequence[float]) -> float:
    """L2 norm accumulated in the SAME order as ``embedding.cosine``'s ``nb``
    loop, so the pure-Python score below stays bit-identical to ``cosine()``."""
    n = 0.0
    for y in vec:
        n += y * y
    return math.sqrt(n)


def _decode_embedding(raw: object) -> array:
    """Decode a stored embedding cell into an ``array('d')`` of finite floats, or
    raise ``ValueError`` if the cell is corrupt.

    Defense-in-depth for the READ path. ``MemoryRecord.verify`` hashes only the
    CONTENT (ADR-0019), so a tampered or corrupted embedding column is invisible
    to tamper-detection — yet ``json.loads`` accepts ``NaN``/``Infinity`` and
    ``array('d', ...)`` raises a raw ``TypeError`` on a non-numeric / nested
    value. Left unguarded, a valid-JSON-but-wrong-shape cell (``"garbage"``,
    ``[[1,2]]``) CRASHED ``Memory.recall`` with an uncaught ``TypeError``, and a
    ``NaN``/``Infinity`` cell silently poisoned the ADR-0028 abstain gate
    (``NaN < min_score`` is False → GATE_PASS, so withheld content SURFACED).

    Rekoll's contract is that a hand-edited/corrupt store fails VISIBLY rather
    than returning silent-wrong results (tests/test_cli.py
    ``test_recall_on_a_hand_edited_store_fails_cleanly``). So every corrupt shape
    is funnelled to ONE clean ``ValueError`` — the CLI already turns that into a
    clean exit 1, and an SDK caller gets a documented, catchable error instead of
    a raw ``TypeError`` or a NaN that defeats the safety gate.
    """
    try:
        decoded = json.loads(raw)
    except (ValueError, TypeError, RecursionError) as exc:
        # RecursionError: a deeply nested JSON array/object ("[[[[...]]]]") blows
        # the decoder's C stack — caught here so a tampered cell is one clean
        # ValueError, never an uncaught RecursionError up through Memory.recall.
        raise ValueError(f"corrupt embedding cell (not decodable JSON): {exc}") from None
    if not isinstance(decoded, (list, tuple)) or not decoded:
        raise ValueError("corrupt embedding cell: not a non-empty numeric array")
    if len(decoded) > _MAX_EMBEDDING_DIM:
        # A tampered cell with a huge dim (e.g. 5M floats) decodes in O(dim) time +
        # memory and, being one row, escapes the vector-COUNT cache budget. No real
        # embedder exceeds a few thousand dims; cap it as corrupt (defense in depth).
        raise ValueError(f"corrupt embedding cell: dim {len(decoded)} over cap {_MAX_EMBEDDING_DIM}")
    try:
        vec = array("d", decoded)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"corrupt embedding cell (non-numeric): {exc}") from None
    if not all(math.isfinite(x) for x in vec):
        raise ValueError("corrupt embedding cell: contains NaN or Infinity")
    # Reject a FINITE-but-overflowing vector: values so large their squares sum to
    # inf give a non-finite L2 norm and a NaN cosine downstream (which would defeat
    # the ADR-0028 abstain gate from finite inputs). Real embeddings are ~unit-norm;
    # this rejects only tamper, at the same O(dim) cost as the finiteness scan.
    if not math.isfinite(math.fsum(x * x for x in vec)):
        raise ValueError("corrupt embedding cell: magnitude overflows the L2 norm")
    return vec


class _CachedVector:
    __slots__ = ("status", "trust", "vec", "norm", "dim")

    def __init__(self, status: str, trust: int, vec: Sequence[float]) -> None:
        self.status = status
        self.trust = trust
        # array('d') holds raw C doubles: 8 bytes each instead of a list's
        # 8-byte pointer + 24-byte float object (4x smaller at dim=384). The
        # values are the identical doubles json.loads produced, so the scores
        # stay bit-exact.
        self.vec = vec if isinstance(vec, array) else array("d", vec)
        self.norm = _norm(self.vec)
        self.dim = len(self.vec)


class _ScanCache:
    """Decoded embeddings for one (table, scope_key), in ROWID ORDER.

    Holds only what the SCAN needs — id, status, trust_tier, vector, norm, dim.
    Never a whole record: ``vector_query`` re-reads the top-k rows from SQLite,
    so every column the caller actually sees is live, not cached.

    ``rows`` is an insertion-ordered dict, and that ordering IS the rowid
    ordering — maintained, not re-derived. The three mutations SQLite performs
    map exactly onto the three dict mutations:

      INSERT                 -> new rowid at the end   -> ``rows[id] = v``
      INSERT OR REPLACE      -> delete + insert, so the rowid moves to the end
                                -> ``rows.pop(id)`` then ``rows[id] = v``
      DELETE                 -> row gone               -> ``rows.pop(id)``

    That correspondence is what lets a write update the cache in place instead of
    dropping it, and it is asserted directly by the randomized order test in
    tests/test_vector_scan_equivalence.py.
    """

    __slots__ = ("data_version", "rows", "_mats")

    def __init__(self, data_version: int) -> None:
        self.data_version = data_version
        self.rows: dict[str, _CachedVector] = {}
        self._mats: dict[int, tuple] = {}

    def put(self, rid: str, entry: Optional[_CachedVector]) -> None:
        self._mats.clear()
        self.rows.pop(rid, None)  # replace => the row moves to the end
        if entry is not None:  # a row with no embedding simply leaves the scan
            self.rows[rid] = entry

    def drop(self, rid: str) -> None:
        if self.rows.pop(rid, None) is not None:
            self._mats.clear()

    def matrix_for_dim(self, np, dim: int):
        """(ids, matrix, norms) for the rows of dimension ``dim``, in rowid order.

        Grouped by dim because a mid-flight embedder swap leaves vectors of two
        widths in one scope; the scan skips the non-matching ones (it does not
        crash and does not truncate), so they must not enter the matmul.
        """
        cached = self._mats.get(dim)
        if cached is None:
            ids = [rid for rid, v in self.rows.items() if v.dim == dim]
            mat = np.array([self.rows[i].vec for i in ids], dtype=np.float64).reshape(len(ids), dim)
            norms = np.array([self.rows[i].norm for i in ids], dtype=np.float64)
            cached = (ids, mat, norms)
            self._mats[dim] = cached
        return cached


class SQLiteAdapter(StorageAdapter):
    name = "sqlite"
    capabilities = frozenset({CAP_VECTOR, CAP_LEXICAL})
    distance_metric = "cosine"

    def __init__(
        self,
        path: str = ":memory:",
        *,
        vector_backend: str = "auto",
        vector_cache_max_vectors: int = DEFAULT_VECTOR_CACHE_MAX_VECTORS,
    ) -> None:
        if vector_backend not in VECTOR_BACKENDS:
            raise ValueError(
                f"unknown vector_backend {vector_backend!r}; "
                f"expected one of {list(VECTOR_BACKENDS)}"
            )
        if vector_cache_max_vectors < 0:
            raise ValueError("vector_cache_max_vectors must be >= 0 (0 disables the cache)")
        self.path = path
        self.vector_backend = vector_backend
        self.vector_cache_max_vectors = vector_cache_max_vectors
        if vector_backend == "numpy":
            # Fail here, not on the first read. Asking for the vectorized backend
            # on a box without numpy is a configuration error, and a store that
            # only reveals it under query load reveals it in production.
            self._numpy()
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()
        # Scan cache, keyed (table, scope_key).
        #
        # Writes through THIS adapter update it IN PLACE (we know exactly which
        # rows moved), so the common remember()-then-recall() cycle never pays a
        # rebuild. Writes by ANY OTHER connection or process on the same file are
        # caught by ``PRAGMA data_version``, which SQLite bumps for foreign
        # commits and deliberately does NOT bump for our own; a mismatch forces a
        # full rebuild. Without that second half, a second process writing the
        # same .db would be served stale vectors forever.
        self._scan_cache: dict[tuple[str, str], _ScanCache] = {}

    # -- vector-scan backend ------------------------------------------------
    def _numpy(self):
        """The numpy module to vectorize with, or None to stay pure-Python.

        ``auto`` (the default) NEVER imports numpy — it only uses it if
        something else already has. That keeps the zero-dependency default path
        honestly zero-dependency (``import rekoll`` + a StubEmbedder recall
        pulls in nothing), while a user on the ``[embeddings]`` extra gets the
        fast path for free: fastembed imports numpy, so by the time any real
        vector exists numpy is already resident. ``numpy`` forces it (and raises
        if absent); ``python`` pins the fallback.
        """
        if self.vector_backend == "python":
            return None
        if self.vector_backend == "numpy":
            import numpy  # noqa: PLC0415 - deliberately lazy

            return numpy
        return sys.modules.get("numpy")

    def _data_version(self) -> int:
        return self._conn.execute("PRAGMA data_version").fetchone()[0]

    def _apply_cache_writes(self, mutations: Sequence[tuple]) -> None:
        """Fold committed row mutations into any live cache entry.

        Applied only AFTER a successful commit, so a rolled-back batch leaves the
        cache exactly as it found it. Entries that were never built are skipped —
        they will be filled from SQLite on first query.
        """
        for op, table, skey, rid, entry in mutations:
            cached = self._scan_cache.get((table, skey))
            if cached is None:
                continue
            if op == "put":
                cached.put(rid, entry)
            else:
                cached.drop(rid)
        if mutations:
            # A long ingest can grow a live entry past the budget one row at a
            # time; it must not escape the bound just because it never re-scanned.
            self._evict_to_budget()

    def _cached_vector_count(self) -> int:
        return sum(len(e.rows) for e in self._scan_cache.values())

    def _evict_to_budget(self, protect: Optional[tuple[str, str]] = None) -> None:
        """Drop least-recently-scanned entries until the budget is met.

        ``self._scan_cache`` doubles as the LRU order: ``_scan`` re-inserts the
        entry it serves, moving it to the end, so the front is the coldest.
        """
        total = self._cached_vector_count()
        for key in list(self._scan_cache):
            if total <= self.vector_cache_max_vectors:
                return
            if key == protect:
                continue
            total -= len(self._scan_cache.pop(key).rows)

    def _scan(self, table: str, skey: str, data_version: int) -> _ScanCache:
        key = (table, skey)
        entry = self._scan_cache.get(key)
        if entry is not None and entry.data_version == data_version:
            self._scan_cache[key] = self._scan_cache.pop(key)  # LRU: touch
            return entry
        entry = _ScanCache(data_version)
        # Explicit rowid order: the pre-cache scan relied on SQLite's
        # implementation-defined row order to break exact score ties. Pinning it
        # makes tie order deterministic AND equal to what the old unordered scan
        # returned in practice (a table/index walk is already rowid-ascending).
        rows = self._conn.execute(
            f"SELECT id, status, trust_tier, embedding FROM {table} "
            f"WHERE scope_key=? AND embedding IS NOT NULL ORDER BY rowid",
            (skey,),
        ).fetchall()
        for row in rows:
            # A corrupt/non-finite embedding raises ValueError here (tamper-
            # visible): a hand-edited store fails cleanly rather than crashing the
            # scan on array('d', ...) or feeding a NaN into the cosine + gate.
            entry.rows[row["id"]] = _CachedVector(
                row["status"], row["trust_tier"], _decode_embedding(row["embedding"])
            )
        self._scan_cache.pop(key, None)
        if len(entry.rows) <= self.vector_cache_max_vectors:
            self._scan_cache[key] = entry
            self._evict_to_budget(protect=key)
        # else: this scope alone exceeds the budget. Serve the freshly decoded
        # scan and let it be garbage-collected — i.e. fall back to exactly the
        # pre-ADR-0030 cost model. Bounded memory beats a fast wrong promise.
        return entry

    # -- schema -------------------------------------------------------------
    def _create_schema(self) -> None:
        for table in _KIND_TABLE.values():
            self._conn.execute(
                f"""
                CREATE TABLE IF NOT EXISTS {table} (
                    id TEXT PRIMARY KEY,
                    human_id TEXT,
                    scope_key TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    content TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    source_id TEXT,
                    prov_source_uri TEXT NOT NULL,
                    prov_adapter_name TEXT NOT NULL,
                    prov_adapter_version TEXT NOT NULL,
                    prov_ingest_run_id TEXT,
                    prov_source_file TEXT,
                    prov_chunk_index INTEGER,
                    trust_tier INTEGER NOT NULL,
                    embedding TEXT,
                    embedder_name TEXT,
                    embedder_dim INTEGER,
                    created_at TEXT NOT NULL,
                    seen_at TEXT NOT NULL,
                    valid_from TEXT,
                    valid_until TEXT,
                    proof_count INTEGER NOT NULL DEFAULT 0,
                    declared_transformations TEXT NOT NULL DEFAULT '',
                    privacy_class TEXT NOT NULL DEFAULT 'unknown',
                    status TEXT NOT NULL DEFAULT 'active',
                    UNIQUE(scope_key, content_hash)
                )
                """
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_scope ON {table}(scope_key)"
            )
            # Board reads (ADR-0035) ORDER BY created_at within a scope on every
            # poll; additive + idempotent, no migration machinery — an existing
            # store gains it on next open.
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_scope_created "
                f"ON {table}(scope_key, created_at)"
            )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS record_metadata (
                record_id TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT,
                vtype TEXT NOT NULL,
                PRIMARY KEY (record_id, key)
            )
            """
        )
        # The Tier-2 board JOIN probes record_metadata by key (PK order is
        # (record_id, key), useless for a key-first lookup). Same additive,
        # idempotent shape as the scope_created indexes above.
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_record_metadata_key "
            "ON record_metadata(key, record_id)"
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS record_links (
                record_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                link_type TEXT NOT NULL,
                PRIMARY KEY (record_id, target_id, link_type)
            )
            """
        )
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS embedder_identity (
                scope_key TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                dim INTEGER NOT NULL,
                config_hash TEXT NOT NULL
            )
            """
        )
        self._conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS fts "
            "USING fts5(content, rid UNINDEXED, scope_key UNINDEXED, kind UNINDEXED)"
        )
        self._conn.commit()

    # -- writes -------------------------------------------------------------
    def add(self, *, records: Sequence[MemoryRecord]) -> None:
        self._write(records, replace=False)

    def upsert(self, *, records: Sequence[MemoryRecord]) -> None:
        self._write(records, replace=True)

    def _write(self, records: Sequence[MemoryRecord], *, replace: bool) -> None:
        mutations: list[tuple] = []
        try:
            for record in records:
                self._write_one(record, replace=replace, mutations=mutations)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise  # mutations discarded: the cache still mirrors the rolled-back DB
        self._apply_cache_writes(mutations)

    def _write_one(self, r: MemoryRecord, *, replace: bool, mutations: list[tuple]) -> None:
        table = _KIND_TABLE[r.kind]
        proof_count = r.proof_count
        if replace:
            # The PK id includes source_uri but UNIQUE() is on (scope_key,
            # content_hash). So the SAME content from a DIFFERENT source yields a
            # different id, and INSERT OR REPLACE silently deletes the prior PK row
            # via the UNIQUE conflict — orphaning its fts/metadata/link rows, which
            # are keyed by the *old* id. Purge the displaced id's child rows first.
            prior = self._conn.execute(
                f"SELECT id, trust_tier, proof_count FROM {table} "
                f"WHERE scope_key=? AND content_hash=?",
                (r.scope.key(), r.content_hash),
            ).fetchone()
            if prior is not None:
                # TRUST-AWARE UPSERT (ADR-0023): trust for identical content is
                # MONOTONIC — it may rise, never silently fall. This holds
                # regardless of source, because the UNIQUE(scope_key,
                # content_hash) upsert otherwise let a lower-trust write REPLACE a
                # trusted row (survivor keeps the content but takes the lower
                # trust — and, if it now trips the firewall, gets QUARANTINED and
                # vanishes from recall). Both the cross-source hijack and the
                # same-source re-ingest-at-a-lower-default downgrade are covered
                # (confirmed by repro).
                same_id = prior["id"] == r.id
                if int(r.trust_tier) < prior["trust_tier"]:
                    return  # never lower trust for identical content (any source)
                if int(r.trust_tier) == prior["trust_tier"] and not same_id:
                    return  # equal-trust, different source: dedup, keep incumbent
                if not same_id:
                    # A STRICTLY higher-trust source takes over a different id:
                    # purge the displaced id's orphaned fts/metadata/link rows.
                    old = prior["id"]
                    self._conn.execute("DELETE FROM record_metadata WHERE record_id=?", (old,))
                    self._conn.execute("DELETE FROM record_links WHERE record_id=?", (old,))
                    self._conn.execute("DELETE FROM fts WHERE rid=?", (old,))
                    # the UNIQUE conflict below will drop the displaced row too
                    mutations.append(("drop", table, r.scope.key(), old, None))
                # else same_id: fall through to update in place (idempotent
                # re-ingest / re-embed) — trust is equal or higher, never lower.
                #
                # PROOF-COUNT MONOTONIC (same rule as trust): the surviving row
                # keeps MAX(stored, incoming). A freshly-built re-ingest of
                # identical content carries proof_count=0 and used to ZERO the
                # promoted value mark_used had accumulated (L-proofcount-reset);
                # usage credit belongs to the content, so a takeover by a
                # higher-trust source keeps it too. An incoming HIGHER count
                # (an import/restore carrying usage) may still raise it.
                proof_count = max(int(prior["proof_count"]), proof_count)
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        vector = list(r.embedding) if r.embedding is not None else None
        embedding = json.dumps(vector) if vector is not None else None
        # The row lands at the END of the table (a fresh INSERT, or a REPLACE
        # that deletes then re-inserts), so the cache appends it at the end too.
        # A record with no embedding drops out of the scan entirely.
        mutations.append(
            (
                "put",
                table,
                r.scope.key(),
                r.id,
                None
                if vector is None
                else _CachedVector(r.status.value, int(r.trust_tier), vector),
            )
        )
        placeholders = ",".join("?" * 25)
        self._conn.execute(
            f"{verb} INTO {table} ({_INSERT_COLUMNS}) VALUES ({placeholders})",
            (
                r.id,
                r.human_id,
                r.scope.key(),
                r.kind.value,
                r.content,
                r.content_hash,
                r.source_id,
                r.provenance.source_uri,
                r.provenance.adapter_name,
                r.provenance.adapter_version,
                r.provenance.ingest_run_id,
                r.provenance.source_file,
                r.provenance.chunk_index,
                int(r.trust_tier),
                embedding,
                r.embedder_name,
                r.embedder_dim,
                _dt(r.created_at),
                _dt(r.seen_at),
                _dt(r.valid_from),
                _dt(r.valid_until),
                proof_count,
                ",".join(r.declared_transformations),
                r.privacy_class,
                r.status.value,
            ),
        )
        self._conn.execute("DELETE FROM record_metadata WHERE record_id=?", (r.id,))
        for key, value in r.metadata.items():
            vtype, sval = _encode_scalar(value)
            self._conn.execute(
                "INSERT INTO record_metadata (record_id, key, value, vtype) VALUES (?,?,?,?)",
                (r.id, key, sval, vtype),
            )
        self._conn.execute(
            "DELETE FROM record_links WHERE record_id=? AND link_type='derived_from'",
            (r.id,),
        )
        for target in r.provenance.derived_from:
            self._conn.execute(
                "INSERT OR IGNORE INTO record_links (record_id, target_id, link_type) VALUES (?,?,?)",
                (r.id, target, "derived_from"),
            )
        self._conn.execute("DELETE FROM fts WHERE rid=?", (r.id,))
        self._conn.execute(
            "INSERT INTO fts (content, rid, scope_key, kind) VALUES (?,?,?,?)",
            (r.content, r.id, r.scope.key(), r.kind.value),
        )

    def delete(self, *, scope: Scope, ids: Sequence[str]) -> int:
        ids = list(ids)
        if not ids:
            return 0
        skey = scope.key()
        placeholders = ",".join("?" * len(ids))
        # Resolve which ids actually live in THIS scope FIRST. record_metadata and
        # record_links carry no scope_key column, so cleaning them by the
        # caller-supplied ids (as a previous version did) let a caller in scope A
        # wipe scope B's metadata + lexical index just by passing B's id — an
        # ADR-0003 isolation violation. We only ever touch child/FTS rows for ids
        # confirmed to belong to this scope.
        in_scope: list[str] = []
        mutations: list[tuple] = []
        for table in _KIND_TABLE.values():
            rows = self._conn.execute(
                f"SELECT id FROM {table} WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            ).fetchall()
            in_scope.extend(row["id"] for row in rows)
            mutations.extend(("drop", table, skey, row["id"], None) for row in rows)
        removed = 0
        for table in _KIND_TABLE.values():
            cur = self._conn.execute(
                f"DELETE FROM {table} WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            )
            removed += cur.rowcount
        if in_scope:
            ph = ",".join("?" * len(in_scope))
            self._conn.execute(f"DELETE FROM record_metadata WHERE record_id IN ({ph})", tuple(in_scope))
            self._conn.execute(f"DELETE FROM record_links WHERE record_id IN ({ph})", tuple(in_scope))
            self._conn.execute(f"DELETE FROM fts WHERE rid IN ({ph})", tuple(in_scope))
        self._conn.commit()
        self._apply_cache_writes(mutations)
        return removed

    def bump_proof_count(self, *, scope: Scope, ids: Sequence[str]) -> int:
        """Atomic, targeted ``proof_count += 1`` for in-scope, non-quarantined
        ids (the was-it-used signal). Unlike a full-row upsert this can neither
        lose a concurrent increment nor revert a concurrent change to any other
        column: the increment happens IN the database (``SET proof_count =
        proof_count + 1``), touching only that column.

        "Non-quarantined" is the EFFECTIVE status (``_effective_status``): a
        forged row (raw ``status='active'`` at trust 0) is effectively
        quarantined, so it is NOT credited — matching the base-class default,
        which filters the reconstructed record's status. Gating the raw column
        instead credited a quarantine-level row as if it were active."""
        ids = list(ids)
        if not ids:
            return 0
        skey = scope.key()
        placeholders = ",".join("?" * len(ids))
        credited = 0
        for table in _KIND_TABLE.values():
            cur = self._conn.execute(
                f"UPDATE {table} SET proof_count = proof_count + 1 "
                f"WHERE scope_key=? AND {_EFFECTIVE_STATUS_SQL} != ? AND id IN ({placeholders})",
                (skey, *_EFFECTIVE_STATUS_SQL_PARAMS, Status.QUARANTINED.value, *ids),
            )
            credited += cur.rowcount
        self._conn.commit()
        # NO scan-cache mutation on purpose. This touches exactly one column —
        # proof_count — and the cache holds none of it (id/status/trust_tier/
        # vector/norm only); the records handed back by vector_query are re-read
        # from SQLite, so the fresh count is always what the caller sees. Since
        # mark_used() fires after every recall, dropping the cache here would
        # throw it away on every read cycle and undo the whole optimization.
        return credited

    # -- reads --------------------------------------------------------------
    def get(self, *, scope: Scope, ids: Sequence[str]) -> GetResult:
        ids = list(ids)
        if not ids:
            return GetResult(records=())
        skey = scope.key()
        placeholders = ",".join("?" * len(ids))
        out: list[MemoryRecord] = []
        for table in _KIND_TABLE.values():
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            ).fetchall()
            out.extend(self._row_to_record(row) for row in rows)
        return GetResult(records=tuple(out))

    def count(
        self, *, scope: Scope, kind: Optional[Kind] = None, status: Optional[str] = None
    ) -> int:
        skey = scope.key()
        tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
        total = 0
        for table in tables:
            sql = f"SELECT COUNT(*) AS c FROM {table} WHERE scope_key=?"
            params: list = [skey]
            if status is not None:
                # Filter on the EFFECTIVE status (``_effective_status``), so a
                # forged ``status='active'`` row at trust 0 counts as quarantined,
                # never as active — the count feeds the MCP status number and an
                # audit view, both of which must agree with the lexical leg.
                sql += f" AND {_EFFECTIVE_STATUS_SQL} = ?"
                params.extend([*_EFFECTIVE_STATUS_SQL_PARAMS, status])
            row = self._conn.execute(sql, tuple(params)).fetchone()
            total += row["c"]
        return total

    def newest(self, *, scope: Scope, n: int = 3, kind: Optional[Kind] = None) -> GetResult:
        if n <= 0:
            return GetResult(records=())
        skey = scope.key()
        tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
        rows: list[sqlite3.Row] = []
        for table in tables:
            rows.extend(
                self._conn.execute(
                    # created_at is ISO-8601 UTC, so lexicographic order IS
                    # chronological; id breaks same-instant ties deterministically
                    # (DESC to match the Python merge sort below).
                    f"SELECT * FROM {table} WHERE scope_key=? "
                    f"ORDER BY created_at DESC, id DESC LIMIT ?",
                    (skey, n),
                ).fetchall()
            )
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return GetResult(records=tuple(self._row_to_record(row) for row in rows[:n]))

    def active_directives(
        self, *, scope: Scope, limit: int, min_trust: int
    ) -> GetResult:
        """Active directives in ``scope`` at ``trust >= min_trust``, oldest first,
        capped at ``limit`` — the standing-directive channel read (ADR-0034).

        A single scoped SELECT over the dedicated ``directives`` table (ADR-0001:
        directives are their own physical table), zero-LLM and zero-embedding
        (ADR-0007). Gates on the EFFECTIVE status (``_effective_status``) exactly
        like ``count`` / ``vector_query`` / ``lexical_query`` do, so a forged raw
        ``status='active'`` row at trust 0 can never surface as a standing rule.
        (The ``trust >= min_trust`` floor is itself above QUARANTINED, so the
        effective-status CASE never actually reclassifies here — but gating
        identically keeps the one-status-rule invariant visible across every read
        leg.) Oldest-first (``created_at`` ASC, ``id`` ASC) is the contract from
        :meth:`StorageAdapter.active_directives`: under the cap the foundational
        rules survive, and the rendered prefix stays byte-stable as rules accrue.
        """
        if limit <= 0:
            return GetResult(records=())
        skey = scope.key()
        table = _KIND_TABLE[Kind.DIRECTIVE]
        rows = self._conn.execute(
            f"SELECT * FROM {table} WHERE scope_key=? "
            f"AND {_EFFECTIVE_STATUS_SQL} = ? AND trust_tier >= ? "
            f"ORDER BY created_at ASC, id ASC LIMIT ?",
            (
                skey,
                *_EFFECTIVE_STATUS_SQL_PARAMS,
                Status.ACTIVE.value,
                int(min_trust),
                int(limit),
            ),
        ).fetchall()
        return GetResult(records=tuple(self._row_to_record(row) for row in rows))

    # -- live project board (ADR-0035) --------------------------------------
    @contextmanager
    def _read_txn(self):
        """One consistent read snapshot across several SELECTs.

        Every public method on this adapter commits before returning, so the
        connection is in autocommit between calls and each of a multi-statement
        read's SELECTs sees whatever foreign commit landed in between — a torn
        board. An explicit BEGIN (deferred) pins all reads inside to the
        snapshot WAL establishes at the first statement; ROLLBACK ends it
        without writing (this is a read-only transaction). Ended per call on
        purpose: the NEXT board read starts a fresh snapshot, so a foreign
        connection's committed write is always visible to it (these reads never
        touch the vector scan cache). Joins an already-open transaction rather
        than nesting a BEGIN.
        """
        if self._conn.in_transaction:
            yield
            return
        self._conn.execute("BEGIN")
        try:
            yield
        finally:
            self._conn.rollback()

    def _recent_rows(self, skey: str, limit: int, min_trust: int) -> list[sqlite3.Row]:
        """Tier-1 rows: effective-active at/above ``min_trust``, newest first —
        the ``newest()`` per-table-then-Python-merge shape with the surfaced-read
        gates ``newest()`` deliberately lacks."""
        rows: list[sqlite3.Row] = []
        for table in _KIND_TABLE.values():
            rows.extend(
                self._conn.execute(
                    # created_at is ISO-8601 UTC, so lexicographic order IS
                    # chronological; id breaks same-instant ties (as newest()).
                    f"SELECT * FROM {table} WHERE scope_key=? "
                    f"AND {_EFFECTIVE_STATUS_SQL} = ? AND trust_tier >= ? "
                    f"ORDER BY created_at DESC, id DESC LIMIT ?",
                    (
                        skey,
                        *_EFFECTIVE_STATUS_SQL_PARAMS,
                        Status.ACTIVE.value,
                        int(min_trust),
                        limit,
                    ),
                ).fetchall()
            )
        rows.sort(key=lambda row: (row["created_at"], row["id"]), reverse=True)
        return rows[:limit]

    def _board_rows(self, skey: str, limit: int, min_trust: int) -> list[sqlite3.Row]:
        """Tier-2 rows: ``board`` in {major, pending} + the same gates, oldest
        first. record_metadata has NO scope_key column, so every gate
        (scope/status/trust) sits on the KIND-TABLE side — a metadata-first
        query would leak tags across scopes. (record_metadata's PK is
        (record_id, key): one 'board' row per record, so the JOIN cannot fan
        out.) The bare ``status``/``trust_tier`` in the effective-status CASE
        are unambiguous — record_metadata has no columns of those names."""
        rows: list[sqlite3.Row] = []
        for table in _KIND_TABLE.values():
            rows.extend(
                self._conn.execute(
                    f"SELECT t.* FROM {table} AS t "
                    f"JOIN record_metadata AS m ON m.record_id = t.id "
                    f"WHERE m.key = ? AND m.value IN (?, ?) "
                    f"AND t.scope_key = ? AND {_EFFECTIVE_STATUS_SQL} = ? "
                    f"AND t.trust_tier >= ? "
                    f"ORDER BY t.created_at ASC, t.id ASC LIMIT ?",
                    (
                        BOARD_METADATA_KEY,
                        BOARD_TAG_MAJOR,
                        BOARD_TAG_PENDING,
                        skey,
                        *_EFFECTIVE_STATUS_SQL_PARAMS,
                        Status.ACTIVE.value,
                        int(min_trust),
                        limit,
                    ),
                ).fetchall()
            )
        rows.sort(key=lambda row: (row["created_at"], row["id"]))
        return rows[:limit]

    def _pending_open_count(self, skey: str, min_trust: int) -> int:
        """FULL count of ``board=pending`` rows passing the Tier-2 gates (not
        capped by any leg limit); same kind-table-side gating as _board_rows."""
        total = 0
        for table in _KIND_TABLE.values():
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} AS t "
                f"JOIN record_metadata AS m ON m.record_id = t.id "
                f"WHERE m.key = ? AND m.value = ? "
                f"AND t.scope_key = ? AND {_EFFECTIVE_STATUS_SQL} = ? "
                f"AND t.trust_tier >= ?",
                (
                    BOARD_METADATA_KEY,
                    BOARD_TAG_PENDING,
                    skey,
                    *_EFFECTIVE_STATUS_SQL_PARAMS,
                    Status.ACTIVE.value,
                    int(min_trust),
                ),
            ).fetchone()
            total += row["c"]
        return total

    def recent_records(
        self, *, scope: Scope, limit: int = 10, min_trust: int = int(TrustTier.UNVERIFIED)
    ) -> GetResult:
        """Tier 1 of the live project board (see :meth:`StorageAdapter.recent_records`).

        Deliberately NOT a reuse of :meth:`newest`: that read has no status or
        trust gate — ``health()``/``reindex()`` depend on it seeing every row,
        forged or quarantined included — while this one gates on the EFFECTIVE
        status (``_effective_status``) and the trust floor like every other
        surfaced read leg. Rows and their child-table reconstruction happen
        inside one read transaction so the returned records are one snapshot.
        """
        limit = _validate_board_limit("limit", limit)
        if limit == 0:
            return GetResult(records=())
        skey = scope.key()
        with self._read_txn():
            rows = self._recent_rows(skey, limit, min_trust)
            return GetResult(records=tuple(self._row_to_record(row) for row in rows))

    def board_entries(
        self,
        *,
        scope: Scope,
        limit: int = 10,
        min_trust: int = int(TrustTier.TRUSTED_SOURCE),
    ) -> GetResult:
        """Tier 2 of the live project board (see :meth:`StorageAdapter.board_entries`).

        Oldest-first (``created_at`` ASC, ``id`` ASC — ADR-0034 §4's
        prefix-stability rationale), capped, gated on the kind-table side only.
        """
        limit = _validate_board_limit("limit", limit)
        if limit == 0:
            return GetResult(records=())
        skey = scope.key()
        with self._read_txn():
            rows = self._board_rows(skey, limit, min_trust)
            return GetResult(records=tuple(self._row_to_record(row) for row in rows))

    def board_snapshot(
        self,
        *,
        scope: Scope,
        recent_limit: int = 10,
        major_limit: int = 10,
        min_trust: int = int(TrustTier.UNVERIFIED),
    ) -> BoardSnapshot:
        """Both tiers + the open-pending count from ONE read transaction, so a
        concurrent writer can never produce a torn snapshot (tiers that
        contradict each other). ``min_trust`` gates the Tier-1 leg only; the
        curated leg and ``pending_open`` always apply the Tier-2 board floor
        policy (``firewall.BOARD_FLOOR`` — spelled via ``TrustTier`` here
        because ``firewall`` imports this package; a test pins them equal).
        """
        recent_limit = _validate_board_limit("recent_limit", recent_limit)
        major_limit = _validate_board_limit("major_limit", major_limit)
        skey = scope.key()
        tier2_floor = int(TrustTier.TRUSTED_SOURCE)  # == int(firewall.BOARD_FLOOR)
        with self._read_txn():
            major_rows = self._board_rows(skey, major_limit, tier2_floor) if major_limit else []
            recent_rows = self._recent_rows(skey, recent_limit, min_trust) if recent_limit else []
            pending = self._pending_open_count(skey, tier2_floor)
            return BoardSnapshot(
                majors=tuple(self._row_to_record(row) for row in major_rows),
                recent=tuple(self._row_to_record(row) for row in recent_rows),
                pending_open=pending,
            )

    def set_status(self, *, scope: Scope, record_id: str, status: str) -> bool:
        """Atomic, targeted status transition for one effective-active record —
        the :meth:`bump_proof_count` concurrency pattern: the effective-status
        gate lives IN the UPDATE's WHERE (evaluated on the pre-update row), so
        two racing callers yield exactly one transition and there is no
        read-modify-write window. The gate means a quarantined, forged
        (raw-active-at-trust-0), proposed, superseded, or cross-scope row never
        transitions — and nothing can be resurrected through this verb.
        """
        status_value = Status(status).value  # garbage target -> loud ValueError
        skey = scope.key()
        transitioned: list[str] = []
        for table in _KIND_TABLE.values():
            cur = self._conn.execute(
                f"UPDATE {table} SET status = ? "
                f"WHERE scope_key=? AND id=? AND {_EFFECTIVE_STATUS_SQL} = ?",
                (
                    status_value,
                    skey,
                    record_id,
                    *_EFFECTIVE_STATUS_SQL_PARAMS,
                    Status.ACTIVE.value,
                ),
            )
            if cur.rowcount:
                transitioned.append(table)
        self._conn.commit()
        # Scan-cache coherence: unlike bump_proof_count's proof_count, `status`
        # IS held by the scan cache (the vector gate reads it). An UPDATE moves
        # no rowid, so the entry is patched in place — never dropped — keeping
        # the remember()-then-recall() cache warm. (_mats holds ids/vectors/
        # norms only, so no invalidation is needed there.)
        for table in transitioned:
            cached = self._scan_cache.get((table, skey))
            if cached is not None:
                entry = cached.rows.get(record_id)
                if entry is not None:
                    entry.status = status_value
        return bool(transitioned)

    def vector_query(
        self,
        *,
        scope: Scope,
        embedding: Sequence[float],
        k: int = 10,
        kind: Optional[Kind] = None,
        where: Optional[Mapping[str, object]] = None,
    ) -> QueryResult:
        status_filter, min_trust = _validate_where(where)
        query_vec = [float(x) for x in embedding]
        qdim = len(query_vec)
        if k <= 0:
            return QueryResult(hits=())
        skey = scope.key()
        tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
        # qnorm accumulated exactly as embedding.cosine's `na` loop does.
        qn = 0.0
        for x in query_vec:
            qn += x * x
        qnorm = math.sqrt(qn)

        np = self._numpy()
        data_version = self._data_version()
        scored: list[tuple[float, str, str]] = []  # (score, table, id) in scan order
        for table in tables:
            entry = self._scan(table, skey, data_version)
            # Rows of a different dim come from a different embedder (e.g. a
            # model swap mid-scope). They are not comparable, so they are
            # skipped — not crashed on, not truncated — and keyword recall keeps
            # working while the scope is re-embedded.
            if np is None:
                for rid, v in entry.rows.items():
                    if v.dim != qdim:
                        continue
                    # Gate on the EFFECTIVE status (mirrors the lexical leg): a
                    # forged raw-'active'-at-trust-0 row is quarantined, so it
                    # neither surfaces through status='active' nor hides from
                    # status='quarantined'. min_trust still reads the raw tier
                    # (the effective-status rule reclassifies status, not trust).
                    if status_filter is not None and _effective_status(v.status, v.trust) != status_filter:
                        continue
                    if min_trust is not None and v.trust < min_trust:
                        continue
                    denom = qnorm * v.norm
                    # reduce(add, ...) is a NAIVE left fold, matching cosine()'s
                    # `dot += x*y` accumulation bit for bit. Do not swap in the
                    # builtin sum(): since 3.12 it applies Neumaier compensated
                    # summation to floats, which is more accurate and therefore
                    # NOT equal to the score this adapter returned before.
                    dot = reduce(add, map(mul, query_vec, v.vec), 0.0)
                    scored.append((dot / denom if denom else 0.0, table, rid))
            else:
                dim_ids, mat, dim_norms = entry.matrix_for_dim(np, qdim)
                if not dim_ids:
                    continue
                rows = entry.rows
                if status_filter is None and min_trust is None:
                    keep = dim_ids  # whole dim group survives; skip the gather
                else:
                    local = [
                        j
                        for j, rid in enumerate(dim_ids)
                        if (
                            status_filter is None
                            or _effective_status(rows[rid].status, rows[rid].trust) == status_filter
                        )
                        and (min_trust is None or rows[rid].trust >= min_trust)
                    ]
                    if not local:
                        continue
                    keep = [dim_ids[j] for j in local]
                    mat = mat[local]
                    dim_norms = dim_norms[local]
                denom = dim_norms * qnorm
                dots = mat @ np.asarray(query_vec, dtype=np.float64)
                scores = np.divide(dots, denom, out=np.zeros_like(dots), where=denom != 0.0)
                scored.extend(zip(scores.tolist(), [table] * len(keep), keep))
        # Stable sort: exact ties keep scan order (table order, then rowid) —
        # the same tiebreak the old unordered brute-force scan produced.
        scored.sort(key=lambda item: item[0], reverse=True)
        if not scored:
            return QueryResult(hits=())
        # TOCTOU window (PR #42 review): the ranking above is consistent with
        # the ``data_version`` snapshot, but ``_rows_for`` re-reads the winners
        # LIVE. A foreign connection committing inside that window can DELETE a
        # winner (its key is then absent from ``rows_by_key``) or UPDATE one
        # below the status/min_trust gate that admitted it from the cache.
        # INVARIANT: the cached path never returns a result the old
        # single-snapshot scan could not have returned. So absent winners are
        # SKIPPED (mirroring lexical_query's ``if rid in records`` guard),
        # every row is re-checked against the SAME filters on its LIVE columns,
        # and dropped winners are BACKFILLED from the scored tail — yielding
        # exactly what the old scan would have returned had the foreign commit
        # landed just before its single read. Backfill fetches one batch per
        # round via ``_rows_for`` (never a per-row query), and backfilled
        # candidates pass the same live re-checks; the loop ends at k live
        # winners or when the candidates are exhausted. The no-race common
        # case stays a single ``_rows_for`` round-trip over ``scored[:k]``.
        hits: list[QueryHit] = []
        pos = 0
        while len(hits) < k and pos < len(scored):
            batch = scored[pos : pos + (k - len(hits))]
            pos += len(batch)
            rows_by_key = self._rows_for(skey, batch)
            for score, table, rid in batch:
                row = rows_by_key.get((table, rid))
                if row is None:  # deleted (or re-scoped) inside the window
                    continue
                # Same EFFECTIVE-status gate as the cache scan, re-applied on the
                # LIVE row: a foreign write that lands a forged (raw-'active',
                # trust-0) row inside the TOCTOU window is still gated correctly.
                if (
                    status_filter is not None
                    and _effective_status(row["status"], int(row["trust_tier"])) != status_filter
                ):
                    continue
                if min_trust is not None and row["trust_tier"] < min_trust:
                    continue
                hits.append(QueryHit(record=self._row_to_record(row), score=score))
        return QueryResult(hits=tuple(hits))

    def _rows_for(
        self, skey: str, want: Sequence[tuple[float, str, str]]
    ) -> dict[tuple[str, str], sqlite3.Row]:
        """Full rows for the winners only.

        The scan used to ``SELECT *`` every candidate row just to throw all but
        ``k`` of them away. Fetching the top-k by id after ranking keeps the
        record data live (nothing about a returned record is served from cache)
        while reading ~k rows instead of N.
        """
        by_table: dict[str, list[str]] = {}
        for _, table, rid in want:
            by_table.setdefault(table, []).append(rid)
        out: dict[tuple[str, str], sqlite3.Row] = {}
        for table, ids in by_table.items():
            placeholders = ",".join("?" * len(ids))
            rows = self._conn.execute(
                f"SELECT * FROM {table} WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            ).fetchall()
            for row in rows:
                out[(table, row["id"])] = row
        return out

    def lexical_query(
        self,
        *,
        scope: Scope,
        text: str,
        k: int = 10,
        kind: Optional[Kind] = None,
        where: Optional[Mapping[str, object]] = None,
    ) -> QueryResult:
        status_filter, min_trust = _validate_where(where)
        if k <= 0:
            # Asking for nothing returns nothing. The bound below is checked
            # AFTER an append, which returned one phantom hit for k<=0 while
            # vector_query returned zero (conformance-gated: both legs agree).
            return QueryResult(hits=())
        match = _fts_query(text)
        if match is None:
            return QueryResult(hits=())
        has_filter = status_filter is not None or min_trust is not None
        skey = scope.key()
        sql = "SELECT rid, bm25(fts) AS s FROM fts WHERE fts MATCH ? AND scope_key=?"
        params: list[object] = [match, skey]
        if kind is not None:
            sql += " AND kind=?"
            params.append(kind.value)
        sql += " ORDER BY s"
        if not has_filter:
            # Fast path: a bounded over-fetch is enough (we trim to k below).
            sql += " LIMIT ?"
            params.append(max(0, k) * 4 + 10)
        # With a status/min_trust filter we must NOT cap the fetch before filtering:
        # low-trust / wrong-status rows ranking ahead can crowd valid matches out of
        # a fixed window and wrongly return < k hits. The MATCH set is already
        # scope-bounded, so scanning it fully and trimming to k after the filter is
        # both correct and acceptable at SQLite's local scale.
        rows = self._conn.execute(sql, params).fetchall()
        if not rows:
            return QueryResult(hits=())
        # Rank and filter on two cheap scalar columns, THEN reconstruct only the
        # k winners. Building a full MemoryRecord costs two child-table queries
        # and a json.loads of the embedding; doing that for every MATCH row just
        # to keep k of them made a 1k-record recall spend most of its time
        # decoding vectors it would immediately discard.
        gate = self._filter_columns(skey, [row["rid"] for row in rows])
        chosen: list[tuple[str, float]] = []
        for row in rows:
            probe = gate.get(row["rid"])
            if probe is None:  # an fts row whose record is gone, or out of scope
                continue
            status, trust = probe
            if status_filter is not None and status != status_filter:
                continue
            if min_trust is not None and trust < min_trust:
                continue
            chosen.append((row["rid"], -float(row["s"])))  # bm25: lower is better
            if len(chosen) >= k:
                break
        if not chosen:
            return QueryResult(hits=())
        records = {r.id: r for r in self.get(scope=scope, ids=[rid for rid, _ in chosen]).records}
        return QueryResult(
            hits=tuple(
                QueryHit(record=records[rid], score=score)
                for rid, score in chosen
                if rid in records
            )
        )

    def _filter_columns(self, skey: str, ids: Sequence[str]) -> dict[str, tuple[str, int]]:
        """``{id: (effective_status, trust_tier)}`` for in-scope ids — the two
        columns the read-path filters gate on, without reconstructing a record.

        ``status`` is the EFFECTIVE status via the shared ``_effective_status``
        predicate — the same rule the vector scan, its live re-check, and
        ``count`` / ``bump_proof_count`` now all use, so every leg gates a forged
        ``status='active'`` row at trust 0 as quarantined identically. Reading
        the raw column instead would let it surface through a ``status='active'``
        filter (and hide from a ``status='quarantined'`` audit).
        """
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        out: dict[str, tuple[str, int]] = {}
        for table in _KIND_TABLE.values():
            rows = self._conn.execute(
                f"SELECT id, status, trust_tier FROM {table} "
                f"WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            ).fetchall()
            for row in rows:
                trust = int(row["trust_tier"])
                out[row["id"]] = (_effective_status(row["status"], trust), trust)
        return out

    # -- embedder identity --------------------------------------------------
    def get_embedder_identity(self, *, scope: Scope) -> Optional[EmbedderIdentity]:
        row = self._conn.execute(
            "SELECT name, dim, config_hash FROM embedder_identity WHERE scope_key=?",
            (scope.key(),),
        ).fetchone()
        if row is None:
            return None
        return EmbedderIdentity(name=row["name"], dim=row["dim"], config_hash=row["config_hash"])

    def set_embedder_identity(self, *, scope: Scope, identity: EmbedderIdentity) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO embedder_identity (scope_key, name, dim, config_hash) "
            "VALUES (?,?,?,?)",
            (scope.key(), identity.name, identity.dim, identity.config_hash),
        )
        self._conn.commit()

    def close(self) -> None:
        self._scan_cache.clear()
        self._conn.close()

    # -- reconstruction -----------------------------------------------------
    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        rid = row["id"]
        meta_rows = self._conn.execute(
            "SELECT key, value, vtype FROM record_metadata WHERE record_id=?", (rid,)
        ).fetchall()
        metadata = {m["key"]: _decode_scalar(m["vtype"], m["value"]) for m in meta_rows}
        link_rows = self._conn.execute(
            "SELECT target_id FROM record_links WHERE record_id=? AND link_type='derived_from'",
            (rid,),
        ).fetchall()
        derived_from = tuple(link["target_id"] for link in link_rows)
        provenance = Provenance(
            source_uri=row["prov_source_uri"],
            adapter_name=row["prov_adapter_name"],
            adapter_version=row["prov_adapter_version"],
            ingest_run_id=row["prov_ingest_run_id"],
            source_file=row["prov_source_file"],
            chunk_index=row["prov_chunk_index"],
            derived_from=derived_from,
        )
        # Decode the returned record's embedding through the same validated path
        # (raises a clean ValueError on a corrupt/non-finite cell instead of
        # building a garbage tuple that MemoryRecord's validator would reject with
        # a confusing error) — tamper stays visible and uniform across legs.
        embedding = tuple(_decode_embedding(row["embedding"])) if row["embedding"] else None
        dt_raw = row["declared_transformations"]
        declared = tuple(x for x in dt_raw.split(",") if x) if dt_raw else ()
        return MemoryRecord(
            id=rid,
            scope=_scope_from_key(row["scope_key"]),
            kind=Kind(row["kind"]),
            content=row["content"],
            content_hash=row["content_hash"],
            provenance=provenance,
            trust_tier=TrustTier(row["trust_tier"]),
            human_id=row["human_id"],
            source_id=row["source_id"],
            embedding=embedding,
            embedder_name=row["embedder_name"],
            embedder_dim=row["embedder_dim"],
            created_at=_parse_dt(row["created_at"]),
            seen_at=_parse_dt(row["seen_at"]),
            valid_from=_parse_dt(row["valid_from"]),
            valid_until=_parse_dt(row["valid_until"]),
            proof_count=row["proof_count"],
            declared_transformations=declared,
            privacy_class=row["privacy_class"],
            status=Status(row["status"]),
            metadata=metadata,
        )
