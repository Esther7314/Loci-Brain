# -*- coding: utf-8 -*-
"""
tests/test_recall_search_lines.py — what one search line says, and in what order.

A search line carries how the hit matched (字面 / 意思 / 部分字面) right after its score and
how many days ago it was written. Hits that grew from the same root are one line that still
names every other hit by its id. Lines holding an open promise go first, newest first within
each part — and only among what the search matched. The panel's search results are these
same lines (`rows`), paged by the route.
"""

import asyncio
import re
from datetime import timedelta

import pytest

from core import _when as W
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.recall import core as R


class SilentLogger:
    def _noop(self, *a, **k):
        return None
    warning = info = debug = error = _noop


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", SilentLogger())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return mgr


async def _new(store, text, **fields):
    return await store.create(text, tags=["t"], name=text[:12], room="EVENT/SELF", **fields)


def _search(query):
    return R.recall_core(when="", room="", tag="", query=query)


def _lines(out: str) -> list[str]:
    """The entry lines of a search view (they open with a score)."""
    return [ln for ln in out.splitlines() if re.match(r"^\s*\d+\.\d\s", ln)]


def _line_of(out: str, bid: str) -> str:
    found = [ln for ln in _lines(out) if f"({bid[:6]})" in ln]
    assert len(found) == 1, out
    return found[0]


def test_a_line_says_how_it_matched_and_how_long_ago_it_was_written(store, monkeypatch):
    async def go():
        bid = await _new(store, "楼下新开了一家卖烤红薯的小摊。")
        later = W.now() + timedelta(days=5)
        monkeypatch.setattr(W, "now", lambda: later)
        line = _line_of(await _search("烤红薯"), bid)
        assert re.match(r"^\s*\d+\.\d 字面  ", line), line
        assert line.endswith("· 5天前写的"), line
    asyncio.run(go())


def test_the_mark_is_what_holds():
    # The two sides of the score, never a third number: both when both hold; a hit with
    # neither came over on some of the words, or on a weaker closeness in meaning alone.
    assert R._how_mark({"literal": True, "meaning": True}) == "字面+意思"
    assert R._how_mark({"literal": True}) == "字面"
    assert R._how_mark({"meaning": True, "words": True}) == "意思"
    assert R._how_mark({"words": True}) == "部分字面"
    assert R._how_mark({}) == "意思"


def test_hits_from_one_root_are_one_line_that_names_every_hit(store):
    async def go():
        root = await _new(store, "小周说周六想去海边看日落。")
        a = await _new(store, "小周喜欢海边，日落的时候心情会变好。",
                       prov=[{"rel": "wasDerivedFrom", "target": root}])
        b = await _new(store, "海边那次小周提了两遍，大概真的很想去。",
                       prov=[{"rel": "wasDerivedFrom", "target": a}])
        out = await _search("海边")
        lines = _lines(out)
        assert len(lines) == 1, out
        line = lines[0]
        assert f"({root[:6]})" in line
        assert "＋2 条派生：" in line and a[:6] in line and b[:6] in line, line
        assert "3 条，出自 1 个根" in out
    asyncio.run(go())


def test_a_root_that_did_not_match_is_named_with_why(store):
    async def go():
        root = await _new(store, "周六在码头吃了一碗鱼丸面。")
        kid = await _new(store, "小周说海边的风很舒服。",
                         prov=[{"rel": "wasDerivedFrom", "target": root}])
        line = _line_of(await _search("海边"), kid)
        assert f"派生自 {root[:6]}（这次没列）" in line, line

        assert await store.delete(root)
        line = _line_of(await _search("海边"), kid)
        assert f"派生自 {root[:6]} ⚠️在归档区" in line, line
    asyncio.run(go())


def test_a_newer_version_of_the_root_stands_for_it(store):
    # A revision is the same entry, not a source: what grew from the old wording and the
    # new wording itself are one origin.
    async def go():
        old = await _new(store, "海边那家店周一休息。")
        new = await _new(store, "海边那家店周二休息，之前记错了。",
                         prov=[{"rel": "wasRevisionOf", "target": old}])
        assert await store.update(old, superseded_by=new)
        kid = await _new(store, "去海边那家店之前得先看看今天是星期几。",
                         prov=[{"rel": "wasDerivedFrom", "target": old}])
        lines = _lines(await _search("海边"))
        assert len(lines) == 1 and f"({new[:6]})" in lines[0] and kid[:6] in lines[0], lines
    asyncio.run(go())


def test_open_promises_go_first_then_newest_first(store, monkeypatch):
    # The order, in one sentence: lines holding an open promise first, then the rest;
    # newest first within each (by the date the line shows).
    async def go():
        older_promise = await _new(store, "答应小周找一天去海边放风筝。", when="2026-09-01",
                                   direction_of_fit="telic", bound=["AI"])
        newest = await _new(store, "海边下了一整天雨。", when="2026-09-20")
        closed = await _new(store, "答应过陪小周去海边捡贝壳，已经去过了。", when="2026-09-25",
                            direction_of_fit="telic", bound=["AI"])
        assert await store.update(closed, status="resolved")
        middle = await _new(store, "小周把海边的照片设成了屏保。", when="2026-09-10")
        newer_promise = await _new(store, "答应小周下次去海边带上相机。", when="2026-09-05",
                                   direction_of_fit="telic", bound=["AI"])
        # A promise that does not match stays out: rising is only among what matched.
        elsewhere = await _new(store, "答应小周周末修好台灯。", direction_of_fit="telic",
                               bound=["AI"])
        out = await _search("海边")
        order = [re.search(r"\(([0-9a-f]{6})\)", ln).group(1) for ln in _lines(out)]
        assert order == [x[:6] for x in (newer_promise, older_promise, closed, newest, middle)], out
        assert elsewhere[:6] not in out
        assert "答应了还没关的在最前" in out
        assert R._PROMISE_MARK in _line_of(out, newer_promise)
        assert R._PROMISE_MARK not in _line_of(out, closed)
    asyncio.run(go())


def test_the_panel_gets_the_same_facts(store):
    async def go():
        root = await _new(store, "小周说周六想去海边看日落。")
        kid = await _new(store, "小周喜欢海边。", prov=[{"rel": "wasDerivedFrom", "target": root}])
        data = await R.recall_data(when="", room="", tag="", query="海边")
        by_id = {e["id"]: e for e in data["entries"]}
        assert by_id[kid]["roots"] == [root] and by_id[root]["roots"] == [root]
        assert by_id[kid]["how"] == "字面" and by_id[kid]["written_days"] == 0
        assert by_id[kid]["open_promise"] is False
    asyncio.run(go())


def test_the_panel_lines_are_the_text_skins_lines(store):
    async def go():
        root = await _new(store, "小周说周六想去海边看日落。")
        a = await _new(store, "小周喜欢海边，日落的时候心情会变好。",
                       prov=[{"rel": "wasDerivedFrom", "target": root}])
        b = await _new(store, "海边那次小周提了两遍，大概真的很想去。",
                       prov=[{"rel": "wasDerivedFrom", "target": a}])
        data = await R.recall_data(when="", room="", tag="", query="海边")
        [line] = data["rows"]
        assert line["id"] == root and line["short"] == root[:6]
        assert line["how"] == "字面" and line["roots"] == [root]
        assert line["score"] >= data["floor"] and line["written_words"] == "今天写的"
        assert {o["id"] for o in line["others"]} == {a, b}, "what 「+ N 条派生」 counts"
        assert all(o["open_promise"] is False for o in line["others"])
    asyncio.run(go())


def test_the_panel_lines_keep_the_order_and_leave_out_what_is_under_the_line(store):
    async def go():
        promise = await _new(store, "答应小周找一天去海边放风筝。", when="2026-09-01",
                             direction_of_fit="telic", bound=["AI"])
        newest = await _new(store, "海边下了一整天雨。", when="2026-09-20")
        older = await _new(store, "小周把海边的照片设成了屏保。", when="2026-09-10")
        data = await R.recall_data(when="", room="", tag="", query="海边")
        assert [r["id"] for r in data["rows"]] == [promise, newest, older]
        assert [r["open_promise"] for r in data["rows"]] == [True, False, False]
    asyncio.run(go())


def test_a_hit_under_the_line_is_not_a_panel_line():
    from datetime import datetime

    def hit(bid, score, literal):
        return {"id": bid, "meta": {"name": bid}, "content": bid, "score": score,
                "literal": literal, "ts": datetime(2026, 9, 1), "roots": frozenset({bid})}
    rows = R.search_rows_json([hit("aaaaaaaaaaaa", 20.0, False),
                               hit("bbbbbbbbbbbb", 20.0, True)], 35.0)
    assert [r["id"] for r in rows] == ["bbbbbbbbbbbb"], "a literal hit is lifted to the line"
    assert rows[0]["score"] == 35.0


def test_without_a_query_every_entry_is_a_line_newest_first(store):
    async def go():
        first = await _new(store, "九月一号记下的一件事。", when="2026-09-01")
        second = await _new(store, "九月五号记下的一件事。", when="2026-09-05")
        data = await R.recall_data(when="2026-09", room="", tag="", query="")
        assert [r["id"] for r in data["rows"]] == [second, first]
        assert "score" not in data["rows"][0] and data["rows"][0]["others"] == []
        assert data["rows"][0]["date"] == "2026-09-05"
    asyncio.run(go())


def test_the_route_pages_the_lines(store, monkeypatch):
    import json
    from urllib.parse import urlencode
    from starlette.requests import Request
    from web import loci_reads as WR

    def get(**query):
        req = Request({"type": "http", "method": "GET", "path": "/api/loci/recall",
                       "path_params": {}, "headers": [],
                       "query_string": urlencode(query).encode()})
        resp = asyncio.run(WR.api_loci_recall(req))
        return resp.status_code, json.loads(resp.body)

    async def seed():
        return [await _new(store, f"海边的第 {n} 件事。", when=f"2026-09-{n:02d}")
                for n in range(1, 8)]
    ids = asyncio.run(seed())
    status, out = get(query="海边")
    assert status == 200, out
    rows = out["rows"]
    assert rows["total"] == 7 and rows["offset"] == 0 and rows["next_offset"] == 5
    assert [r["id"] for r in rows["items"]] == ids[::-1][:5]
    _s, more = get(query="海边", offset=5, as_of=rows["as_of"])
    assert [r["id"] for r in more["rows"]["items"]] == ids[::-1][5:]
    assert more["rows"]["next_offset"] is None
    status, bad = get(query="海边", offset="x")
    assert status == 400 and bad["error"]
