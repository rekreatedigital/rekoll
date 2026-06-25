"""Rekoll — injection-hardened, storage-agnostic, private memory for AI agents.

P0 (foundation) public surface: the memory record model, the storage-adapter
contract, the reference SQLite adapter (via the registry), the local embedder,
and the importable conformance suite. Retrieval, the injection firewall, and the
learning loop arrive in later phases (see docs/DESIGN.md).
"""

from __future__ import annotations

from ._version import __version__
from .adapters.base import (
    CAP_LEXICAL,
    CAP_RELATIONAL,
    CAP_VECTOR,
    GetResult,
    QueryHit,
    QueryResult,
    StorageAdapter,
    UnsupportedCapabilityError,
)
from .adapters.registry import available_adapters, get_adapter, register_adapter
from .chunking import chunk_file, chunk_markdown, chunk_text
from .embedding import (
    Embedder,
    EmbedderIdentity,
    EmbedderIdentityMismatch,
    FastEmbedEmbedder,
    StubEmbedder,
    compare_identity,
    cosine,
    guard_identity,
)
from .evaluation import EvalResult, LabeledQuery, evaluate, recall_at_k, reciprocal_rank
from .firewall import (
    ContextEnvelope,
    DefenseAction,
    DefenseDecision,
    build_envelope,
    sanitize_unicode,
    screen,
    screened_record,
)
from .memory import Memory, RecallResult
from .reranking import CrossEncoderReranker, Reranker
from .retrieval import hybrid_search, rrf_fuse
from .ids import content_hash, human_id, normalize_content, record_id
from .model import Kind, MemoryRecord, Provenance, Scalar, Scope, Status, TrustTier

__all__ = [
    "__version__",
    # model
    "MemoryRecord",
    "Kind",
    "TrustTier",
    "Status",
    "Scope",
    "Provenance",
    "Scalar",
    # ids
    "content_hash",
    "record_id",
    "human_id",
    "normalize_content",
    # embedding
    "Embedder",
    "EmbedderIdentity",
    "StubEmbedder",
    "EmbedderIdentityMismatch",
    "compare_identity",
    "guard_identity",
    "cosine",
    # storage
    "StorageAdapter",
    "QueryHit",
    "QueryResult",
    "GetResult",
    "UnsupportedCapabilityError",
    "CAP_VECTOR",
    "CAP_LEXICAL",
    "CAP_RELATIONAL",
    "register_adapter",
    "get_adapter",
    "available_adapters",
    # retrieval (P1)
    "FastEmbedEmbedder",
    "chunk_text",
    "chunk_markdown",
    "chunk_file",
    "hybrid_search",
    "rrf_fuse",
    "Reranker",
    "CrossEncoderReranker",
    # evaluation (P1)
    "LabeledQuery",
    "EvalResult",
    "evaluate",
    "recall_at_k",
    "reciprocal_rank",
    # firewall (P2)
    "DefenseAction",
    "DefenseDecision",
    "screen",
    "screened_record",
    "sanitize_unicode",
    "ContextEnvelope",
    "build_envelope",
    # facade — the drop-in SDK
    "Memory",
    "RecallResult",
]
