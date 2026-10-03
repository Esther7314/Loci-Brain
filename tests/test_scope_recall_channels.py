# -*- coding: utf-8 -*-
"""
tests/test_scope_recall_channels.py — under a read scope, recall names nothing the scope
may not read on any of its side channels, not only the entry itself.

Each test plants one entry out of scope (a source record with `use: venues [private]`,
asked in the group) where a read in scope would mention it in passing, and checks it is
absent under the scope and present without one (the positive control):

  · a read by id: the line to a version it replaced (`wasRevisionOf` is not a source, so
    the entry is readable while that version is not), and a 「疑似同件:<handle>」 tag
  · a search: the root a hit grew out of, when a newer version of that root is out of
    scope (the root walk follows a root to its newest version)
  · a browse: a period covering the days on screen
  · 「今天」: the gist covering an entry of today
"""

import asyncio
import json
from datetime import timedelta

import pytest

from core import _sources as S
from core import _when as W
from core import scope as SC
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.recall import core as R

GROUP = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
BOT = SC.Host("book-bot", max_grant=(S.Place("telegram", "bot-a"),), token="bot-key")
SCOPE = json.dumps({"v": 1, "entry": GROUP, "venue": "group", "audience": ["user:U"],
                    "grant": [GROUP]})
_LINE = iter(range(1, 10_000))


def seen_in(venue: str) -> list[dict]:
    return [{**GROUP, "id": f"m_{next(_LINE)}", "use": {"venues": [venue]}}]


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return mgr


def run(coro):
    return asyncio.run(coro)


def scoped(coro):
    async def go():
        with SC.request_scope(SC.RequestScope.resolve(BOT, SCOPE)):
            return await coro
    return run(go())


def recall(**gates):
    args = {"when": "", "room": "", "tag": "", "query": ""}
    args.update(gates)
    return R.recall_core(**args)


def test_a_read_by_id_does_not_name_a_replaced_version_out_of_scope(store):
    old = run(store.create("The club met on Fridays.", room="EVENT/WORLD",
                           sources=seen_in("private")))
    new = run(store.create("The club meets on Saturdays now.", room="EVENT/WORLD",
                           sources=seen_in("group"),
                           prov=[{"rel": "wasRevisionOf", "target": old}]))
    run(store.update(new, supersedes=old))
    out = scoped(recall(query=new))
    assert "Saturdays now" in out and old not in out, out
    assert old in run(recall(query=new)), "unscoped, the replaced version is named"


def test_a_same_thing_tag_names_only_what_the_scope_may_read(store):
    private = run(store.create("She booked the hall for Saturday.", room="EVENT/WORLD",
                               sources=seen_in("private")))
    other = run(store.create("The hall is booked.", room="EVENT/WORLD", sources=seen_in("group")))
    entry = run(store.create("The club has a hall on Saturday.", room="EVENT/WORLD",
                             sources=seen_in("group"),
                             tags=[f"疑似同件:{private[:6]}", f"疑似同件:{other[:6]}"]))
    out = scoped(recall(query=entry))
    assert f"疑似同件:{other[:6]}" in out, out
    assert private[:6] not in out
    assert f"疑似同件:{private[:6]}" in run(recall(query=entry))


def test_a_search_root_is_not_followed_to_a_version_out_of_scope(store):
    root = run(store.create("A walk along the canal on Sunday.", room="EVENT/SELF",
                            sources=seen_in("group")))
    newer = run(store.create("A long walk along the canal and back.", room="EVENT/SELF",
                             sources=seen_in("private"),
                             prov=[{"rel": "wasRevisionOf", "target": root}]))
    run(store.update(newer, supersedes=root))
    run(store.update(root, superseded_by=newer))
    hit = run(store.create("Walking clears the head; 烤红薯 after.", room="MIND/VIEWS",
                           sources=seen_in("group"),
                           prov=[{"rel": "wasDerivedFrom", "target": root}]))
    out = scoped(recall(query="烤红薯"))
    assert f"({hit[:6]})" in out and f"派生自 {root[:6]}" in out, out
    assert newer[:6] not in out
    assert f"派生自 {newer[:6]}" in run(recall(query="烤红薯")), "unscoped: the newest version"


def _period(store, text: str, venue: str) -> str:
    today = W.today()
    span = f"{(today - timedelta(days=2)):%Y-%m-%d}..{(today + timedelta(days=1)):%Y-%m-%d}"
    return run(store.create(text, room="EVENT/SELF", tags=["__大event__"], when=span,
                            sources=seen_in(venue)))


def test_a_browse_does_not_name_a_period_out_of_scope(store):
    run(store.create("Read two chapters before bed.", room="EVENT/SELF", tags=["读书"],
                     sources=seen_in("group")))
    private = _period(store, "The weeks of the counselling sessions", "private")
    out = scoped(recall(when="7d"))
    assert "two chapters" in out or "读书" in out, out
    assert "counselling" not in out and private[:6] not in out
    assert "counselling" in run(recall(when="7d")), "unscoped, the period is named"
    group = _period(store, "The reading-club fortnight", "group")
    assert group[:6] in scoped(recall(when="7d"))


def _today_under(store, venue: str) -> tuple[str, str]:
    member = run(store.create("Swept the balcony this morning.", room="EVENT/SELF",
                              sources=seen_in("group")))
    gist = run(store.create(f"A tidy morning ({venue}).", room="EVENT/SELF",
                            sources=seen_in(venue)))
    run(store.update(gist, cover=[member]))
    run(store.update(member, covered_by=[gist]))
    return member, gist


def test_today_does_not_name_a_covering_gist_out_of_scope(store):
    member, gist = _today_under(store, "private")
    out = scoped(recall(when="今天"))
    assert f"({member[:6]})" in out, "its cover is out of scope: the entry stands for itself"
    assert gist[:6] not in out and "盖着这里" not in out, out
    assert f"{gist[:6]}" in run(recall(when="今天"))


def test_today_names_a_covering_gist_in_scope(store):
    member, gist = _today_under(store, "group")
    out = scoped(recall(when="今天"))
    assert gist[:6] in out and "盖着这里 1 条" in out, out
