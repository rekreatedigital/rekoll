"""Voyage AI embeddings — the answer to "can I use my Claude key?" (ADR-0015).

Anthropic sells NO embeddings API; Voyage is the embeddings provider Anthropic
itself recommends for the Claude ecosystem. So the answer is: for embeddings,
no — use Voyage (this class), Gemini, or the free local default; for the
optional consolidation/learning slot, yes — your Anthropic key works via
``OpenAICompatibleConsolidator(..., provider="anthropic")``.

Voyage's ``POST /v1/embeddings`` mirrors the OpenAI response shape but has its
own knobs (``input_type``, ``output_dimension``), hence a dedicated class.
"""

from __future__ import annotations

import hashlib
import os
from typing import Optional, Sequence

from ..embedding import EmbedderIdentity
from ._http import ProviderError, batched, post_json

__all__ = ["VoyageEmbedder"]


class VoyageEmbedder:
    """Voyage AI embeddings (``VOYAGE_API_KEY``).

    Examples::

        VoyageEmbedder()                       # voyage-3.5
        VoyageEmbedder("voyage-3-large", output_dimension=1024)

    ``input_type`` (``"query"`` / ``"document"``) is optional; Rekoll embeds
    documents and queries through one ``embed()`` seam, so the symmetric
    default (``None``) is safe — Voyage documents that vectors produced with
    and without ``input_type`` are compatible.
    """

    DEFAULT_MODEL = "voyage-3.5"
    DEFAULT_BASE_URL = "https://api.voyageai.com/v1"
    ENV_VAR = "VOYAGE_API_KEY"

    def __init__(
        self,
        model: Optional[str] = None,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        dim: Optional[int] = None,
        input_type: Optional[str] = None,
        output_dimension: Optional[int] = None,
        timeout: float = 30.0,
        batch_size: int = 128,
        retries: int = 2,
    ) -> None:
        self.model = model or self.DEFAULT_MODEL
        self.base_url = (base_url or self.DEFAULT_BASE_URL).rstrip("/")
        if batch_size <= 0:
            raise ValueError("batch_size must be positive")
        key = api_key if api_key is not None else os.environ.get(self.ENV_VAR)
        if not key:
            raise ValueError(
                "VoyageEmbedder needs an API key: pass api_key=... or set the "
                f"{self.ENV_VAR} environment variable (Anthropic sells no embeddings "
                "API — Voyage is the Claude-ecosystem choice; see docs/PROVIDERS.md)"
            )
        self._api_key = key
        self._input_type = input_type
        self._output_dimension = output_dimension
        self._dim = dim if dim is not None else output_dimension
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
            f"voyage|{self.base_url}|{self.model}"
            f"|input_type={self._input_type}|output_dimension={self._output_dimension}"
        )
        return EmbedderIdentity(
            name=f"voyage:{self.model}",
            dim=self.dim,
            config_hash=hashlib.sha256(config.encode("utf-8")).hexdigest()[:16],
        )

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        texts = list(texts)
        if not texts:
            return []
        headers = {"Authorization": f"Bearer {self._api_key}"}
        out: list[list[float]] = []
        for chunk in batched(texts, self._batch_size):
            payload: dict[str, object] = {"model": self.model, "input": list(chunk)}
            if self._input_type is not None:
                payload["input_type"] = self._input_type
            if self._output_dimension is not None:
                payload["output_dimension"] = self._output_dimension
            data = post_json(
                f"{self.base_url}/embeddings",
                payload,
                headers=headers,
                timeout=self._timeout,
                retries=self._retries,
                provider="voyage",
            )
            out.extend(self._parse_batch(data, expected=len(chunk)))
        return out

    def _parse_batch(self, data: dict, *, expected: int) -> list[list[float]]:
        items = data.get("data")
        if not isinstance(items, list) or len(items) != expected:
            got = len(items) if isinstance(items, list) else type(items).__name__
            raise ProviderError(
                f"voyage: embeddings response malformed (expected {expected} vectors, got {got})",
                provider="voyage",
            )
        vectors: list[Optional[list[float]]] = [None] * expected
        for position, item in enumerate(items):
            if not isinstance(item, dict) or "embedding" not in item:
                raise ProviderError(
                    f"voyage: embeddings response malformed (item {position} has no 'embedding')",
                    provider="voyage",
                )
            try:
                index = int(item.get("index", position))
                vector = [float(x) for x in item["embedding"]]
            except (TypeError, ValueError):
                raise ProviderError(
                    f"voyage: embeddings response malformed (item {position} is not numeric)",
                    provider="voyage",
                ) from None
            if not 0 <= index < expected or vectors[index] is not None:
                raise ProviderError(
                    f"voyage: embeddings response malformed (bad index {index})",
                    provider="voyage",
                )
            self._check_dim(len(vector))
            vectors[index] = vector
        return vectors  # type: ignore[return-value]  # all slots filled: count + index checked

    def _check_dim(self, n: int) -> None:
        if self._dim is None:
            self._dim = n
        elif n != self._dim:
            raise ProviderError(
                f"voyage:{self.model} returned {n}-dim vectors but this embedder is pinned "
                f"to dim={self._dim} — fix or drop dim= so the per-scope embedder-identity "
                f"guard stays truthful",
                provider="voyage",
            )
