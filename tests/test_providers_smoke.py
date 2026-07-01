"""OPTIONAL real-provider smoke tests — opt in with ``REKOLL_SMOKE=1``.

These are the ONLY tests that may leave the machine. Each needs BOTH the
explicit ``REKOLL_SMOKE=1`` opt-in AND that provider's key env var, so an
ambient (possibly stale) key in a dev shell can never make the offline suite
run — or fail on — real network calls. CI sets neither. Run locally, e.g.::

    REKOLL_SMOKE=1 OPENAI_API_KEY=sk-... pytest tests/test_providers_smoke.py -v
"""

from __future__ import annotations

import os

import pytest


def requires(var: str):
    wanted = os.environ.get("REKOLL_SMOKE") == "1" and os.environ.get(var)
    return pytest.mark.skipif(
        not wanted, reason=f"needs REKOLL_SMOKE=1 and {var} (real-key smoke test)"
    )


@requires("OPENAI_API_KEY")
def test_openai_embed_smoke():
    from rekoll.providers import OpenAICompatibleEmbedder

    emb = OpenAICompatibleEmbedder()  # text-embedding-3-small
    vectors = emb.embed(["rekoll smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) == emb.dim > 100
    assert emb.identity().name == "openai:text-embedding-3-small"


@requires("GEMINI_API_KEY")
def test_gemini_embed_smoke():
    from rekoll.providers import GeminiEmbedder

    emb = GeminiEmbedder()
    vectors = emb.embed(["rekoll smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) == emb.dim > 100
    assert emb.identity().name.startswith("gemini:")


@requires("VOYAGE_API_KEY")
def test_voyage_embed_smoke():
    from rekoll.providers import VoyageEmbedder

    emb = VoyageEmbedder()
    vectors = emb.embed(["rekoll smoke test"])
    assert len(vectors) == 1
    assert len(vectors[0]) == emb.dim > 100
    assert emb.identity().name.startswith("voyage:")


@requires("OPENAI_API_KEY")
def test_openai_consolidator_smoke():
    from rekoll.providers import OpenAICompatibleConsolidator

    consolidator = OpenAICompatibleConsolidator("gpt-4o-mini", max_tokens=64)
    summary = consolidator.summarize(
        ["the team chose postgres over bigquery for cost",
         "the deploy runs nightly on a small vps"]
    )
    assert isinstance(summary, str) and summary.strip()
