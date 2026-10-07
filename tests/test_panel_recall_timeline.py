# -*- coding: utf-8 -*-
"""
tests/test_panel_recall_timeline.py — recall's empty-box timeline and the usage counts.

GET /api/loci/recall/timeline: the last three natural days, 「我搜 X」 lines (the usage
log's lookups that typed a query) and 「你说 X」 lines (turns the card ledger handed cards
to), mixed, newest first, each with its id (`u_…` / `<window>/<turn>`). A browse without a
query, a turn whose answer was empty and anything older than the day before yesterday are
not lines. GET /api/loci/usage: per memory, how often shown / found / stood on; shown and
never stood on first.
"""

import asyncio
import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from core import _usage as U
from core import _when as W
from core import activity as ACT
from core.bucket_manager import BucketManager


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))

    def get(route, **query):
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": route, "path_params": {},
                       "headers": [], "query_string": urlencode(query).encode()})
        resp = run(routes[("GET", route)](req))
        return resp.status_code, json.loads(resp.body)
    return {"store": store, "get": get}


def _at(**delta) -> str:
    return (W.now() + timedelta(**delta)).isoformat(timespec="seconds")


def _usage(store, **row) -> dict:
    store.usage.path.parent.mkdir(parents=True, exist_ok=True)
    row.setdefault("at", _at())
    with store.usage.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")
    return row


def _ledger_row(store, **row):
    store.cues.path.parent.mkdir(parents=True, exist_ok=True)
    with store.cues.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture
def seeded(panel):
    store = panel["store"]

    async def seed():
        a = await store.create("考完去吃那家甜品。", room="EVENT/SELF")
        b = await store.create("累的时候先别讲道理。", room="MIND/TRAITS")
        return a, b
    a, b = run(seed())
    _ledger_row(store, op="offer", at=_at(days=-5), host="life", window="w0", turn="old",
                cards=[{"card": f"{a}@old", "id": a, "kind": "memory", "why": "很久以前"}])
    led = store.cues
    led.open_window("life", "w1", [])
    card = {"card": f"{a}@aaaa", "id": a, "kind": "memory", "why": "考完"}
    led.offer("life", "w1", "t1", [card])
    led.deliver("life", "w1", turn="t1")
    led.offer("life", "w1", "t2", [])
    search = _usage(store, at=_at(minutes=-30), kind="found", road="recall.search",
                    ids=[a, b], query="甜品", host="life")
    _usage(store, at=_at(minutes=-20), kind="found", road="recall.browse", ids=[a],
           query="", host="life")
    _usage(store, at=_at(days=-5), kind="found", road="recall.search", ids=[a],
           query="很久以前搜的", host="life")
    _usage(store, at=_at(minutes=-10), kind="shown", road="breath.recent", ids=[a])
    return {**panel, "a": a, "b": b, "search": search, "card": card}


def test_searches_and_cards_mix_newest_first(seeded):
    status, out = seeded["get"]("/api/loci/recall/timeline")
    assert status == 200, out
    assert out["scope"] == "〔范围：全库（open）〕"
    assert [i["kind"] for i in out["items"]] == ["card", "search"]
    card, search = out["items"]
    assert card["id"] == "w1/t1" and card["said"] == "考完" and card["host"] == "life"
    [c] = card["cards"]
    assert c["id"] == seeded["a"] and c["state"] == "delivered" and c["state_words"] == "送到了"
    assert c["text"] == "考完去吃那家甜品。"
    assert search["id"] == U.row_id(seeded["search"]) and search["id"].startswith("u_")
    assert search["query"] == "甜品" and search["n"] == 2
    assert search["ids"] == [seeded["a"], seeded["b"]]
    text = json.dumps(out, ensure_ascii=False)
    assert "很久以前" not in text, "older than the day before yesterday"
    assert out["total"] == 2


def test_the_timeline_pages_with_as_of(seeded):
    get = seeded["get"]
    _s, first = get("/api/loci/recall/timeline", limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1 and len(first["items"]) == 1
    _usage(seeded["store"], at=_at(minutes=5), kind="found", road="recall.search",
           ids=[], query="翻页时进来的", host="life")
    _s, second = get("/api/loci/recall/timeline", limit=1, offset=1, as_of=first["as_of"])
    assert second["total"] == 2 and second["items"][0]["kind"] == "search"
    assert second["next_offset"] is None
    _s, fresh = get("/api/loci/recall/timeline", as_of=_at(minutes=6))
    assert fresh["total"] == 3 and fresh["items"][0]["query"] == "翻页时进来的"
    assert fresh["items"][0]["n"] == 0


def test_an_empty_library_has_an_empty_timeline(panel):
    status, out = panel["get"]("/api/loci/recall/timeline")
    assert status == 200
    assert out["items"] == [] and out["total"] == 0 and out["next_offset"] is None
    assert out["offset"] == 0 and out["limit"] == 5 and out["as_of"]


def test_a_non_panel_read_never_carries_the_query(seeded):
    store = seeded["store"]
    now = W.now()
    out = ACT.timeline(store.cues.events(), store.usage.read(),
                       run(store.list_all(include_archive=True)), now=now, offset=0,
                       limit=50, as_of=now + timedelta(minutes=1), panel=False)
    text = json.dumps(out, ensure_ascii=False)
    assert '"query"' not in text and "甜品\"" not in text


def test_usage_counts_put_the_shown_and_never_used_first(seeded):
    store, a, b = seeded["store"], seeded["a"], seeded["b"]
    for _ in range(2):
        _usage(store, kind="shown", road="breath.recent", ids=[b])
    _usage(store, kind="source", road="grow", ids=[b])
    _usage(store, at=_at(days=-40), kind="shown", road="breath.recent", ids=[b, a])
    status, out = seeded["get"]("/api/loci/usage")
    assert status == 200, out
    rows = {r["id"]: r for r in out["items"]}
    assert [r["id"] for r in out["items"]] == [a, b]
    assert rows[a]["shown"] == 1 and rows[a]["found"] == 3 and rows[a]["source"] == 0
    assert rows[b]["shown"] == 2 and rows[b]["found"] == 1 and rows[b]["source"] == 1
    assert rows[a]["short"] == a[:6] and rows[a]["text"] == "考完去吃那家甜品。"
    assert out["since"] == (W.today() - timedelta(days=30)).date().isoformat()
    assert "query" not in json.dumps(out, ensure_ascii=False)


def test_usage_since_and_paging(seeded):
    get = seeded["get"]
    _s, out = get("/api/loci/usage", since=(W.now() - timedelta(days=1)).date().isoformat(),
                  limit=1)
    assert out["total"] == 2 and len(out["items"]) == 1 and out["next_offset"] == 1
    _s, old = get("/api/loci/usage", as_of=_at(days=-4))
    assert old["total"] == 1 and old["items"][0]["id"] == seeded["a"]


def test_usage_of_an_empty_library(panel):
    status, out = panel["get"]("/api/loci/usage")
    assert status == 200 and out["items"] == [] and out["total"] == 0
