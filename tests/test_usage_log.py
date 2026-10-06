# -*- coding: utf-8 -*-
"""
tests/test_usage_log.py — what Loci handed out and what a write stood on, by code.

One memory goes into breath (shown), is found by recall (found, with the query as typed),
and a thought is grown from it (source): three lines, written by code, none of them in the
ledger. Under a read scope only what the scope let through is recorded. A lookup with no
hit is still a line. Old lines go after the retention; a source change takes the query off
a lookup that listed what it cleared.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

import tools.grow as grow_mod
from core import _usage as U
from core import scope as SC
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.breath import awaken as A
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.recall import dispatch as recall

TG = {"system": "telegram", "instance": "bot-a", "container": "g", "id": "1"}
HOME = {"system": "lento", "instance": "home", "container": "p", "id": "1"}


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def _kinds(store, bid):
    return [(r["kind"], r["road"]) for r in store.usage.read() if bid in r["ids"]]


def test_shown_found_and_used_are_three_lines_by_code(store):
    bid = run(store.create("周六和小周去海边看日出。", room="EVENT/WORLD", sources=[HOME]))
    text = run(A.surface_awaken())
    assert bid[:6] in text
    assert ("shown", "breath.recent") in _kinds(store, bid)
    out = run(recall(query="海边看日出"))
    assert bid[:6] in out
    found = [r for r in store.usage.read() if r["kind"] == "found"]
    assert found[-1]["ids"] == [bid] and found[-1]["query"] == "海边看日出"
    assert found[-1]["road"] == "recall.search" and found[-1]["gates"]["when"] == ""
    out = run(grow(kind="mind", room="MIND/TRAITS", text="小周喜欢早起看海。", from_=[bid],
                   v=0.6, a=0.3))
    assert "🧠mind→" in out, out
    assert ("source", "grow") in _kinds(store, bid)
    ledger = store.ledger_mirror.path.read_text(encoding="utf-8")
    assert "海边看日出" not in ledger and "usage" not in ledger


def test_shown_and_source_lines_hold_ids_only(store):
    bid = run(store.create("早上的事。", room="EVENT/WORLD", sources=[HOME]))
    run(A.surface_awaken())
    run(store.touch_many([bid], road="regrow"))
    for row in store.usage.read():
        if row["kind"] != "found":
            assert set(row) <= {"at", "kind", "road", "ids", "host", "key"}


def test_a_lookup_that_finds_nothing_is_still_a_line(store):
    run(recall(query="根本没有这回事"))
    [row] = [r for r in store.usage.read() if r["kind"] == "found"]
    assert row["ids"] == [] and row["query"] == "根本没有这回事"


def test_under_a_scope_only_what_was_let_through_is_recorded(store):
    mine = run(store.create("群里说周末爬山。", room="EVENT/WORLD", sources=[TG]))
    theirs = run(store.create("家里说周末爬山。", room="EVENT/WORLD", sources=[HOME]))
    host = SC.Host("bot", max_grant=(SC._src.Place("telegram", "bot-a"),))
    req = SC.RequestScope.resolve(host, json.dumps({
        "v": 1, "entry": {"system": "telegram", "instance": "bot-a"}, "venue": "group",
        "audience": ["u"], "grant": [{"system": "telegram", "instance": "bot-a"}]}))
    with SC.request_scope(req):
        run(A.surface_awaken())
        run(recall(query="周末爬山"))
    rows = store.usage.read()
    seen = {i for r in rows for i in r["ids"]}
    assert mine in seen and theirs not in seen
    assert all(r.get("host") == "bot" for r in rows)


def test_old_lines_go_after_the_retention(tmp_path):
    log = U.UsageLog(tmp_path, retain_days=7)
    log.record("shown", ["a"], "breath.recent")
    old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat(timespec="seconds")
    with log.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": old, "kind": "shown", "road": "x", "ids": ["b"]}) + "\n")
    assert log.prune() == 1
    assert [r["ids"] for r in log.read()] == [["a"]]
    assert U.UsageLog(tmp_path, retain_days="nonsense").retain_days == U.DEFAULT_RETAIN_DAYS


def test_scrub_takes_the_query_off_and_keeps_the_ids(tmp_path):
    log = U.UsageLog(tmp_path)
    log.record("found", ["a"], "recall.search", query="青柠", gates={"when": "3d"})
    log.record("found", ["b"], "recall.search", query="别的")
    log.record("found", [], "recall.search", query="说起青柠汽水")
    assert log.scrub({"a"}, lambda q: "青柠汽水" in q) == 2
    rows = log.read()
    assert rows[0] == {k: v for k, v in rows[0].items() if k not in ("query", "gates")}
    assert rows[0]["ids"] == ["a"] and rows[1]["query"] == "别的" and "query" not in rows[2]


def test_the_same_source_without_fingerprint_or_revision_only_says_referenced(store):
    bare = dict(HOME)
    first = run(grow(kind="event", items=[{"room": "EVENT/WORLD", "text": "第一件事。",
                                           "v": 0.5, "a": 0.3}], sources=[bare]))
    first_id = first.split("📝", 1)[1].split()[0]
    out = run(grow(kind="event", items=[{"room": "EVENT/WORLD", "text": "第二件事。",
                                         "v": 0.5, "a": 0.3}], sources=[bare]))
    assert f"{first_id} 也引过同一条来源" in out and "说不准" in out
    assert "已经记过" not in out
    revised = {**HOME, "id": "2", "revision": "3"}
    first = run(grow(kind="event", items=[{"room": "EVENT/WORLD", "text": "第三件事。",
                                           "v": 0.5, "a": 0.3}], sources=[revised]))
    out = run(grow(kind="event", items=[{"room": "EVENT/WORLD", "text": "第四件事。",
                                         "v": 0.5, "a": 0.3}], sources=[revised]))
    assert "这条来源已经记过" in out, "a named revision says the same delivery"
