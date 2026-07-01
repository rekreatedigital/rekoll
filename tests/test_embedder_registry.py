"""The embedder registry: spec grammar, resolution order, and lazy opt-in."""

from __future__ import annotations

import sys

import pytest

import rekoll.embedders as embedders_module
from rekoll import Memory, available_embedders, get_embedder, register_embedder
from rekoll.embedding import StubEmbedder


@pytest.fixture(autouse=True)
def _hermetic(monkeypatch, scrub_provider_env):
    """Isolate explicit registrations per test; keep env key-free."""
    monkeypatch.setattr(embedders_module, "_REGISTRY", dict(embedders_module._REGISTRY))


def test_stub_spec_with_and_without_dim():
    assert isinstance(get_embedder("stub"), StubEmbedder)
    assert get_embedder("stub").dim == 64
    assert get_embedder("stub:32").dim == 32
    with pytest.raises(ValueError, match="numeric dim"):
        get_embedder("stub:abc")


def test_unknown_name_lists_known_embedders():
    with pytest.raises(KeyError, match="known embedders"):
        get_embedder("nope")


def test_blank_spec_rejected():
    with pytest.raises(ValueError, match="non-empty"):
        get_embedder("  ")


def test_explicit_registration_wins_over_builtin():
    marker = StubEmbedder(dim=7)
    register_embedder("stub", lambda model=None, **kwargs: marker)
    assert get_embedder("stub") is marker


def test_register_rejects_colon_names():
    with pytest.raises(ValueError, match="':'"):
        register_embedder("bad:name", lambda model=None, **kwargs: StubEmbedder())


def test_entry_point_resolution(monkeypatch):
    calls = {}

    class FakeEntryPoint:
        name = "acme"

        @staticmethod
        def load():
            def factory(model=None, **kwargs):
                calls["model"] = model
                calls["kwargs"] = kwargs
                return StubEmbedder(dim=5)

            return factory

    monkeypatch.setattr(embedders_module, "_entry_points", lambda: {"acme": FakeEntryPoint})
    emb = get_embedder("acme:tiny-model", extra="yes")
    assert emb.dim == 5
    assert calls == {"model": "tiny-model", "kwargs": {"extra": "yes"}}
    assert "acme" in available_embedders()


def test_local_specs_never_import_providers(monkeypatch):
    """Resolving LOCAL names must not touch rekoll.providers; a cloud name may."""
    for name in [m for m in sys.modules if m.startswith("rekoll.providers")]:
        monkeypatch.delitem(sys.modules, name)
    get_embedder("stub:16")
    assert not any(m.startswith("rekoll.providers") for m in sys.modules)
    get_embedder("openai:m", api_key="k")  # explicit opt-in → NOW it loads
    assert any(m.startswith("rekoll.providers") for m in sys.modules)


def test_openai_compat_name_sets_stay_in_sync():
    from rekoll.providers.openai_compat import PRESETS

    assert embedders_module._OPENAI_COMPAT_NAMES == set(PRESETS)


def test_spec_binds_the_preset_provider():
    emb = get_embedder("deepseek:some-embed-model", api_key="k")
    assert emb.provider == "deepseek"
    assert emb.model == "some-embed-model"


def test_available_embedders_contains_marquee_names():
    names = available_embedders()
    for expected in ("stub", "fastembed", "openai", "gemini", "voyage", "ollama", "custom"):
        assert expected in names


def test_memory_accepts_spec_strings():
    mem = Memory(path=":memory:", embedder="stub:32", reranker=None)
    assert isinstance(mem.embedder, StubEmbedder)
    assert mem.embedder.dim == 32
    mem.remember("registry spec strings work")
    assert mem.recall("registry", k=1).texts()
    mem.close()


def test_memory_with_registered_cloud_factory(fake_provider):
    """The third-party extension story, end to end against the fake server."""
    from rekoll.providers import OpenAICompatibleEmbedder

    register_embedder(
        "faketest",
        lambda model=None, **kwargs: OpenAICompatibleEmbedder(
            model, api_key="k", base_url=fake_provider.base_url, **kwargs
        ),
    )
    mem = Memory(path=":memory:", embedder="faketest:emb-x", reranker=None)
    mem.remember("cloud embedded memory")
    assert mem.recall("cloud memory", k=1).texts() == ["cloud embedded memory"]
    ident = mem.adapter.get_embedder_identity(scope=mem.scope)
    assert ident is not None and ident.name == "openai:emb-x"
    mem.close()
