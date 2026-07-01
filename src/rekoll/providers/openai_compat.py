"""OpenAI-compatible embeddings: one class, many providers (ADR-0015).

Most hosted AI platforms — and local servers like Ollama / LM Studio — speak
OpenAI's wire format: ``POST {base_url}/embeddings``. ``OpenAICompatibleEmbedder``
plus the :data:`PRESETS` table therefore covers OpenAI, DeepSeek, Qwen
(DashScope), MiniMax, Moonshot/Kimi, Mistral, xAI, OpenRouter, Ollama,
LM Studio, and any self-hosted OpenAI-compatible endpoint via ``base_url=``.

Opt-in rules (the no-key default stays intact):
 - Nothing in this module runs unless the user explicitly constructs a
   provider (directly, or by naming one: ``Memory(embedder="openai:...")``).
 - Key resolution: explicit ``api_key`` arg > the named preset's environment
   variable. Environment variables are read ONLY on that explicit opt-in —
   never on the default path (CI-gated in tests/test_invariants.py).
 - Constructing an embedder opens no socket; the first network call happens on
   use (``embed``, or the one-off dimension probe via ``dim``/``identity()``).
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from typing import Mapping, Optional, Sequence

from ..embedding import EmbedderIdentity
from ._http import ProviderError, batched, post_json

__all__ = ["ProviderPreset", "PRESETS", "OpenAICompatibleEmbedder", "resolve_provider"]


@dataclass(frozen=True)
class ProviderPreset:
    """A known OpenAI-compatible provider: its base URL + how its key is found.

    ``embeddings`` / ``chat`` are documentation hints (docs/PROVIDERS.md): they
    soften a 404 into a helpful message but never block a call — if a provider
    adds an endpoint tomorrow, it just works.
    """

    base_url: Optional[str]
    env_var: Optional[str]  # None → keyless local server (Ollama, LM Studio)
    embeddings: bool = True
    chat: bool = True


#: Known providers. ``base_url`` can always be overridden (e.g. the mainland
#: variants of qwen/minimax/moonshot); ``"custom"`` is any OpenAI-compatible
#: server you point ``base_url=`` at (vLLM, LiteLLM proxy, llama.cpp, ...).
PRESETS: dict[str, ProviderPreset] = {
    "openai": ProviderPreset("https://api.openai.com/v1", "OPENAI_API_KEY"),
    "deepseek": ProviderPreset("https://api.deepseek.com/v1", "DEEPSEEK_API_KEY", embeddings=False),
    "qwen": ProviderPreset("https://dashscope-intl.aliyuncs.com/compatible-mode/v1", "DASHSCOPE_API_KEY"),
    "minimax": ProviderPreset("https://api.minimax.io/v1", "MINIMAX_API_KEY", embeddings=False),
    "moonshot": ProviderPreset("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", embeddings=False),
    "kimi": ProviderPreset("https://api.moonshot.ai/v1", "MOONSHOT_API_KEY", embeddings=False),
    "mistral": ProviderPreset("https://api.mistral.ai/v1", "MISTRAL_API_KEY"),
    "xai": ProviderPreset("https://api.x.ai/v1", "XAI_API_KEY", embeddings=False),
    "openrouter": ProviderPreset("https://openrouter.ai/api/v1", "OPENROUTER_API_KEY", embeddings=False),
    "anthropic": ProviderPreset("https://api.anthropic.com/v1", "ANTHROPIC_API_KEY", embeddings=False),
    "groq": ProviderPreset("https://api.groq.com/openai/v1", "GROQ_API_KEY", embeddings=False),
    "ollama": ProviderPreset("http://localhost:11434/v1", None),
    "lmstudio": ProviderPreset("http://localhost:1234/v1", None),
    "custom": ProviderPreset(None, None),
}


def resolve_provider(
    provider: str, *, api_key: Optional[str], base_url: Optional[str]
) -> tuple[str, Optional[str], ProviderPreset]:
    """Shared preset + key resolution for the embedder and consolidator classes.

    Returns ``(base_url, api_key_or_None, preset)``. A keyed provider with no
    key raises ``ValueError`` naming the exact environment variable to set —
    never a silent fallback.
    """
    preset = PRESETS.get(provider)
    if preset is None:
        known = ", ".join(sorted(PRESETS))
        raise ValueError(f"unknown provider {provider!r}; known providers: {known}")
    url = (base_url or preset.base_url or "").rstrip("/")
    if not url:
        raise ValueError(
            f"provider {provider!r} needs base_url= (your server's OpenAI-compatible root, "
            f"e.g. 'http://localhost:8000/v1')"
        )
    key = api_key if api_key is not None else (
        os.environ.get(preset.env_var) if preset.env_var else None
    )
    if not key and preset.env_var is not None:
        raise ValueError(
            f"provider {provider!r} needs an API key: pass api_key=... or set the "
            f"{preset.env_var} environment variable"
        )
    return url, (key or None), preset


class OpenAICompatibleEmbedder:
    """Cloud/self-hosted embeddings over the OpenAI wire format.

    Explicit opt-in only — Rekoll never constructs one of these by itself (the
    default embedder is local; ADR-0008/0009). Examples::

        OpenAICompatibleEmbedder()                                  # OpenAI, key from OPENAI_API_KEY
        OpenAICompatibleEmbedder("text-embedding-v4", provider="qwen")
        OpenAICompatibleEmbedder("nomic-embed-text", provider="ollama")  # local server, keyless
        OpenAICompatibleEmbedder("my-model", provider="custom",
                                 base_url="http://localhost:8000/v1")

    ``dim`` is probed with one tiny request on first use unless passed;
    ``dimensions`` (OpenAI v3 models) asks the server for shortened vectors
    and fixes ``dim`` to it. ``identity()`` is truthful — name, real dim, and
    a config hash over (base_url, model, dimensions) — so the per-scope
    identity guard and the mixed-dim skip keep working with cloud vectors.
    """

    DEFAULT_MODEL = "text-embedding-3-small"  # applies to provider="openai" only

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        provider: str = "openai",
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        dim: Optional[int] = None,
        dimensions: Optional[int] = None,
        timeout: float = 30.0,
        batch_size: int = 128,
        retries: int = 2,
        extra_headers: Optional[Mapping[str, str]] = None,
    ) -> None:
        if model is None:
            if provider != "openai":
                raise ValueError(
                    f"pass a model for provider {provider!r}, e.g. "
                    f"OpenAICompatibleEmbedder('<model>', provider={provider!r})"
                )
            model = self.DEFAULT_MODEL
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        self.model = model
        self.provider = provider
        self.base_url, self._api_key, self._preset = resolve_provider(
            provider, api_key=api_key, base_url=base_url
        )
        self._dimensions = dimensions
        self._dim = dim if dim is not None else dimensions
        self._timeout = timeout
        self._batch_size = batch_size
        self._retries = retries
        self._extra_headers = dict(extra_headers or {})

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def identity(self) -> EmbedderIdentity:
        config = f"openai-compat|{self.base_url}|{self.model}|dimensions={self._dimensions}"
        return EmbedderIdentity(
            name=f"{self.provider}:{self.model}",
            dim=self.dim,
            config_hash=hashlib.sha256(config.encode("utf-8")).hexdigest()[:16],
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        headers = dict(self._extra_headers)
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        out: list[list[float]] = []
        for chunk in batched(texts, self._batch_size):
            payload: dict[str, object] = {"model": self.model, "input": list(chunk)}
            if self._dimensions is not None:
                payload["dimensions"] = self._dimensions
            try:
                data = post_json(
                    f"{self.base_url}/embeddings",
                    payload,
                    headers=headers,
                    timeout=self._timeout,
                    retries=self._retries,
                    provider=self.provider,
                )
            except ProviderError as exc:
                raise self._with_capability_hint(exc) from None
            out.extend(self._parse_batch(data, expected=len(chunk)))
        return out

    # -- internals ------------------------------------------------------------
    def _parse_batch(self, data: dict, *, expected: int) -> list[list[float]]:
        items = data.get("data")
        if not isinstance(items, list) or len(items) != expected:
            got = len(items) if isinstance(items, list) else type(items).__name__
            raise ProviderError(
                f"{self.provider}: embeddings response malformed "
                f"(expected {expected} vectors, got {got})",
                provider=self.provider,
            )
        # Reassemble by the server's per-item index rather than trusting list
        # order, and verify the index set is exactly 0..n-1.
        vectors: list[Optional[list[float]]] = [None] * expected
        for position, item in enumerate(items):
            if not isinstance(item, dict) or "embedding" not in item:
                raise ProviderError(
                    f"{self.provider}: embeddings response malformed (item {position} "
                    f"has no 'embedding')",
                    provider=self.provider,
                )
            try:
                index = int(item.get("index", position))
                vector = [float(x) for x in item["embedding"]]
            except (TypeError, ValueError):
                raise ProviderError(
                    f"{self.provider}: embeddings response malformed (item {position} "
                    f"is not numeric)",
                    provider=self.provider,
                ) from None
            if not 0 <= index < expected or vectors[index] is not None:
                raise ProviderError(
                    f"{self.provider}: embeddings response malformed (bad index {index})",
                    provider=self.provider,
                )
            self._check_dim(len(vector))
            vectors[index] = vector
        return vectors  # type: ignore[return-value]  # all slots filled: count + index checked

    def _check_dim(self, n: int) -> None:
        if self._dim is None:
            self._dim = n
        elif n != self._dim:
            raise ProviderError(
                f"{self.provider}:{self.model} returned {n}-dim vectors but this embedder "
                f"is pinned to dim={self._dim} — fix or drop dim= so the per-scope "
                f"embedder-identity guard stays truthful",
                provider=self.provider,
            )

    def _with_capability_hint(self, exc: ProviderError) -> ProviderError:
        if exc.status == 404 and not self._preset.embeddings:
            return ProviderError(
                f"{exc} — note: {self.provider} may not offer an embeddings endpoint; "
                f"see docs/PROVIDERS.md (Voyage, Gemini, or the local default cover embeddings)",
                status=exc.status,
                provider=self.provider,
            )
        return exc
