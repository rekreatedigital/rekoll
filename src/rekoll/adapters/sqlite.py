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
from datetime import datetime
from functools import reduce
from operator import add, mul
from typing import Mapping, Optional, Sequence

from ..embedding import EmbedderIdentity
from ..model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from .base import CAP_LEXICAL, CAP_VECTOR, GetResult, QueryHit, QueryResult, StorageAdapter

_KIND_TABLE = {
    Kind.RAW_FACT: "verbatim_records",
    Kind.OBSERVATION: "observations",
    Kind.DIRECTIVE: "directives",
    Kind.EPISODE: "episodes",
}

_ALLOWED_WHERE_KEYS = {"status", "min_trust"}

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
            entry.rows[row["id"]] = _CachedVector(
                row["status"], row["trust_tier"], json.loads(row["embedding"])
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
        proof_count + 1``), touching only that column."""
        ids = list(ids)
        if not ids:
            return 0
        skey = scope.key()
        placeholders = ",".join("?" * len(ids))
        credited = 0
        for table in _KIND_TABLE.values():
            cur = self._conn.execute(
                f"UPDATE {table} SET proof_count = proof_count + 1 "
                f"WHERE scope_key=? AND status!=? AND id IN ({placeholders})",
                (skey, Status.QUARANTINED.value, *ids),
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
                sql += " AND status=?"
                params.append(status)
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

    def vector_query(
        self,
        *,
        scope: Scope,
        embedding: Sequence[float],
        k: int = 10,
        kind: Optional[Kind] = None,
        where: Optional[Mapping[str, object]] = None,
    ) -> QueryResult:
        if where:
            bad = set(where) - _ALLOWED_WHERE_KEYS
            if bad:
                raise ValueError(
                    f"unsupported where keys {sorted(bad)}; "
                    f"allowed: {sorted(_ALLOWED_WHERE_KEYS)}"
                )
        query_vec = [float(x) for x in embedding]
        qdim = len(query_vec)
        if k <= 0:
            return QueryResult(hits=())
        skey = scope.key()
        tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
        status_filter = where.get("status") if where else None
        min_trust = None if not where or where.get("min_trust") is None else int(where["min_trust"])
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
                    if status_filter is not None and v.status != status_filter:
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
                        if (status_filter is None or rows[rid].status == status_filter)
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
        top = scored[:k]
        if not top:
            return QueryResult(hits=())
        rows_by_key = self._rows_for(skey, top)
        hits = tuple(
            QueryHit(record=self._row_to_record(rows_by_key[(table, rid)]), score=score)
            for score, table, rid in top
        )
        return QueryResult(hits=hits)

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
        if where:
            bad = set(where) - _ALLOWED_WHERE_KEYS
            if bad:
                raise ValueError(
                    f"unsupported where keys {sorted(bad)}; "
                    f"allowed: {sorted(_ALLOWED_WHERE_KEYS)}"
                )
        if k <= 0:
            # Asking for nothing returns nothing. The bound below is checked
            # AFTER an append, which returned one phantom hit for k<=0 while
            # vector_query returned zero (conformance-gated: both legs agree).
            return QueryResult(hits=())
        match = _fts_query(text)
        if match is None:
            return QueryResult(hits=())
        status_filter = where.get("status") if where else None
        min_trust = where.get("min_trust") if where else None
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
            if min_trust is not None and trust < int(min_trust):
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
        """``{id: (status, trust_tier)}`` for in-scope ids — the two columns the
        read-path filters gate on, without reconstructing a record.

        ``status`` is the EFFECTIVE status, i.e. what ``MemoryRecord`` would
        report. Quarantine-level trust forces ``status=quarantined`` at
        construction (``model.MemoryRecord.__post_init__``), so a row whose
        stored status column still says ``active`` — written by an older Rekoll,
        or by hand — must be gated as quarantined here too. Reading the raw
        column instead would let it surface through a ``status='active'`` filter.
        """
        if not ids:
            return {}
        placeholders = ",".join("?" * len(ids))
        active = Status.ACTIVE.value
        quarantined = Status.QUARANTINED.value
        floor = int(TrustTier.QUARANTINED)
        out: dict[str, tuple[str, int]] = {}
        for table in _KIND_TABLE.values():
            rows = self._conn.execute(
                f"SELECT id, status, trust_tier FROM {table} "
                f"WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            ).fetchall()
            for row in rows:
                trust = int(row["trust_tier"])
                status = row["status"]
                if trust <= floor and status == active:
                    status = quarantined
                out[row["id"]] = (status, trust)
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
        embedding = tuple(json.loads(row["embedding"])) if row["embedding"] else None
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
