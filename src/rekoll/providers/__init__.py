"""rekoll.providers — OPT-IN bring-your-own-AI: cloud embedders + consolidator.

This package is NEVER imported by the default path (``Memory()`` with no
arguments) — a CI invariant pins it out of ``sys.modules`` there. You reach it
only by constructing a provider yourself or naming one explicitly, e.g.
``Memory(embedder="openai:text-embedding-3-small")``. Environment variables
(API keys) are read only at that explicit construction, and no socket opens
until first use.

Standard-library HTTP only (ADR-0015): bringing your own AI adds ZERO pip
dependencies. See docs/PROVIDERS.md for per-provider setup.
"""

from ._http import ProviderError
from .consolidation import DEFAULT_SYSTEM_PROMPT, OpenAICompatibleConsolidator
from .gemini import GeminiEmbedder
from .openai_compat import PRESETS, OpenAICompatibleEmbedder, ProviderPreset
from .voyage import VoyageEmbedder

__all__ = [
    "ProviderError",
    "OpenAICompatibleEmbedder",
    "ProviderPreset",
    "PRESETS",
    "GeminiEmbedder",
    "VoyageEmbedder",
    "OpenAICompatibleConsolidator",
    "DEFAULT_SYSTEM_PROMPT",
]
