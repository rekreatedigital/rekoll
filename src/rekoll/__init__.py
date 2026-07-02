"""Rekoll — injection-hardened, storage-agnostic, private memory for AI agents.

Public surface shipped today: the memory record model, the storage-adapter
contract + reference SQLite adapter (via the registry), local embeddings +
structure-aware chunking, hybrid (vector + lexical) retrieval with optional
cross-encoder reranking, the injection firewall (ingest screen + read envelope),
the importable conformance suite, the high-level ``Memory`` facade, and the
opt-in BYO-AI layer (cloud embedders via ``rekoll.providers`` + the write-side
``Memory.consolidate`` seam). The full learning loop and additional backends
arrive in later phases (see docs/DESIGN.md).
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
from .consolidation import Consolidator
from .embedders import available_embedders, get_embedder, register_embedder
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
from .ledger import RecallLedger
from .memory import HealthReport, Memory, RecallResult
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
    # embedder registry (BYO-AI is opt-in; rekoll.providers itself is lazy)
    "register_embedder",
    "get_embedder",
    "available_embedders",
    # write-side consolidation seam (the only place LLM output can enter)
    "Consolidator",
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
    # memory-quality loop (was-it-used, freshness, honest degradation)
    "RecallLedger",
    "HealthReport",
]
