"""Embedder discovery: explicit registration + ``rekoll.embedders`` entry points.

Mirrors the storage-adapter registry (``rekoll.adapters.registry``): resolution
order is explicit ``register_embedder`` > entry point > built-in. The spec
grammar is ``"name"`` or ``"name:model"``::

    Memory(embedder="openai:text-embedding-3-small")   # cloud — explicit opt-in
    Memory(embedder="fastembed")                       # local ONNX (the auto default)
    Memory(embedder="stub:32")                         # dependency-free stub

Cloud names import :mod:`rekoll.providers` LAZILY and only when named. The
default path (``Memory()`` with no arguments) never calls this module at all,
so no environment variable is read and no socket opened without explicit
opt-in (CI-gated in tests/test_invariants.py).

Third-party packages add embedders with no core change::

    [project.entry-points."rekoll.embedders"]
    myprovider = "my_pkg:MyEmbedder"   # called as MyEmbedder(model_or_None, **kwargs)
"""

from __future__ import annotations

from importlib import metadata
from typing import Callable, Dict, Optional

from .embedding import Embedder, FastEmbedEmbedder, StubEmbedder

__all__ = ["register_embedder", "get_embedder", "available_embedders"]

_REGISTRY: Dict[str, Callable[..., Embedder]] = {}

# Preset names served by OpenAICompatibleEmbedder. Duplicated from
# rekoll.providers.openai_compat.PRESETS so that resolving a LOCAL name never
# imports the providers package; a test pins the two sets equal.
_OPENAI_COMPAT_NAMES = frozenset({
    "openai", "deepseek", "qwen", "minimax", "moonshot", "kimi", "mistral",
    "xai", "openrouter", "anthropic", "groq", "ollama", "lmstudio", "custom",
})
_LOCAL_BUILTINS = frozenset({"stub", "fastembed"})
_PROVIDER_BUILTINS = frozenset({"gemini", "voyage"}) | _OPENAI_COMPAT_NAMES


def register_embedder(name: str, factory: Callable[..., Embedder]) -> None:
    """Register ``factory``, called as ``factory(model_or_None, **kwargs)``."""
    if ":" in name:
        raise ValueError("embedder names must not contain ':' (it separates name from model)")
    _REGISTRY[name] = factory


def _entry_points() -> Dict[str, metadata.EntryPoint]:
    found: Dict[str, metadata.EntryPoint] = {}
    try:
        for ep in metadata.entry_points(group="rekoll.embedders"):
            found[ep.name] = ep
    except Exception:  # pragma: no cover - importlib metadata edge cases
        pass
    return found


def _builtin(name: str, model: Optional[str], kwargs: dict) -> Embedder:
    if name == "stub":
        if model is not None:
            try:
                kwargs.setdefault("dim", int(model))
            except ValueError:
                raise ValueError(f"stub takes a numeric dim, e.g. 'stub:64' (got {model!r})") from None
        return StubEmbedder(**kwargs)
    if name == "fastembed":
        if model is not None:
            kwargs.setdefault("model_name", model)
        return FastEmbedEmbedder(**kwargs)
    if name == "gemini":
        from .providers.gemini import GeminiEmbedder  # lazy: explicit opt-in only

        return GeminiEmbedder(model, **kwargs)
    if name == "voyage":
        from .providers.voyage import VoyageEmbedder  # lazy: explicit opt-in only

        return VoyageEmbedder(model, **kwargs)
    if name in _OPENAI_COMPAT_NAMES:
        from .providers.openai_compat import OpenAICompatibleEmbedder  # lazy: explicit opt-in only

        kwargs.setdefault("provider", name)
        return OpenAICompatibleEmbedder(model, **kwargs)
    raise KeyError(f"no embedder named {name!r}; known embedders: {available_embedders()}")


def get_embedder(spec: str, **kwargs: object) -> Embedder:
    """Resolve ``"name"`` / ``"name:model"`` to a constructed embedder."""
    if not isinstance(spec, str) or not spec.strip():
        raise ValueError("embedder spec must be a non-empty string like 'openai:text-embedding-3-small'")
    name, _, model_part = spec.partition(":")
    name = name.strip()
    model: Optional[str] = model_part.strip() or None
    if name in _REGISTRY:
        return _REGISTRY[name](model, **kwargs)
    eps = _entry_points()
    if name in eps:
        factory = eps[name].load()
        return factory(model, **kwargs)
    return _builtin(name, model, dict(kwargs))


def available_embedders() -> list[str]:
    return sorted(set(_REGISTRY) | set(_entry_points()) | _LOCAL_BUILTINS | _PROVIDER_BUILTINS)
