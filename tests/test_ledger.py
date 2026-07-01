"""RecallLedger — the process-local was-it-used ring buffer. No I/O, no deps."""

from __future__ import annotations

import pytest

from rekoll import RecallLedger


def test_record_and_entries_roundtrip():
    ledger = RecallLedger()
    ledger.record(["id-a", "id-b"], query="postgres pooling", ts=1000.0)
    entries = ledger.entries(now=1001.0)
    assert len(entries) == 1
    assert entries[0]["ids"] == ["id-a", "id-b"]
    assert entries[0]["query"] == "postgres pooling"
    assert entries[0]["ts"].endswith("+00:00")  # JSON-safe ISO-8601 UTC


def test_entries_newest_first_and_limited():
    ledger = RecallLedger()
    for i in range(10):
        ledger.record([f"id-{i}"], ts=1000.0 + i)
    entries = ledger.entries(limit=3, now=1010.0)
    assert [e["ids"][0] for e in entries] == ["id-9", "id-8", "id-7"]


def test_call_id_scoping_never_credits_another_call():
    ledger = RecallLedger()
    ledger.record(["id-mine"], call_id="call-1", ts=1000.0)
    ledger.record(["id-other"], call_id="call-2", ts=1001.0)
    ledger.record(["id-loose"], ts=1002.0)  # session-scope, no call_id
    scoped = ledger.entries("call-1", now=1003.0)
    assert [e["ids"][0] for e in scoped] == ["id-mine"]
    # No call_id: the recent-entries fallback sees everything live.
    assert len(ledger.entries(now=1003.0)) == 3


def test_ttl_prunes_stale_entries():
    ledger = RecallLedger(ttl_seconds=60)
    ledger.record(["id-old"], ts=1000.0)
    ledger.record(["id-new"], ts=1055.0)
    entries = ledger.entries(now=1061.0)  # id-old is 61s stale, id-new 6s
    assert [e["ids"][0] for e in entries] == ["id-new"]
    assert len(ledger) == 1  # pruning is a side effect


def test_ring_buffer_caps_entries():
    ledger = RecallLedger(max_entries=5)
    for i in range(20):
        ledger.record([f"id-{i}"], ts=1000.0 + i)
    assert len(ledger) == 5
    entries = ledger.entries(limit=50, now=1020.0)
    assert [e["ids"][0] for e in entries] == [f"id-{i}" for i in range(19, 14, -1)]


def test_record_is_fail_soft_on_junk_input():
    ledger = RecallLedger()
    ledger.record([])  # nothing to record — no entry, no error
    ledger.record([None, "", 0])  # falsy ids are dropped
    ledger.record(object())  # not even a sequence of ids — swallowed
    assert ledger.entries() == []


def test_record_accepts_a_lone_string_id():
    ledger = RecallLedger()
    ledger.record("id-solo", ts=1000.0)  # not exploded into characters
    (entry,) = ledger.entries(now=1000.0)
    assert entry["ids"] == ["id-solo"]


def test_record_truncates_oversized_input():
    ledger = RecallLedger()
    ledger.record([f"id-{i}" for i in range(100)], query="q" * 1000, ts=1000.0)
    (entry,) = ledger.entries(now=1000.0)
    assert len(entry["ids"]) == 20
    assert len(entry["query"]) == 200


def test_clear_resets():
    ledger = RecallLedger()
    ledger.record(["id-a"], ts=1000.0)
    ledger.clear()
    assert len(ledger) == 0
    assert ledger.entries(now=1000.0) == []


def test_constructor_validates():
    with pytest.raises(ValueError):
        RecallLedger(max_entries=0)
    with pytest.raises(ValueError):
        RecallLedger(ttl_seconds=0)


def test_ledger_is_thread_safe_under_contention():
    # The claim is "thread-safe": concurrent record/entries/clear from many
    # threads must never raise, never exceed the cap, and never corrupt an
    # entry (ids always intact, never partially written).
    import threading

    ledger = RecallLedger(max_entries=32)
    errors: list[BaseException] = []

    def _hammer(worker: int) -> None:
        try:
            for i in range(300):
                ledger.record([f"w{worker}-a{i}", f"w{worker}-b{i}"],
                              query=f"q-{worker}-{i}", call_id=f"call-{worker}")
                if i % 7 == 0:
                    for entry in ledger.entries(f"call-{worker}", limit=5):
                        assert len(entry["ids"]) == 2  # never a torn write
                if i % 97 == 0:
                    ledger.clear()
        except BaseException as exc:  # noqa: BLE001 — collect, assert below
            errors.append(exc)

    threads = [threading.Thread(target=_hammer, args=(w,)) for w in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors, errors
    assert len(ledger) <= 32
    for entry in ledger.entries(limit=50):
        assert len(entry["ids"]) == 2
