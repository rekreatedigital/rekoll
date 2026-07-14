"""Battle-tester red-team v1 — the ACCEPTED piped-token false positive.

The ``<\\|...\\|>`` marker (firewall._INJECTION_MARKERS) exists to catch the
canonical piped model control tokens a host honors as role/channel switches
(``<|system|>``, ``<|im_start|>``, ``<|eot_id|>``, …). Spaceless F#/Elm/Haskell
pipe-operator code such as ``a<|b|>c`` matches the same shape and is quarantined
when it arrives from an untrusted source.

This was ACCEPTED as LOW in PR #49 (commit 21272ce) and documented ONLY in that
commit message — so a future contributor could "fix" it blindly by narrowing the
marker, reopening the exact bypass it guards (a host honors ``<|system|>``
whether or not it is space-delimited). These tests pin the accepted behavior next
to the reasoning.

DELIBERATELY NOT in benchmarks/attack_corpus.json's benign controls: those
enforce a ZERO-false-positive gate, and this FP is accepted precisely because it
lives OUTSIDE that gate. Adding it there would either fail that gate or force the
marker to be narrowed — the opposite of the decision.
"""

from __future__ import annotations

import pytest

from rekoll.adapters.base import QueryHit
from rekoll.firewall import build_envelope, screen
from rekoll.model import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier


@pytest.mark.parametrize("snippet", ["a<|b|>c", "f<|x|>g", "xs<|map|>ys"])
def test_spaceless_pipe_operator_code_is_quarantined_when_untrusted(snippet):
    # ACCEPTED false positive: the piped-token marker fires on spaceless
    # F#/Elm/Haskell ``<|...|>`` operators. Pinned so narrowing the marker to
    # "fix" this fails loudly (it would reopen the <|system|> bypass).
    decision = screen(snippet, source_trust=TrustTier.UNVERIFIED)
    assert decision.quarantined, f"marker narrowed? {snippet!r} no longer quarantined"
    assert decision.trust_tier is TrustTier.QUARANTINED
    assert decision.injection_markers  # the piped-token marker is what fired


def test_the_cost_is_only_lost_recall_the_wall_still_holds():
    # The FP is LOW precisely because containment does not depend on detection: an
    # un-recalled snippet is harmless, and even the quarantined record — were it
    # surfaced — renders as neutralized DATA, never a directive. So the accepted
    # cost is bounded to "an untrusted code snippet is not recallable", nothing more.
    record = MemoryRecord.create(
        scope=Scope(), kind=Kind.RAW_FACT, content="a<|b|>c",
        provenance=Provenance(source_uri="web://untrusted"),
        trust_tier=TrustTier.QUARANTINED,
    )
    record.status = Status.QUARANTINED
    env = build_envelope([QueryHit(record=record, score=1.0)])
    assert env.directives == ()  # never reaches the instruction channel


def test_a_trusted_pipe_snippet_is_not_quarantined():
    # Symmetry with every other marker: quarantine fires only at trust <=
    # UNVERIFIED. A developer who vouches for their own F#/Elm source (trust>=
    # trusted_source) keeps it recallable — the documented escape hatch.
    decision = screen("a<|b|>c", source_trust=TrustTier.TRUSTED_SOURCE)
    assert not decision.quarantined
