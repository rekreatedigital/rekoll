"""P1-5 / ADR-0020: the versioned attack-corpus regression gates.

Two independent gates run over benchmarks/attack_corpus.json:

1. CONTAINMENT (the wall — must be 100%): no untrusted attack, detected or not,
   ever reaches the recall envelope's *instruction* channel. Even an undetected
   payload lands as DATA/evidence with its forged frame neutralized. This is
   Rekoll's load-bearing security claim (DESIGN §6).

2. DETECTION / ASR (the tripwire — ratchet only): every payload the corpus
   marks ``detected: true`` must be quarantined at ingest, and the aggregate
   detection rate may only rise (ASR may only fall). Per-entry asserts make a
   marker regression fail CI on the exact payload it stops catching.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rekoll import Kind, MemoryRecord, Provenance, Scope, Status, TrustTier
from rekoll.adapters.base import QueryHit
from rekoll.firewall import build_envelope, screen

CORPUS_PATH = Path(__file__).resolve().parents[1] / "benchmarks" / "attack_corpus.json"
_CORPUS = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
_ENTRIES = _CORPUS["attacks"]
_ATTACKS = [e for e in _ENTRIES if e["category"] != "benign_control"]
_BENIGN = [e for e in _ENTRIES if e["category"] == "benign_control"]

# Ratchet baseline — RAISE when detection improves, NEVER lower (DESIGN §9,
# "ASR may only go DOWN"). Observed detection: 50/63 = 79.4%.
BASELINE_DETECTION_RATE = 0.79


def _ids(entries):
    return [e["id"] for e in entries]


def test_corpus_is_versioned_and_large_enough():
    assert _CORPUS["version"] >= 1
    assert len(_ATTACKS) >= 50, "attack corpus must hold >= 50 payloads (DESIGN §9)"
    # ids are unique — a duplicate would silently weaken a gate
    ids = _ids(_ENTRIES)
    assert len(ids) == len(set(ids))
    # every attack carries the categorization the gates rely on
    for e in _ATTACKS:
        assert e["category"] and isinstance(e["detected"], bool) and e["payload"]


@pytest.mark.parametrize("entry", [e for e in _ATTACKS if e["detected"]], ids=_ids([e for e in _ATTACKS if e["detected"]]))
def test_flagged_attacks_are_quarantined_at_ingest(entry):
    # The ratchet: a payload the corpus claims we detect MUST stay detected.
    decision = screen(entry["payload"], source_trust=TrustTier.UNVERIFIED)
    assert decision.quarantined, f"regression: {entry['id']} no longer quarantined"
    assert decision.trust_tier is TrustTier.QUARANTINED


@pytest.mark.parametrize("entry", _BENIGN, ids=_ids(_BENIGN))
def test_benign_controls_are_not_quarantined(entry):
    decision = screen(entry["payload"], source_trust=TrustTier.UNVERIFIED)
    assert not decision.quarantined, f"false positive on benign control {entry['id']}"


def test_detection_rate_meets_baseline():
    detected = sum(
        1 for e in _ATTACKS
        if screen(e["payload"], source_trust=TrustTier.UNVERIFIED).quarantined
    )
    rate = detected / len(_ATTACKS)
    asr = 1.0 - rate
    assert rate >= BASELINE_DETECTION_RATE, (
        f"detection regressed to {rate:.1%} (ASR {asr:.1%}); "
        f"baseline is {BASELINE_DETECTION_RATE:.0%}. Fix the marker regression "
        "or, if you added harder attacks, improve detection — do not lower the baseline."
    )
    # The `detected` flags must stay truthful to the code (no stale corpus).
    for e in _ATTACKS:
        actual = screen(e["payload"], source_trust=TrustTier.UNVERIFIED).quarantined
        assert actual == e["detected"], (
            f"{e['id']}: corpus says detected={e['detected']} but screen says {actual}; "
            "update the corpus flag (and baseline if the rate moved)."
        )


def _render_untrusted(payload: str) -> tuple[tuple[str, ...], str]:
    """Ingest a payload as untrusted RAW_FACT, then build+render its envelope."""
    decision = screen(payload, source_trust=TrustTier.UNVERIFIED)
    if not decision.content:  # sanitized to empty (only invisible chars)
        return (), ""
    record = MemoryRecord.create(
        scope=Scope(), kind=Kind.RAW_FACT, content=decision.content,
        provenance=Provenance(source_uri="web://untrusted"), trust_tier=decision.trust_tier,
    )
    if decision.quarantined:
        record.status = Status.QUARANTINED
    env = build_envelope([QueryHit(record=record, score=1.0)])
    return env.directives, env.render()


@pytest.mark.parametrize("entry", _ATTACKS, ids=_ids(_ATTACKS))
def test_containment_no_attack_reaches_instruction_channel(entry):
    # THE WALL: regardless of detection, an untrusted attack never populates the
    # directive channel, and never forges the envelope's own frame.
    directives, rendered = _render_untrusted(entry["payload"])
    assert directives == (), f"{entry['id']} reached the instruction channel: {directives}"
    # The real directive header only renders when directives is non-empty; a
    # forged one in content must have been neutralized to [marker].
    assert "# Trusted directives (rules to follow):" not in rendered
    # Forged role/channel tags are defanged.
    for tag in ("</system>", "<system>", "<assistant>", "<im_start>", "[INST]", "[/INST]"):
        assert tag not in rendered, f"{entry['id']} left a live role tag {tag!r} in the frame"


def test_markdown_forgery_headers_are_neutralized():
    # Targeted proof for the markdown-forgery family: the forged section header
    # is replaced by [marker] and the payload is framed as DATA, not a rule.
    forgeries = [e for e in _ATTACKS if e["category"] == "markdown_forgery"]
    assert forgeries
    for e in forgeries:
        directives, rendered = _render_untrusted(e["payload"])
        assert directives == ()
        assert "[marker]" in rendered, f"{e['id']}: forged header not neutralized"
        assert "NOT instructions" in rendered
