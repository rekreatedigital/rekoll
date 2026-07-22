"""The ``Memory`` facade's live-project-board door (ADR-0035, Lane B).

The SDK surface — ``mem.board()``, ``mem.resolve()``, ``mem.remember(board=)``
— is deliberately THIN: every leg is delegated to
``rekoll.board.build_board_payload``, the one builder the CLI and MCP doors
will also render. What this file pins is exactly that thinness, plus the
promises the facade adds on top:

 * BYTE-PARITY — ``BoardResult.to_dict()`` serializes identically to the
   builder's dict for the same store, so a typed SDK object and a raw door
   payload can never disagree (this is what makes the doors comparable);
 * the board is a FREE read — it constructs no embedder and credits nothing to
   the was-it-used ledger, so polling it cannot fake evidence that a memory
   informed an action (``informed_by``);
 * ``resolve()`` count semantics — ACTIVE -> SUPERSEDED only, silent per-id
   non-transitions, and a resolved item leaving BOTH the board and recall while
   its bytes survive;
 * ``remember(board=)`` being metadata sugar — it changes no record id, refuses
   an unknown leg, and refuses to guess when the caller states two intents.
"""

from __future__ import annotations

import json

import pytest

from rekoll.adapters.base import (
    BOARD_METADATA_KEY,
    BOARD_TAG_MAJOR,
    BOARD_TAG_PENDING,
    UnsupportedCapabilityError,
)
from rekoll.adapters.sqlite import SQLiteAdapter
from rekoll.board import build_board_payload
from rekoll.embedding import StubEmbedder
from rekoll.memory import BoardResult, Memory
from rekoll.model import Kind, Status, TrustTier


def _mem(tmp_path, name="board.db", **kwargs):
    return Memory(
        path=str(tmp_path / name), project="board",
        embedder=StubEmbedder(), reranker=None, **kwargs,
    )


def _seeded(tmp_path, name="board.db"):
    mem = _mem(tmp_path, name)
    mem.remember("always explain simply", kind=Kind.DIRECTIVE, trust=TrustTier.OWNER)
    mem.remember("storage lane shipped", board=BOARD_TAG_MAJOR)
    mem.remember("docs pass still open", board=BOARD_TAG_PENDING)
    mem.remember("an untagged plain fact")
    return mem


# --- byte-parity with the one builder ---------------------------------------

def test_board_result_to_dict_is_byte_identical_to_the_builder(tmp_path):
    """The facade RE-SHAPES the payload, it never recomputes a leg. If these
    two ever diverge by a single byte, the SDK board and the CLI/MCP boards are
    no longer the same board — which is the whole point of the shared builder."""
    mem = _seeded(tmp_path)
    result = mem.board()
    assert isinstance(result, BoardResult)
    direct = build_board_payload(mem.adapter, mem.scope)
    assert json.dumps(result.to_dict()) == json.dumps(direct)
    assert list(result.to_dict().keys()) == list(direct.keys())  # key ORDER too
    mem.close()


def test_board_result_is_frozen_and_its_dict_is_a_detached_copy(tmp_path):
    """A board handed to several concurrent sessions must not be mutable under
    them, and mutating one caller's ``to_dict()`` must not corrupt the result
    the next caller reads."""
    mem = _seeded(tmp_path)
    result = mem.board()
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        result.pending_open = 99
    dumped = result.to_dict()
    dumped["majors"].append({"id": "injected"})
    dumped["rules"].append("injected rule")
    if result.majors:
        dumped_entry = result.to_dict()["majors"][0]
        dumped_entry["text"] = "mutated"
        assert result.majors[0]["text"] != "mutated"
    assert len(result.to_dict()["majors"]) == len(result.majors)
    assert len(result.to_dict()["rules"]) == len(result.rules)
    mem.close()


def test_board_result_entries_refuse_in_place_mutation(tmp_path):
    """The frozen dataclass only blocks attribute REBINDING; the entries
    themselves must also be read-only, or one session's in-place edit
    (``result.majors[0]["text"] = ...``) silently rewrites the board every
    other holder of the same instance serializes — a confirmed PR #57 review
    finding. Entries are copy-then-proxied at construction, so mutation raises
    and the builder's own dicts are never aliased."""
    mem = _seeded(tmp_path)
    result = mem.board()
    assert result.majors, "seed guarantees at least one major"
    before = json.dumps(result.to_dict())
    with pytest.raises(TypeError):
        result.majors[0]["text"] = "POISONED"
    with pytest.raises(TypeError):
        result.recent[0]["trust"] = "owner"
    assert json.dumps(result.to_dict()) == before, "a failed mutation leaked"
    mem.close()


def test_resolve_on_storage_without_the_capability_fails_honestly(tmp_path):
    """``resolve()`` mirrors ``board()``'s honest failure: an adapter without
    ``set_status`` raises ``UnsupportedCapabilityError`` naming the adapter —
    never a silent count of 0 that reads as 'nothing needed resolving'."""

    class NoResolve(SQLiteAdapter):
        def set_status(self, **kwargs):
            raise UnsupportedCapabilityError("no set_status here")

    mem = _mem(tmp_path)
    rid = mem.remember("a pending item", board=BOARD_TAG_PENDING).id
    mem.adapter.close()
    mem.adapter = NoResolve(str(tmp_path / "nr.db"))
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        mem.resolve(rid)
    message = str(excinfo.value)
    assert "NoResolve" in message, "the message must name the adapter in use"
    assert "sqlite" in message.lower()  # and point at one that does serve it
    mem.close()


def test_board_legs_carry_the_expected_records(tmp_path):
    """A sanity pass over the delegated legs through the facade's own types."""
    mem = _seeded(tmp_path)
    result = mem.board()
    assert result.rules == ("always explain simply",)
    texts = {entry["text"] for entry in result.majors}
    assert texts == {"storage lane shipped", "docs pass still open"}
    assert result.pending_open == 1
    assert {entry["board"] for entry in result.majors} == {
        BOARD_TAG_MAJOR, BOARD_TAG_PENDING,
    }
    assert result.latest is not None
    mem.close()


# --- parameters mirror the builder ------------------------------------------

def test_limits_disable_legs_and_refuse_hostile_values(tmp_path):
    """0 disables a leg; negative / over-ceiling raise rather than clamp — the
    builder's rule, reached through the facade unchanged."""
    mem = _seeded(tmp_path)
    assert mem.board(rules_limit=0).rules == ()
    assert mem.board(major_limit=0).majors == ()
    assert mem.board(recent_limit=0).recent == ()
    # pending_open is a COUNT, not a capped leg: disabling majors doesn't hide it
    assert mem.board(major_limit=0).pending_open == 1
    for kwargs in (
        {"rules_limit": -1}, {"major_limit": -1}, {"recent_limit": -1},
        {"rules_limit": 51}, {"major_limit": 51}, {"recent_limit": 51},
    ):
        with pytest.raises(ValueError):
            mem.board(**kwargs)
    mem.close()


def test_min_trust_defaults_to_the_builder_and_gates_tier1_only(tmp_path):
    """``min_trust=None`` means "the builder's default", and the Tier-2 floor is
    NOT a caller preference: raising min_trust cannot ADD a low-trust row to the
    curated leg, and lowering it cannot either."""
    mem = _mem(tmp_path)
    mem.remember(
        "a low-trust tagged major", trust=TrustTier.UNVERIFIED, board=BOARD_TAG_MAJOR,
    )
    default = mem.board()
    assert json.dumps(default.to_dict()) == json.dumps(
        build_board_payload(mem.adapter, mem.scope)
    )
    assert [e["text"] for e in default.recent] == [None], (
        "below the board floor an entry appears but its text is withheld"
    )
    assert default.majors == (), "a tag alone never curates a below-floor row"
    # Raise the Tier-1 floor: the row leaves the feed, the curated leg is still empty.
    strict = mem.board(min_trust=int(TrustTier.TRUSTED_SOURCE))
    assert strict.recent == () and strict.majors == ()
    mem.close()


# --- the board is a FREE read ------------------------------------------------

def test_board_never_constructs_an_embedder(tmp_path, monkeypatch):
    """The board does no query embedding by construction — enforce it: any
    embedder construction fails the test (the tests/test_cli.py status pattern,
    adapted to the SDK)."""
    mem = _seeded(tmp_path)

    def bomb(*args, **kwargs):
        raise AssertionError("board() must not construct an embedder")

    monkeypatch.setattr("rekoll.memory._auto_embedder", bomb)
    monkeypatch.setattr("rekoll.embedding.FastEmbedEmbedder", bomb, raising=False)
    monkeypatch.setattr(mem.embedder, "embed", bomb)
    assert mem.board().pending_open == 1
    assert mem.resolve() == 0
    mem.close()


def test_board_credits_nothing_to_the_was_it_used_ledger(tmp_path):
    """Only ``recall()`` writes the ledger. If polling the board credited ids,
    ``informed_by`` would report memories that informed nothing — manufacturing
    the very evidence the was-it-used loop exists to make honest."""
    mem = _seeded(tmp_path)
    mem.recall("docs", call_id="real-action")
    before = [dict(entry) for entry in mem.informed_by(limit=50)]
    assert before, "the real recall was credited, so this test can detect a change"

    for _ in range(3):
        mem.board()
    mem.resolve("nonexistent-id")

    after = [dict(entry) for entry in mem.informed_by(limit=50)]
    assert after == before, "board()/resolve() must not touch the recall ledger"
    assert mem.informed_by("real-action", limit=50), "the real recall is still credited"
    mem.close()


# --- resolve() ---------------------------------------------------------------

def test_resolve_counts_only_real_transitions(tmp_path):
    """The return value is the honest report of what MOVED; every non-transition
    is silent per-id (no exceptions for no-ops)."""
    mem = _seeded(tmp_path)
    pending = [e for e in mem.board().majors if e["board"] == BOARD_TAG_PENDING][0]

    assert mem.resolve() == 0                      # nothing asked, nothing moved
    assert mem.resolve("no-such-id") == 0          # unknown id: silent
    assert mem.resolve(pending["id"]) == 1         # the real transition
    assert mem.resolve(pending["id"]) == 0         # already resolved: silent
    # A batch reports only the movers.
    major = [e for e in mem.board().majors if e["board"] == BOARD_TAG_MAJOR][0]
    assert mem.resolve("no-such-id", major["id"], pending["id"]) == 1
    mem.close()


def test_resolve_is_scoped(tmp_path):
    """A resolve in one scope can never reach another's row — the count says 0
    and the other board is untouched."""
    mine = _seeded(tmp_path, "a.db")
    theirs = Memory(
        path=str(tmp_path / "a.db"), project="other-project",
        embedder=StubEmbedder(), reranker=None,
    )
    target = mine.board().majors[0]["id"]
    assert theirs.resolve(target) == 0, "a cross-scope resolve must not transition"
    assert target in {e["id"] for e in mine.board().majors}
    mine.close()
    theirs.close()


def test_a_resolved_item_leaves_the_board_and_recall_but_keeps_its_bytes(tmp_path):
    """Resolve MARKS, it never deletes (contrast ``forget``): the item leaves
    every surfaced read, and the record is still there for audit."""
    mem = _seeded(tmp_path)
    before = mem.board()
    pending = [e for e in before.majors if e["board"] == BOARD_TAG_PENDING][0]
    assert mem.recall("docs pass still open").ids(), "recall surfaces it first"

    assert mem.resolve(pending["id"]) == 1
    after = mem.board()

    assert pending["id"] not in {e["id"] for e in after.majors}
    assert pending["id"] not in {e["id"] for e in after.recent}
    assert after.pending_open == before.pending_open - 1
    assert pending["id"] not in mem.recall("docs pass still open").ids()

    stored = mem.adapter.get(scope=mem.scope, ids=[pending["id"]]).records
    assert len(stored) == 1, "resolve marks, never evicts"
    assert stored[0].status is Status.SUPERSEDED
    assert stored[0].content == "docs pass still open"  # every byte survives
    mem.close()


def test_resolve_takes_no_status_argument(tmp_path):
    """The facade verb is ACTIVE -> SUPERSEDED and nothing else: a caller cannot
    reach another transition (or resurrect a row) through it."""
    mem = _seeded(tmp_path)
    with pytest.raises(TypeError):
        mem.resolve("some-id", status="active")
    mem.close()


# --- remember(board=) --------------------------------------------------------

def test_board_keyword_tags_metadata_without_changing_the_record_id(tmp_path):
    """The tag is METADATA, which lives outside the content address — the same
    content yields the same id tagged or not."""
    mem = _mem(tmp_path)
    plain = mem.remember("identical content here", source="s://one")
    mem.forget(plain.id)
    tagged = mem.remember("identical content here", source="s://one", board=BOARD_TAG_MAJOR)
    assert tagged.id == plain.id, "a board tag must not change the record id"
    assert tagged.metadata[BOARD_METADATA_KEY] == BOARD_TAG_MAJOR
    assert BOARD_METADATA_KEY not in plain.metadata
    mem.close()


def test_board_keyword_merges_with_other_metadata(tmp_path):
    mem = _mem(tmp_path)
    record = mem.remember(
        "a tagged fact", board=BOARD_TAG_PENDING, metadata={"ticket": "REK-1"},
    )
    assert record.metadata[BOARD_METADATA_KEY] == BOARD_TAG_PENDING
    assert record.metadata["ticket"] == "REK-1"
    # An explicit metadata tag that AGREES is redundant, not a conflict.
    agreeing = mem.remember(
        "another tagged fact", board=BOARD_TAG_PENDING,
        metadata={BOARD_METADATA_KEY: BOARD_TAG_PENDING},
    )
    assert agreeing.metadata[BOARD_METADATA_KEY] == BOARD_TAG_PENDING
    mem.close()


def test_an_unknown_board_leg_raises_naming_the_legal_values(tmp_path):
    """Storing an unrecognized tag would be WORSE than refusing: the payload
    normalizes unknown tags to null, so the write would look accepted and the
    item would silently never appear on the board."""
    mem = _mem(tmp_path)
    for bad in ("majors", "MAJOR", "done", "", "pending "):
        with pytest.raises(ValueError) as excinfo:
            mem.remember("a fact", board=bad)
        message = str(excinfo.value)
        assert BOARD_TAG_MAJOR in message and BOARD_TAG_PENDING in message
    assert mem.count() == 0, "a refused tag must not store the record either"
    mem.close()


def test_a_conflicting_metadata_tag_raises_rather_than_picking_one(tmp_path):
    mem = _mem(tmp_path)
    with pytest.raises(ValueError) as excinfo:
        mem.remember(
            "a fact", board=BOARD_TAG_MAJOR,
            metadata={BOARD_METADATA_KEY: BOARD_TAG_PENDING},
        )
    assert "conflicting" in str(excinfo.value)
    assert mem.count() == 0
    mem.close()


def test_metadata_only_tagging_still_works_without_the_keyword(tmp_path):
    """The keyword is SUGAR — it is not the only way in, and adding it must not
    have broken the plain metadata path Lane A shipped."""
    mem = _mem(tmp_path)
    mem.remember("tagged the long way", metadata={BOARD_METADATA_KEY: BOARD_TAG_MAJOR})
    assert [e["text"] for e in mem.board().majors] == ["tagged the long way"]
    mem.close()


# --- honest failure ----------------------------------------------------------

def test_board_on_storage_without_the_capability_fails_honestly(tmp_path):
    """No board_snapshot means there is no board to degrade to: raise, naming
    the adapter, rather than return a plausible empty board."""

    class NoBoard(SQLiteAdapter):
        def board_snapshot(self, **kwargs):
            raise UnsupportedCapabilityError("no board here")

    mem = _mem(tmp_path)
    mem.adapter.close()
    mem.adapter = NoBoard(str(tmp_path / "nb.db"))
    with pytest.raises(UnsupportedCapabilityError) as excinfo:
        mem.board()
    message = str(excinfo.value)
    assert "NoBoard" in message, "the message must name the adapter in use"
    assert "sqlite" in message.lower()  # and point at one that does serve a board
    mem.close()
