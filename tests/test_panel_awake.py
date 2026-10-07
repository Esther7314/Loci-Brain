# -*- coding: utf-8 -*-
"""
tests/test_panel_awake.py — the panel's surface page: the awake pool, computed now

`GET /api/loci/awake` lists everything awake with every reason it is awake, in the words
core makes (core/profile.awake_pool, AWAKE_WORDS): 答应了没做 · 日子快到了 (a date already
past says 过了 N 天) · 近三天写的 · 刚被线索碰上 · 自己是条子. Entries awake for a date come
first, the nearest first; the rest newest written first. Paged by offset / limit / as_of.
"""

from datetime import datetime, timedelta
from urllib.parse import urlencode

import pytest

from _panel_kit import make_store, routes, run
from core import _when as W
from core import scope as SC
from core.profile import AWAKE_WORDS, awake_pool

NOW = datetime(2026, 10, 14, 10, 0, 0, tzinfo=W.LOCAL_TZ)


def day(offset: int) -> str:
    return (NOW + timedelta(days=offset)).strftime("%Y-%m-%d")


def bucket(bid, *, created_days_ago=60, room="EVENT/SELF", **meta) -> dict:
    bid = (bid * 12)[:12]
    m = {"id": bid, "room": room, "name": f"entry {bid[:1]}",
         "created": (NOW - timedelta(days=created_days_ago)).isoformat(timespec="seconds")}
    m.update(meta)
    return {"id": bid, "content": f"body {bid}", "metadata": m}


def _rows():
    owed = bucket("a", direction_of_fit="telic", bound=["AI"], when=day(-2))
    return [
        owed,
        bucket("b", room="EVENT/WORLD", when=day(5)),
        bucket("c", created_days_ago=1),
        bucket("d", created_days_ago=2),
        bucket("e"),                                             # asleep: old, no date
        bucket("h", direction_of_fit="telic", bound=["AI"], exception_of=owed["id"],
               hold="defer", when=day(3), created_days_ago=10),
        bucket("z", created_days_ago=1, superseded_by="f" * 12),  # an old version: not awake
    ]


def test_every_reason_in_words_dated_first_nearest_then_newest_written():
    pool = awake_pool(_rows(), NOW, delivered_at={"c" * 12: NOW - timedelta(days=1)})
    assert [r["id"][:1] for r in pool] == ["a", "b", "c", "d", "h"]
    words = {r["id"][:1]: [(x["key"], x["text"]) for x in r["reasons"]] for r in pool}
    # Criterion: all the reasons, not just one; a date already past says how long ago.
    assert words["a"] == [("promised", "答应了没做"), ("dated", "过了 2 天")]
    assert words["b"] == [("dated", "日子快到了")]
    assert words["c"] == [("recent", "近三天写的"), ("cued", "刚被线索碰上")]
    assert words["h"] == [("promised", "答应了没做"), ("hold", "自己是条子")]
    a = pool[0]
    assert a["short"] == a["id"][:6] and a["text"] == "entry a" and a["date"] == day(-2)
    assert pool[2]["date"] == day(-1) and pool[2]["at"].startswith(day(-1))


def test_the_reason_filter_keeps_one_key():
    pool = awake_pool(_rows(), NOW, reason="recent")
    assert [r["id"][:1] for r in pool] == ["c", "d"]
    assert set(AWAKE_WORDS) == {"promised", "dated", "recent", "cued", "hold"}


# ── the route ───────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    return make_store(tmp_path, monkeypatch)


def test_empty_library_is_an_empty_page(store, monkeypatch):
    r = routes(monkeypatch)("GET", "/api/loci/awake")
    assert r.status == 200
    assert r.json["items"] == [] and r.json["total"] == 0 and r.json["next_offset"] is None
    assert r.json["offset"] == 0 and r.json["limit"] == 5
    assert r.json["scope"] == SC.OPEN_LINE and r.json["as_of"]


def test_pages_by_offset_and_limit_and_as_of_holds_the_pages(store, monkeypatch):
    async def seed():
        return [await store.create(f"written now {i}", room="EVENT/SELF", name=f"n{i}")
                for i in range(3)]
    made = run(seed())
    call = routes(monkeypatch)
    first = call("GET", "/api/loci/awake", "limit=2").json
    assert first["total"] == 3 and len(first["items"]) == 2 and first["next_offset"] == 2
    for it in first["items"]:
        assert it["id"] in made and it["short"] == it["id"][:6]
        assert it["reasons"] == [{"key": "recent", "text": "近三天写的"}]
    second = call("GET", "/api/loci/awake",
                  "offset=2&limit=2&" + urlencode({"as_of": first["as_of"]})).json
    assert len(second["items"]) == 1 and second["next_offset"] is None
    assert second["as_of"] == first["as_of"]
    assert {i["id"] for i in first["items"] + second["items"]} == set(made)
    # The "+" of the offset sent unescaped still reads as the same moment.
    loose = call("GET", "/api/loci/awake", f"offset=2&limit=2&as_of={first['as_of']}").json
    assert loose["as_of"] == first["as_of"] and len(loose["items"]) == 1
    # Written after the first page's as_of: not on these pages.
    earlier = (W.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    assert call("GET", "/api/loci/awake", urlencode({"as_of": earlier})).json["total"] == 0


def test_bad_parameters_are_refused_and_the_limit_is_capped(store, monkeypatch):
    call = routes(monkeypatch)
    assert call("GET", "/api/loci/awake", "limit=x").status == 400
    assert call("GET", "/api/loci/awake", "offset=-1").status == 400
    assert call("GET", "/api/loci/awake", "as_of=yesterday-ish").status == 400
    assert call("GET", "/api/loci/awake", "reason=bored").status == 400
    assert call("GET", "/api/loci/awake", "limit=500").json["limit"] == 50
    assert routes(monkeypatch, locked=True)("GET", "/api/loci/awake").status == 401
