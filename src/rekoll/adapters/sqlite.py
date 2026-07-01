"""Reference storage adapter: local SQLite, zero-config, no daemon, no key.

This is the default backend and the executable example of the canonical schema:
 - SEPARATE physical tables per kind (verbatim_records / observations /
   directives / episodes) — ADR-0001, deliberately not one table + a type column.
 - Flat-scalar metadata and typed links live in BOUNDED CHILD TABLES
   (record_metadata, record_links) — never an unbounded JSON blob.
 - Per-scope embedder identity is persisted for the guard.

Vector search is computed in pure Python over stored vectors here so the
foundation runs with zero native/ML dependencies. P1 swaps in sqlite-vec + a
real local embedding model; the adapter contract does not change.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from typing import Mapping, Optional, Sequence

from ..embedding import EmbedderIdentity, cosine
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


class SQLiteAdapter(StorageAdapter):
    name = "sqlite"
    capabilities = frozenset({CAP_VECTOR, CAP_LEXICAL})
    distance_metric = "cosine"

    def __init__(self, path: str = ":memory:") -> None:
        self.path = path
        self._conn = sqlite3.connect(path)
        self._conn.row_factory = sqlite3.Row
        if path != ":memory:":
            self._conn.execute("PRAGMA journal_mode=WAL")
        self._create_schema()

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
        try:
            for record in records:
                self._write_one(record, replace=replace)
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def _write_one(self, r: MemoryRecord, *, replace: bool) -> None:
        table = _KIND_TABLE[r.kind]
        if replace:
            # The PK id includes source_uri but UNIQUE() is on (scope_key,
            # content_hash). So the SAME content from a DIFFERENT source yields a
            # different id, and INSERT OR REPLACE silently deletes the prior PK row
            # via the UNIQUE conflict — orphaning its fts/metadata/link rows, which
            # are keyed by the *old* id. Purge the displaced id's child rows first.
            prior = self._conn.execute(
                f"SELECT id, trust_tier FROM {table} WHERE scope_key=? AND content_hash=?",
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
                # else same_id: fall through to update in place (idempotent
                # re-ingest / re-embed) — trust is equal or higher, never lower.
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        embedding = json.dumps(list(r.embedding)) if r.embedding is not None else None
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
                r.proof_count,
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
        for table in _KIND_TABLE.values():
            rows = self._conn.execute(
                f"SELECT id FROM {table} WHERE scope_key=? AND id IN ({placeholders})",
                (skey, *ids),
            ).fetchall()
            in_scope.extend(row["id"] for row in rows)
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
        return removed

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

    def count(self, *, scope: Scope, kind: Optional[Kind] = None) -> int:
        skey = scope.key()
        tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
        total = 0
        for table in tables:
            row = self._conn.execute(
                f"SELECT COUNT(*) AS c FROM {table} WHERE scope_key=?", (skey,)
            ).fetchone()
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
        skey = scope.key()
        tables = [_KIND_TABLE[kind]] if kind is not None else list(_KIND_TABLE.values())
        status_filter = where.get("status") if where else None
        min_trust = where.get("min_trust") if where else None
        scored: list[tuple[float, sqlite3.Row]] = []
        for table in tables:
            sql = f"SELECT * FROM {table} WHERE scope_key=? AND embedding IS NOT NULL"
            params: list[object] = [skey]
            if status_filter is not None:
                sql += " AND status=?"
                params.append(status_filter)
            if min_trust is not None:
                sql += " AND trust_tier>=?"
                params.append(int(min_trust))
            for row in self._conn.execute(sql, params).fetchall():
                stored = json.loads(row["embedding"])
                if len(stored) != qdim:
                    # Vectors from a different embedder/dim (e.g. after a model
                    # swap) are not comparable — skip them rather than crash, so
                    # keyword recall still works while vectors are re-embedded.
                    continue
                scored.append((cosine(query_vec, stored), row))
        scored.sort(key=lambda item: item[0], reverse=True)
        hits = tuple(
            QueryHit(record=self._row_to_record(row), score=score)
            for score, row in scored[: max(0, k)]
        )
        return QueryResult(hits=hits)

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
        match = _fts_query(text)
        if match is None:
            return QueryResult(hits=())
        status_filter = where.get("status") if where else None
        min_trust = where.get("min_trust") if where else None
        has_filter = status_filter is not None or min_trust is not None
        sql = "SELECT rid, bm25(fts) AS s FROM fts WHERE fts MATCH ? AND scope_key=?"
        params: list[object] = [match, scope.key()]
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
        records = {
            r.id: r for r in self.get(scope=scope, ids=[row["rid"] for row in rows]).records
        }
        hits: list[QueryHit] = []
        for row in rows:
            record = records.get(row["rid"])
            if record is None:
                continue
            if status_filter is not None and record.status.value != status_filter:
                continue
            if min_trust is not None and int(record.trust_tier) < int(min_trust):
                continue
            hits.append(QueryHit(record=record, score=-float(row["s"])))  # bm25: lower is better
            if len(hits) >= k:
                break
        return QueryResult(hits=tuple(hits))

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
