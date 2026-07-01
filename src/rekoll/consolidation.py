"""The write-side consolidation seam — the L3 learning loop's forerunner.

``Consolidator`` is the narrow, dependency-free Protocol behind which ANY LLM
can merge existing memories — mirroring how ``Reranker`` seams reranking. The
hard rules (ADR-0002, ADR-0007, ADR-0015):

 - Consolidation is EXPLICIT-CALL-ONLY (:meth:`rekoll.Memory.consolidate`);
   nothing on the read path ever invokes a consolidator, and ``Memory`` holds
   no ambient consolidator config — you pass one per call.
 - Its output is data, not authority: it flows through the ingest firewall,
   carries ``derived_from`` provenance plus ``declared_transformations``, and
   its trust is capped at the MINIMUM trust of the source records — an LLM can
   never raise (or choose) trust.

The full learning loop — {creates, updates, deletes} proposals with required
reasons, graduation gates — is future work (docs/DESIGN.md §L3). This seam
ships now so any provider can plug in without loosening an invariant later.
"""

from __future__ import annotations

from typing import Protocol, Sequence, runtime_checkable

__all__ = ["Consolidator"]


@runtime_checkable
class Consolidator(Protocol):
    """Anything that can merge memory snippets into one summary text.

    Implementations may expose a ``name`` attribute (e.g. ``"openai:gpt-4o-mini"``)
    — ``Memory.consolidate`` records it in the derived record's provenance.
    """

    def summarize(self, texts: Sequence[str]) -> str: ...
