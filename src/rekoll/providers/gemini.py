"""Google Gemini embeddings (ADR-0015).

Google's Generative Language API does not speak the OpenAI wire format
(``:batchEmbedContents`` with per-request ``content.parts``), so it gets its
own small class. Auth uses the ``x-goog-api-key`` HEADER — never a ``?key=``
query parameter, which would leak the key into server/proxy logs.

Same opt-in rules as every provider: constructed only by explicit user code,
env vars read only then, no socket until first use.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional, Sequence

from ..embedding import EmbedderIdentity
from ._http import ProviderError, batched, post_json

__all__ = ["GeminiEmbedder"]


class GeminiEmbedder:
    """Gemini embeddings via the Generative Language API.

    Examples::

        GeminiEmbedder()                                   # gemini-embedding-001, GEMINI_API_KEY
        GeminiEmbedder("text-embedding-004")
        GeminiEmbedder(output_dimensionality=768)          # shortened vectors

    ``task_type`` (e.g. ``"RETRIEVAL_DOCUMENT"``) is optional; Rekoll embeds
    documents and queries through one ``embed()`` seam, so leaving it unset —
    symmetric embeddings — is the safe default.
    """

    DEFAULT_MODEL = "gemini-embedding-001"
    DEFAULT_BASE_URL = "https://generativelanguage.googleapis.com/v1beta"
    _ENV_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        dim: Optional[int] = None,
        output_dimensionality: Optional[int] = None,
        task_type: Optional[str] = None,
        timeout: float = 30.0,
        batch_size: int = 100,
        retries: int = 2,
    ) -> None:
        raw_model = model or self.DEFAULT_MODEL
        self.model = raw_model[len("models/"):] if raw_model.startswith("models/") else raw_model
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        key = api_key
        if key is None:
            key = next((os.environ[v] for v in self._ENV_VARS if os.environ.get(v)), None)
        if not key:
            raise ValueError(
                "GeminiEmbedder needs an API key: pass api_key=... or set the "
                "GEMINI_API_KEY (or GOOGLE_API_KEY) environment variable"
            )
        self._api_key = key
        self._output_dimensionality = output_dimensionality
        self._dim = dim if dim is not None else output_dimensionality
        self._task_type = task_type
        self._timeout = timeout
        self._batch_size = batch_size
        self._retries = retries

    @property
    def dim(self) -> int:
        if self._dim is None:
            self._dim = len(self.embed(["dimension probe"])[0])
        return self._dim

    def identity(self) -> EmbedderIdentity:
        config = (
            f"gemini|{self.base_url}|{self.model}"
            f"|output_dimensionality={self._output_dimensionality}|task_type={self._task_type}"
        )
        return EmbedderIdentity(
            name=f"gemini:{self.model}",
            dim=self.dim,
            config_hash=hashlib.sha256(config.encode("utf-8")).hexdigest()[:16],
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        url = f"{self.base_url}/models/{self.model}:batchEmbedContents"
        headers = {"x-goog-api-key": self._api_key}
        out: list[list[float]] = []
        for chunk in batched(texts, self._batch_size):
            requests_payload: list[dict[str, object]] = []
            for text in chunk:
                item: dict[str, object] = {
                    "model": f"models/{self.model}",
                    "content": {"parts": [{"text": text}]},
                }
                if self._output_dimensionality is not None:
                    item["outputDimensionality"] = self._output_dimensionality
                if self._task_type is not None:
                    item["taskType"] = self._task_type
                requests_payload.append(item)
            data = post_json(
                url,
                {"requests": requests_payload},
                headers=headers,
                timeout=self._timeout,
                retries=self._retries,
                provider="gemini",
            )
            out.extend(self._parse_batch(data, expected=len(chunk)))
        return out

    def _parse_batch(self, data: dict, *, expected: int) -> list[list[float]]:
        items = data.get("embeddings")
        if not isinstance(items, list) or len(items) != expected:
            got = len(items) if isinstance(items, list) else type(items).__name__
            raise ProviderError(
                f"gemini: embeddings response malformed (expected {expected} vectors, got {got})",
                provider="gemini",
            )
        vectors: list[list[float]] = []
        for position, item in enumerate(items):
            values = item.get("values") if isinstance(item, dict) else None
            if not isinstance(values, list) or not values:
                raise ProviderError(
                    f"gemini: embeddings response malformed (item {position} has no 'values')",
                    provider="gemini",
                )
            try:
                vector = [float(x) for x in values]
            except (TypeError, ValueError):
                raise ProviderError(
                    f"gemini: embeddings response malformed (item {position} is not numeric)",
                    provider="gemini",
                ) from None
            self._check_dim(len(vector))
            vectors.append(vector)
        return vectors

    def _check_dim(self, n: int) -> None:
        if self._dim is None:
            self._dim = n
        elif n != self._dim:
            raise ProviderError(
                f"gemini:{self.model} returned {n}-dim vectors but this embedder is pinned "
                f"to dim={self._dim} — fix or drop dim= so the per-scope embedder-identity "
                f"guard stays truthful",
                provider="gemini",
            )
