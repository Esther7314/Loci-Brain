# -*- coding: utf-8 -*-
"""
tests/test_panel_turns.py — GET /api/loci/turns/{window}: one window, turn by turn.

Each turn carries the cards it was handed and what became of each (offered, delivered,
dropped), what breath showed and recall found in that turn (joined by the usage line's
write key), and the holds live now on any of them. Turns come newest first and page
with as_of. The search's query reaches the panel and no host: a host credential gets
nothing from this route, and a non-panel read leaves the query out.
"""

import asyncio
import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from core import _when as W
from core import activity as ACT
from core.bucket_manager import BucketManager

HOME = {"system": "lento", "instance": "home", "container": "p", "id": "1"}


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

    def get(route, path_params=None, headers=(), **query):
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": route,
                       "path_params": path_params or {}, "headers": list(headers),
                       "query_string": urlencode(query).encode()})
        resp = run(routes[("GET", route)](req))
        return resp.status_code, json.loads(resp.body)
    return {"store": store, "get": get, "sh": sh, "PA": PA, "routes": routes}


def _usage(store, **row):
    store.usage.path.parent.mkdir(parents=True, exist_ok=True)
    row.setdefault("at", W.now().isoformat(timespec="seconds"))
    with store.usage.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _ledger_row(store, **row):
    store.cues.path.parent.mkdir(parents=True, exist_ok=True)
    with store.cues.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


@pytest.fixture
def seeded(panel):
    store = panel["store"]

    async def seed():
        sweet = await store.create("考完去吃那家甜品。", room="EVENT/SELF", sources=[HOME])
        tooth = await store.create("周末陪去看牙。", room="EVENT/SELF",
                                   direction_of_fit="telic", bound=["AI"])
        hold = await store.create("看牙这事先别催。", room="EVENT/SELF",
                                  direction_of_fit="telic", bound=["AI"],
                                  exception_of=tooth, hold="defer",
                                  when=(W.now() + timedelta(days=5)).strftime("%Y-%m-%d"))
        return sweet, tooth, hold
    sweet, tooth, hold = run(seed())
    led = store.cues
    led.open_window("life", "w1", [])
    a = {"card": f"{sweet}@aaaa", "id": sweet, "kind": "memory", "why": "考完"}
    name = {"card": "name:小林@bbbb", "id": "", "kind": "name_bare", "why": "小林"}
    b = {"card": f"{tooth}@cccc", "id": tooth, "kind": "due", "why": "due"}
    led.offer("life", "w1", "t1", [a, name])
    led.deliver("life", "w1", turn="t1")
    led.offer("life", "w1", "t2", [b])
    led.deliver("life", "w1", cards=[b["card"]])
    led.drop("life", "w1", cards=[b["card"]])
    led.offer("life", "w1", "t3", [])
    _usage(store, kind="found", road="recall.search", ids=[sweet], query="甜品",
           host="life", key="life:t1#1")
    _usage(store, kind="shown", road="breath.prospective", ids=[tooth], host="life",
           key="life:t2#1")
    _usage(store, kind="found", road="recall.search", ids=[tooth], query="别的宿主搜的",
           host="bot", key="bot:t1#1")
    return {**panel, "sweet": sweet, "tooth": tooth, "hold": hold, "a": a, "b": b}


def test_each_turn_carries_its_cards_and_what_became_of_them(seeded):
    status, out = seeded["get"]("/api/loci/turns/{window}", {"window": "w1"}, host="life")
    assert status == 200, out
    assert out["host"] == "life" and out["window"] == "w1" and out["opened"]
    assert out["scope"] == "〔范围：全库（open）〕"
    assert [t["turn"] for t in out["items"]] == ["t3", "t2", "t1"], "newest first"
    t3, t2, t1 = out["items"]
    assert t3["cards"] == [] and t3["shown"] == [] and t3["found"] == []

    by_card = {c["card"]: c for c in t1["cards"]}
    sweet = by_card[seeded["a"]["card"]]
    assert sweet["id"] == seeded["sweet"] and sweet["short"] == seeded["sweet"][:6]
    assert sweet["state"] == "delivered" and sweet["state_words"] == "送到了"
    assert sweet["kind_words"] == "相关记忆" and sweet["why"] == "考完"
    assert sweet["text"] == "考完去吃那家甜品。" and sweet["has_original"] is True
    bare = by_card["name:小林@bbbb"]
    assert bare["text"] == "小林" and bare["id"] == "" and bare["state"] == "delivered"

    [due] = t2["cards"]
    assert due["state"] == "dropped" and due["state_words"] == "丢了"
    assert due["has_original"] is False


def test_shown_and_found_join_the_turn_by_its_write_key(seeded):
    _s, out = seeded["get"]("/api/loci/turns/{window}", {"window": "w1"}, host="life")
    t3, t2, t1 = out["items"]
    [found] = t1["found"]
    assert found["ids"] == [seeded["sweet"]] and found["query"] == "甜品"
    assert found["id"].startswith("u_") and len(found["id"]) == 12
    assert t1["shown"] == []
    [shown] = t2["shown"]
    assert shown["road"] == "breath.prospective" and shown["ids"] == [seeded["tooth"]]
    assert "query" not in shown
    assert "别的宿主搜的" not in json.dumps(out, ensure_ascii=False), \
        "another host's turn of the same name is not this window's"


def test_holds_live_on_what_the_turn_touched(seeded):
    _s, out = seeded["get"]("/api/loci/turns/{window}", {"window": "w1"}, host="life")
    t2 = out["items"][1]
    [hold] = t2["holds"]
    assert hold["id"] == seeded["hold"] and hold["on"] == seeded["tooth"]
    assert hold["hold"] == "defer" and hold["words"] == "先别催" and hold["until"]
    assert out["items"][2]["holds"] == []


def test_pages_hold_still_while_new_turns_arrive(seeded):
    get = seeded["get"]
    status, first = get("/api/loci/turns/{window}", {"window": "w1"}, host="life", limit=2)
    assert status == 200
    assert [t["turn"] for t in first["items"]] == ["t3", "t2"]
    assert first["total"] == 3 and first["next_offset"] == 2 and first["limit"] == 2
    later = (W.now() + timedelta(minutes=5)).isoformat(timespec="seconds")
    _ledger_row(seeded["store"], op="offer", at=later, host="life", window="w1",
                turn="t4", cards=[])
    _s, second = get("/api/loci/turns/{window}", {"window": "w1"}, host="life", limit=2,
                     offset=2, as_of=first["as_of"])
    assert [t["turn"] for t in second["items"]] == ["t1"]
    assert second["total"] == 3 and second["next_offset"] is None
    assert second["as_of"] == first["as_of"]
    _s, fresh = get("/api/loci/turns/{window}", {"window": "w1"}, host="life",
                    as_of=later)
    assert fresh["total"] == 4 and fresh["items"][0]["turn"] == "t4"


def test_unknown_window_and_missing_host(panel):
    get = panel["get"]
    status, out = get("/api/loci/turns/{window}", {"window": "nope"}, host="life")
    assert status == 404 and "nope" in out["error"]
    status, out = get("/api/loci/turns/{window}", {"window": "w1"})
    assert status == 400 and "host" in out["error"]


def test_a_non_panel_read_never_carries_the_query(seeded):
    store = seeded["store"]
    now = W.now()
    out = ACT.turns(store.cues, store.usage.read(), run(store.list_all(include_archive=True)),
                    host="life", window="w1", now=now, offset=0, limit=5,
                    as_of=now + timedelta(minutes=1), panel=False)
    text = json.dumps(out, ensure_ascii=False)
    assert "甜品" not in text.replace("考完去吃那家甜品。", "") and '"query"' not in text


def test_a_host_credential_gets_nothing_from_the_panel_route(seeded, monkeypatch):
    sh, PA = seeded["sh"], seeded["PA"]
    monkeypatch.setenv("T_LIFE", "life-key")
    monkeypatch.setattr(sh, "config", {**sh.config, "hosts": {
        "life": {"token_env": "T_LIFE", "scope_mode": "open"}}})
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    monkeypatch.setattr(PA, "has_session", lambda r: True)
    status, out = seeded["get"]("/api/loci/turns/{window}", {"window": "w1"},
                                headers=[(b"x-loci-hook-token", b"life-key")], host="life")
    assert status == 403 and "甜品" not in json.dumps(out, ensure_ascii=False)
    for route in ("/api/loci/turns/{window}", "/api/loci/recall/timeline", "/api/loci/usage",
                  "/api/loci/grow/today", "/api/loci/grow/slices", "/api/loci/muse",
                  "/api/loci/embedding/missing", "/api/loci/embedding/backfill"):
        assert not PA.is_hook(route) and not PA.is_public(route), route
        assert any(path == route for _m, path in seeded["routes"]), route
