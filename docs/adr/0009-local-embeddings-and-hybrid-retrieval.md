# ADR-0009 — Local embeddings (fastembed, optional extra) + hybrid RRF retrieval

**Status:** Accepted · **Date:** 2026-06-23

## Context
P1 makes retrieval real. Two choices: which embedding model/library, and how to
rank. Constraints: local-and-private by default (ADR-0007/0008), light installs
for non-technical users, and the P0 promise that the *core library imports with
zero required dependencies*.

## Decision
- **Default embedder: `fastembed`** (ONNX runtime, no PyTorch) with a small local
  English model. Chosen over `sentence-transformers` (drags in torch, heavy) and
  raw `onnxruntime` (more glue). It is an **optional extra**: `pip install
  rekoll[embeddings]`. The core still imports without it; `StubEmbedder` keeps the
  no-extra path (and all tests) working with zero network/deps.
- **Retrieval is hybrid and zero-LLM**: vector (cosine) + lexical (SQLite FTS5
  BM25) fused by **Reciprocal Rank Fusion (k=60)**. The verbatim store is the
  always-on floor; lexical is an additive arm advertised via the `lexical`
  capability. RRF avoids needing comparable raw scores across arms.
- The SQLite reference adapter now advertises and implements `lexical` via an FTS5
  mirror kept in sync on every write/delete.

## Consequences
- "Bring any model" still holds for the *learning* slot; the *embedding* slot
  defaults local. A hosted-embedding option can be added later behind the same
  `Embedder` protocol.
- Tests and CI run on the stub (no network, no model download); a separate,
  skippable check exercises fastembed when installed.
- Model identity (name + dim + config_hash) flows through the existing
  embedder-identity guard, so swapping models can't silently corrupt a scope.
