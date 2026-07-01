"""Cloud embedder providers, tested OFFLINE against the local fake server.

No test here needs a real key or leaves loopback; real-provider smoke tests
live in test_providers_smoke.py (skip-unless-env-var).
"""

from __future__ import annotations

import socket

import pytest

from rekoll import Memory
from rekoll.providers import (
    GeminiEmbedder,
    OpenAICompatibleEmbedder,
    ProviderError,
    VoyageEmbedder,
)


@pytest.fixture(autouse=True)
def _hermetic_env(scrub_provider_env):
    """Key resolution below must see only what each test sets."""


# -- OpenAI-compatible ---------------------------------------------------------


def test_embed_round_trip_shape_and_order(fake_provider):
    emb = OpenAICompatibleEmbedder("emb-x", api_key="sk-test", base_url=fake_provider.base_url)
    vectors = emb.embed(["alpha", "beta"])
    # The fake server returns items REVERSED (with index fields) — getting the
    # right order back proves index-based reassembly, not list-order trust.
    assert vectors == [fake_provider.vector_for("alpha"), fake_provider.vector_for("beta")]
    request = fake_provider.requests[0]
    assert request["path"] == "/v1/embeddings"
    assert request["json"]["model"] == "emb-x"
    assert request["json"]["input"] == ["alpha", "beta"]
    assert request["headers"]["authorization"] == "Bearer sk-test"


def test_construction_opens_no_socket(monkeypatch):
    """Opt-in construction ≠ egress: no DNS, no connect until first use —
    for every provider class AND the registry path (the ADR-0015 claim)."""
    from rekoll import get_embedder
    from rekoll.providers import OpenAICompatibleConsolidator

    offenders: list[str] = []
    real_connect = socket.socket.connect
    real_getaddrinfo = socket.getaddrinfo
    monkeypatch.setattr(
        socket.socket, "connect",
        lambda self, address: (offenders.append(f"connect:{address}"), real_connect(self, address)),
    )
    monkeypatch.setattr(
        socket, "getaddrinfo",
        lambda host, *a, **kw: (offenders.append(f"dns:{host}"), real_getaddrinfo(host, *a, **kw))[1],
    )
    OpenAICompatibleEmbedder("m", api_key="k")
    GeminiEmbedder(api_key="k")
    VoyageEmbedder(api_key="k")
    OpenAICompatibleConsolidator("m", api_key="k")
    get_embedder("openai:m", api_key="k")
    get_embedder("gemini:g", api_key="k")
    get_embedder("voyage:v", api_key="k")
    get_embedder("deepseek:d", api_key="k")
    assert offenders == []


def test_explicit_key_beats_env(monkeypatch, fake_provider):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    emb = OpenAICompatibleEmbedder("m", api_key="arg-key", base_url=fake_provider.base_url)
    emb.embed(["x"])
    assert fake_provider.requests[0]["headers"]["authorization"] == "Bearer arg-key"


def test_env_key_fallback_when_provider_named(monkeypatch, fake_provider):
    monkeypatch.setenv("OPENAI_API_KEY", "env-key")
    emb = OpenAICompatibleEmbedder("m", base_url=fake_provider.base_url)
    emb.embed(["x"])
    assert fake_provider.requests[0]["headers"]["authorization"] == "Bearer env-key"


def test_missing_key_raises_naming_the_env_var():
    with pytest.raises(ValueError, match="DEEPSEEK_API_KEY"):
        OpenAICompatibleEmbedder("m", provider="deepseek")


def test_keyless_local_presets_send_no_auth_header(fake_provider):
    emb = OpenAICompatibleEmbedder("nomic-embed-text", provider="ollama", base_url=fake_provider.base_url)
    emb.embed(["x"])
    assert "authorization" not in fake_provider.requests[0]["headers"]


def test_custom_provider_requires_base_url():
    with pytest.raises(ValueError, match="base_url"):
        OpenAICompatibleEmbedder("m", provider="custom")


def test_unknown_provider_lists_known():
    with pytest.raises(ValueError, match="known providers"):
        OpenAICompatibleEmbedder("m", provider="nope")


def test_default_model_is_openai_only():
    assert OpenAICompatibleEmbedder(api_key="k").model == "text-embedding-3-small"
    with pytest.raises(ValueError, match="pass a model"):
        OpenAICompatibleEmbedder(provider="qwen", api_key="k")


def test_dim_probes_lazily_then_caches(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url)
    assert fake_provider.requests == []  # construction made no call
    assert emb.dim == fake_provider.dim
    assert len(fake_provider.requests) == 1
    assert emb.dim == fake_provider.dim
    assert len(fake_provider.requests) == 1  # cached, no second probe


def test_identity_is_truthful(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url)
    ident = emb.identity()
    assert ident.name == "openai:m"
    assert ident.dim == fake_provider.dim
    same = OpenAICompatibleEmbedder("m", api_key="other", base_url=fake_provider.base_url)
    assert same.identity() == ident  # key is NOT part of identity
    other_model = OpenAICompatibleEmbedder("m2", api_key="k", base_url=fake_provider.base_url)
    assert other_model.identity().config_hash != ident.config_hash
    other_url = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url + "/x")
    assert other_url.identity().config_hash != ident.config_hash


def test_dimensions_param_passes_through_and_fixes_dim(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url, dimensions=4)
    assert emb.dim == 4  # known upfront: no probe request
    assert fake_provider.requests == []
    vectors = emb.embed(["alpha"])
    assert fake_provider.requests[0]["json"]["dimensions"] == 4
    assert len(vectors[0]) == 4


def test_declared_dim_mismatch_raises(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url, dim=16)
    with pytest.raises(ProviderError, match="identity guard"):
        emb.embed(["alpha"])


def test_retries_on_429_with_retry_after(fake_provider):
    fake_provider.fail_next(429, headers={"Retry-After": "0"})
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url)
    assert emb.embed(["alpha"]) == [fake_provider.vector_for("alpha")]
    assert len(fake_provider.requests) == 2


def test_retries_exhausted_raise_with_status(fake_provider):
    fake_provider.fail_next(500, times=3, headers={"Retry-After": "0"})
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url, retries=2)
    with pytest.raises(ProviderError) as excinfo:
        emb.embed(["alpha"])
    assert excinfo.value.status == 500
    assert len(fake_provider.requests) == 3


def test_error_message_never_contains_the_key(fake_provider):
    fake_provider.fail_next(401)
    emb = OpenAICompatibleEmbedder("m", api_key="sk-supersecret12345", base_url=fake_provider.base_url)
    with pytest.raises(ProviderError) as excinfo:
        emb.embed(["alpha"])
    assert "sk-supersecret12345" not in str(excinfo.value)
    assert excinfo.value.status == 401


def test_404_gets_capability_hint_for_chat_only_presets(fake_provider):
    fake_provider.fail_next(404)
    emb = OpenAICompatibleEmbedder("m", provider="deepseek", api_key="k", base_url=fake_provider.base_url)
    with pytest.raises(ProviderError, match="may not offer an embeddings endpoint"):
        emb.embed(["alpha"])


def test_batching_splits_requests_and_preserves_order(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url, batch_size=2)
    texts = ["a", "b", "c", "d", "e"]
    vectors = emb.embed(texts)
    assert len(fake_provider.requests) == 3
    assert vectors == [fake_provider.vector_for(t) for t in texts]


def test_empty_input_makes_no_request(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url)
    assert emb.embed([]) == []
    assert fake_provider.requests == []


def test_memory_end_to_end_with_cloud_embedder(fake_provider):
    emb = OpenAICompatibleEmbedder("m", api_key="k", base_url=fake_provider.base_url)
    mem = Memory(path=":memory:", embedder=emb, reranker=None)
    assert mem.adapter.get_embedder_identity(scope=mem.scope) == emb.identity()
    mem.remember("we chose postgres over bigquery for cost")
    mem.remember("the deploy runs nightly on a vps")
    hits = mem.recall("postgres cost", k=1)
    assert len(hits) == 1
    assert "postgres" in hits.texts()[0]
    mem.close()


# -- Gemini ---------------------------------------------------------------------


def test_gemini_request_shape_and_header_auth(fake_provider):
    emb = GeminiEmbedder(api_key="g-key", base_url=fake_provider.root_url)
    vectors = emb.embed(["alpha", "beta"])
    assert vectors == [fake_provider.vector_for("alpha"), fake_provider.vector_for("beta")]
    request = fake_provider.requests[0]
    assert request["path"] == "/models/gemini-embedding-001:batchEmbedContents"
    assert "key=" not in request["path"]  # never the query param — it leaks into logs
    assert request["headers"]["x-goog-api-key"] == "g-key"
    first = request["json"]["requests"][0]
    assert first["model"] == "models/gemini-embedding-001"
    assert first["content"]["parts"] == [{"text": "alpha"}]


def test_gemini_env_order_gemini_key_wins(monkeypatch, fake_provider):
    monkeypatch.setenv("GOOGLE_API_KEY", "google-key")
    emb = GeminiEmbedder(base_url=fake_provider.root_url)
    emb.embed(["x"])
    assert fake_provider.requests[0]["headers"]["x-goog-api-key"] == "google-key"
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-key")
    emb2 = GeminiEmbedder(base_url=fake_provider.root_url)
    emb2.embed(["x"])
    assert fake_provider.requests[1]["headers"]["x-goog-api-key"] == "gemini-key"


def test_gemini_missing_key_names_both_vars():
    with pytest.raises(ValueError, match="GEMINI_API_KEY.*GOOGLE_API_KEY"):
        GeminiEmbedder()


def test_gemini_output_dimensionality(fake_provider):
    emb = GeminiEmbedder(api_key="k", base_url=fake_provider.root_url, output_dimensionality=4)
    assert emb.dim == 4  # known upfront, no probe
    vectors = emb.embed(["alpha"])
    assert fake_provider.requests[0]["json"]["requests"][0]["outputDimensionality"] == 4
    assert len(vectors[0]) == 4


def test_gemini_model_prefix_normalized(fake_provider):
    emb = GeminiEmbedder("models/text-embedding-004", api_key="k", base_url=fake_provider.root_url)
    assert emb.model == "text-embedding-004"
    assert emb.identity().name == "gemini:text-embedding-004"


def test_gemini_batches_at_batch_size(fake_provider):
    emb = GeminiEmbedder(api_key="k", base_url=fake_provider.root_url, batch_size=2)
    emb.embed(["a", "b", "c"])
    assert len(fake_provider.requests) == 2
    assert len(fake_provider.requests[0]["json"]["requests"]) == 2
    assert len(fake_provider.requests[1]["json"]["requests"]) == 1


def test_gemini_task_type_passthrough(fake_provider):
    emb = GeminiEmbedder(api_key="k", base_url=fake_provider.root_url, task_type="RETRIEVAL_DOCUMENT")
    emb.embed(["alpha"])
    assert fake_provider.requests[0]["json"]["requests"][0]["taskType"] == "RETRIEVAL_DOCUMENT"
    plain = GeminiEmbedder(api_key="k", base_url=fake_provider.root_url)
    plain.embed(["alpha"])
    assert "taskType" not in fake_provider.requests[1]["json"]["requests"][0]


# -- Voyage ----------------------------------------------------------------------


def test_voyage_request_shape(fake_provider):
    emb = VoyageEmbedder(api_key="v-key", base_url=fake_provider.base_url)
    vectors = emb.embed(["alpha"])
    assert vectors == [fake_provider.vector_for("alpha")]
    request = fake_provider.requests[0]
    assert request["path"] == "/v1/embeddings"
    assert request["json"]["model"] == "voyage-3.5"  # the documented default
    assert request["headers"]["authorization"] == "Bearer v-key"
    assert "input_type" not in request["json"]  # symmetric default


def test_voyage_input_type_and_output_dimension(fake_provider):
    emb = VoyageEmbedder(
        "voyage-3-large", api_key="k", base_url=fake_provider.base_url,
        input_type="document", output_dimension=4,
    )
    assert emb.dim == 4
    emb.embed(["alpha"])
    payload = fake_provider.requests[0]["json"]
    assert payload["input_type"] == "document"
    assert payload["output_dimension"] == 4


def test_voyage_env_key_and_missing_key_message(monkeypatch, fake_provider):
    with pytest.raises(ValueError, match="VOYAGE_API_KEY"):
        VoyageEmbedder()
    monkeypatch.setenv("VOYAGE_API_KEY", "v-env")
    emb = VoyageEmbedder(base_url=fake_provider.base_url)
    emb.embed(["x"])
    assert fake_provider.requests[0]["headers"]["authorization"] == "Bearer v-env"


def test_voyage_identity_name():
    emb = VoyageEmbedder(api_key="k", dim=1024)
    assert emb.identity().name == "voyage:voyage-3.5"
    assert emb.identity().dim == 1024
