# -*- coding: utf-8 -*-
"""
tests/test_torn_lines.py — a crash mid-write tears a line; every reader goes on.

A torn last line cut inside a multi-byte character is skipped by every reader of a jsonl
(the source registry, the write keys, the usage log, the ledger), and the next append
starts on a fresh line. A ledger whose last line is longer than the tail the appender
reads still numbers the next line past it. A cursor key left short by a crash is made
again instead of blocking every cursor.
"""

import asyncio
import json

from core import _ledger as L
from core import _sources as S
from core import _usage as U
from locibrain.eventsourcing.ledger_mirror import LedgerMirror


def run(coro):
    return asyncio.run(coro)


def _torn() -> bytes:
    """Half a line, cut inside a three-byte character."""
    raw = json.dumps({"key": "暗号是青柠汽水"}, ensure_ascii=False).encode("utf-8")
    cut = raw.index("青".encode("utf-8")) + 1
    return raw[:cut]


def _tear(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("ab") as f:
        f.write(_torn())


def test_the_registry_reads_past_a_torn_line_and_appends_after_it(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    run(reg.apply_change({"change_id": "a", "source": "lento:home/c#m_1", "kind": "withdrawn",
                          "host_seq": 1}))
    for name in (S.CHANGES_FILE, S.LINE_ORDERS_FILE, S.HELD_FILE, S.WRITE_KEYS_FILE):
        _tear(reg.dir / name)
    fresh = S.SourceRegistry(tmp_path)
    assert fresh.state_of("lento:home/c#m_1") == "withdrawn"
    assert fresh.claimed("nothing") is None
    assert fresh.members_of("lento:home/c#m_1..m_2") is None
    run(fresh.apply_change({"change_id": "b", "source": "lento:home/c#m_2", "kind": "deleted",
                            "host_seq": 1}))
    assert S.SourceRegistry(tmp_path).state_of("lento:home/c#m_2") == "deleted"
    # The torn bytes stay where they were, now a line of their own in the middle.
    assert S.SourceRegistry(tmp_path).state_of("lento:home/c#m_1") == "withdrawn"


def test_the_usage_log_reads_past_a_torn_line_and_appends_on_a_fresh_one(tmp_path):
    log = U.UsageLog(tmp_path)
    log.record("shown", ["a1"], "breath.recent")
    _tear(log.path)
    assert [r["ids"] for r in log.read()] == [["a1"]]
    log.record("shown", ["b2"], "breath.recent")
    assert [r["ids"] for r in log.read()] == [["a1"], ["b2"]]
    assert log.scrub(["a1"]) == 0 and log.prune() == 0


def test_the_ledger_reads_past_a_torn_line(tmp_path):
    ledger = LedgerMirror(tmp_path / "_ledger" / "events.jsonl")
    ledger.append_event(event_type="TraceCreated", trace_id="x", trace_kind="dynamic")
    _tear(ledger.path)
    fresh = LedgerMirror(ledger.path)
    assert [e["seq"] for e in fresh.iter_events()] == [1]
    assert fresh.latest_seq() == 1
    report = fresh.verify_integrity()
    assert report["valid_events"] == 1 and report["invalid_lines"] == [2]
    assert fresh.append_event(event_type="TraceUpdated", trace_id="x",
                              trace_kind="dynamic")["seq"] == 2
    assert [e["seq"] for e in LedgerMirror(ledger.path).iter_events()] == [1, 2]


def test_a_last_line_longer_than_the_tail_still_numbers_the_next_one(tmp_path):
    path = tmp_path / "_ledger" / "events.jsonl"
    one = LedgerMirror(path)
    one.append_event(event_type="TraceCreated", trace_id="a", trace_kind="dynamic")
    other = LedgerMirror(path)             # another process
    big = other.append_event(event_type="SourceCleared", trace_id="", trace_kind="source",
                             payload={"entries": [f"{i:012x}" for i in range(6000)]})
    assert big["seq"] == 2
    assert one.append_event(event_type="TraceUpdated", trace_id="a",
                            trace_kind="dynamic")["seq"] == 3
    assert [e["seq"] for e in LedgerMirror(path).iter_events()] == [1, 2, 3]


def test_a_short_cursor_key_left_by_a_crash_is_made_again(tmp_path):
    ledger = LedgerMirror(tmp_path / "_ledger" / "events.jsonl")
    key = ledger.path.with_name(L.CURSOR_KEY_FILE)
    key.parent.mkdir(parents=True, exist_ok=True)
    key.write_bytes(b"")                    # created, never written
    cursor = L.cursor_of(ledger, 7, "bot")
    assert L.seq_of_cursor(ledger, cursor, "bot") == 7
    assert len(key.read_bytes()) == 32
    key.write_bytes(b"\x01" * 5)            # torn halfway
    cursor = L.cursor_of(ledger, 9, "bot")
    assert L.seq_of_cursor(ledger, cursor, "bot") == 9
    # A whole key is kept as it is.
    whole = key.read_bytes()
    L.cursor_of(ledger, 1, "bot")
    assert key.read_bytes() == whole
