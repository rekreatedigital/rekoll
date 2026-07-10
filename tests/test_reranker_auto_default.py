"""Issue #37 / ADR-0029: ``reranker='auto'`` attaches the cross-encoder ONLY
when the scope is degraded to lexical-only by an embedder mismatch — its one
measured win (MRR +0.158, p=2.6e-03) — and is OFF in normal hybrid, where the
ablation found +60% read latency for no detectable lift.

Dependency-independent: ``rekoll.memory._auto_reranker`` is monkeypatched to a
call-counting passthrough, so these tests discriminate the POLICY without the
optional ``embeddings`` extra and without downloading a model. Two assertions
here FAIL on pre-fix main, where 'auto' resolves eagerly and is frozen at
construction:

 - ``test_auto_off_in_normal_hybrid...``: main attaches the reranker (and loads
   the model) in normal hybrid;
 - ``test_auto_decision_is_dynamic_across_reindex``: main's frozen choice does
   not follow reindex() clearing the mismatch.
"""

from __future__ import annotations

import warnings

import pytest

from rekoll import Memory
from rekoll.embedding import StubEmbedder
from rekoll.reranking import Reranker


class _CountingAutoReranker:
    """A passthrough Reranker whose factory records how many times it was built,
    so a test can prove the model is NEVER constructed on the normal-hybrid path."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self):  # stands in for memory._auto_reranker()
        self.calls += 1
        return self

    def rerank(self, query, hits, *, top=None):
        hits = list(hits)
        return hits[: top if top is not None else len(hits)]


def _pin_auto(monkeypatch) -> _CountingAutoReranker:
    sentinel = _CountingAutoReranker()
    monkeypatch.setattr("rekoll.memory._auto_reranker", sentinel)
    return sentinel


def _mismatched(tmp_path, monkeypatch, **kwargs) -> Memory:
    """A scope written dim=64, reopened dim=128 under reranker='auto' — mismatch."""
    db = str(tmp_path / "mismatch.db")
    first = Memory(path=db, embedder=StubEmbedder(dim=64), reranker=None)
    first.remember("alpha fact about postgres pooling written before the swap")
    first.close()
    kwargs.setdefault("embedder", StubEmbedder(dim=128))
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        # reranker defaults to 'auto' — deliberately not passed.
        return Memory(path=db, **kwargs)


# -- 1. normal hybrid: auto is OFF, and the model is never even loaded ---------


def test_auto_off_in_normal_hybrid_and_never_loads_the_model(monkeypatch):
    sentinel = _pin_auto(monkeypatch)
    mem = Memory(path=":memory:", embedder=StubEmbedder())  # reranker='auto' default
    # DISCRIMINATING vs pre-fix main (which froze sentinel at construction):
    assert mem.reranker is None
    mem.remember("the deploy pipeline promotes builds from staging to production")
    res = mem.recall("deploy pipeline staging", k=2)
    assert res.mode == "vector+lexical (stub-embedder)"
    assert "rerank" not in res.mode
    assert mem.health(n=1).mode == "vector+lexical (stub-embedder)"
    # The +60%-latency model was never constructed on the normal path.
    assert sentinel.calls == 0
    mem.close()


# -- 2. degraded (lexical-only): auto turns ON, mode says so -------------------


def test_auto_on_when_degraded_to_lexical_only(tmp_path, monkeypatch):
    sentinel = _pin_auto(monkeypatch)
    mem = _mismatched(tmp_path, monkeypatch)
    assert isinstance(mem.reranker, Reranker)
    assert mem.reranker is sentinel
    assert mem.recall("postgres pooling", k=3).mode == "lexical-only+rerank: embedder mismatch"
    assert mem.health(n=1).mode == "lexical-only+rerank: embedder mismatch"
    assert sentinel.calls >= 1  # built lazily, only because the scope is degraded
    mem.close()


# -- 3. the decision is DYNAMIC: reindex() clearing the mismatch turns it off --


def test_auto_decision_is_dynamic_across_reindex(tmp_path, monkeypatch):
    sentinel = _pin_auto(monkeypatch)
    mem = _mismatched(tmp_path, monkeypatch)
    assert mem.reranker is sentinel  # degraded -> attached
    mem.reindex()  # re-embed + reclaim the scope: identity is now a match
    # DISCRIMINATING vs pre-fix main (frozen at init would still be the sentinel):
    assert mem.reranker is None
    assert mem.recall("postgres pooling", k=3).mode == "vector+lexical (stub-embedder)"
    mem.close()


# -- 4. explicit reranker= is always honored, identically to before ------------


def test_explicit_reranker_always_honored_in_both_states(tmp_path, monkeypatch):
    sentinel = _pin_auto(monkeypatch)

    class _Explicit:
        def rerank(self, query, hits, *, top=None):
            return list(hits)[: top if top is not None else len(hits)]

    explicit = _Explicit()
    # Normal hybrid: an explicit reranker attaches even though auto would not.
    mem = Memory(path=":memory:", embedder=StubEmbedder(), reranker=explicit)
    assert mem.reranker is explicit
    mem.remember("cache is invalidated by content hash")
    assert mem.recall("cache invalidated", k=2).mode == "vector+lexical+rerank (stub-embedder)"
    mem.close()

    # Explicit None is honored even in a degraded scope (auto would attach there).
    mem2 = _mismatched(tmp_path, monkeypatch, reranker=None)
    assert mem2.reranker is None
    assert mem2.recall("postgres pooling", k=3).mode == "lexical-only: embedder mismatch"
    mem2.close()

    # The auto factory was never consulted for any explicit construction.
    assert sentinel.calls == 0


# -- 5. reranker is read-only: the live decision is not a settable attribute ---


def test_reranker_attribute_is_read_only(monkeypatch):
    _pin_auto(monkeypatch)
    mem = Memory(path=":memory:", embedder=StubEmbedder())
    with pytest.raises(AttributeError):
        mem.reranker = object()
    mem.close()
