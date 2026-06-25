"""Storage adapters: the bring-your-own-database seam.

The core depends only on the :class:`~rekoll.adapters.base.StorageAdapter` ABC.
Concrete backends (SQLite, Postgres, ...) implement it and are discovered via
the ``rekoll.adapters`` entry-point group or registered explicitly.
"""

from .base import (
    CAP_LEXICAL,
    CAP_RELATIONAL,
    CAP_VECTOR,
    GetResult,
    QueryHit,
    QueryResult,
    StorageAdapter,
    UnsupportedCapabilityError,
)

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
