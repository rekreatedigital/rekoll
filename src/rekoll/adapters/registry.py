"""Adapter discovery: explicit registration + ``rekoll.adapters`` entry points.

Resolution order: explicit ``register_adapter`` > entry point > built-in fallback.
This lets third parties ship a backend (``pip install rekoll-postgres``) that
Rekoll finds without any core change.
"""

from __future__ import annotations

from importlib import metadata
from typing import Callable, Dict

from .base import StorageAdapter

__all__ = ["register_adapter", "get_adapter", "available_adapters"]

_REGISTRY: Dict[str, Callable[..., StorageAdapter]] = {}


def register_adapter(name: str, factory: Callable[..., StorageAdapter]) -> None:
    _REGISTRY[name] = factory


def _entry_points() -> Dict[str, metadata.EntryPoint]:
    found: Dict[str, metadata.EntryPoint] = {}
    try:
        for ep in metadata.entry_points(group="rekoll.adapters"):
            found[ep.name] = ep
    except Exception:  # pragma: no cover - importlib metadata edge cases
        pass
    return found


def get_adapter(name: str, **kwargs: object) -> StorageAdapter:
    if name in _REGISTRY:
        return _REGISTRY[name](**kwargs)
    eps = _entry_points()
    if name in eps:
        factory = eps[name].load()
        return factory(**kwargs)
    if name == "sqlite":  # built-in fallback when not pip-installed
        from .sqlite import SQLiteAdapter

        return SQLiteAdapter(**kwargs)
    known = sorted(set(_REGISTRY) | set(eps) | {"sqlite"})
    raise KeyError(f"no adapter named {name!r}; known adapters: {known}")


def available_adapters() -> list[str]:
    return sorted(set(_REGISTRY) | set(_entry_points()) | {"sqlite"})
