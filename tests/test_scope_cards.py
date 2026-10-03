# -*- coding: utf-8 -*-
"""
tests/test_scope_cards.py — under a read scope, every kind of strong-reminder card is
produced for what the scope may read and for nothing else.

The library holds the same thing twice, once from the group (`use: venues [group]`) and
once from a private chat (`venues [private]`), both inside the granted container, so only
the use decides. Asked in the group, each kind of card — 提醒 (due), 相关记忆 (a cue),
依据变了 (review), 相关名字 with a card and without one — arrives for the group entry
(the positive control) and never for the private one, not its id, not its count; asked
without a scope, both arrive as before. A name page the scope may not read does not keep
a name card back either.
"""

import asyncio
import json

from core import _cue as C
from core import scope as SCOPE

from test_cue import TODAY, Store, at, bucket, clock, names  # noqa: F401

TG = {"system": "telegram", "instance": "b", "container": "g"}


def rec(line: str, venue: str) -> dict:
    return {**TG, "id": line, "revision": "r1", "use": {"venues": [venue]}}


def view(store):
    req = SCOPE.RequestScope.resolve(
        SCOPE.Host("bot", max_grant=None),
        json.dumps({"v": 1, "entry": TG, "venue": "group", "audience": ["user:U"],
                    "grant": [TG]}))
    return SCOPE.ScopeView(req, {r["id"]: r["metadata"] for r in store.rows}, store.sources)


def cards(store, text, *, scoped: bool, turn="t1") -> dict:
    """One message's cards, in the group's window under the scope or the owner's without."""
    host, window = ("bot", "wg") if scoped else ("life", "wo")
    return asyncio.run(C.cue(store, text=text, window=window, turn=turn, host=host,
                             scope=view(store) if scoped else None))


def ids(out) -> list[str]:
    return sorted(c["id"] for c in out["cards"])


def kinds(out) -> list[str]:
    return sorted(c["kind"] for c in out["cards"])


def test_a_due_card_only_for_what_the_scope_may_read(tmp_path, clock, names):
    group = bucket("a", "答应今晚回群里说结果。", created=at(10), direction_of_fit="telic",
                   bound=["AI"], when="2026-10-01T19:00+08:00", sources=[rec("m_1", "group")])
    private = bucket("b", "答应今晚私下说体检结果。", created=at(10), direction_of_fit="telic",
                     bound=["AI"], when="2026-10-01T19:00+08:00",
                     sources=[rec("m_2", "private")])
    store = Store(tmp_path, [group, private])
    clock["t"] = at(10, 30)
    assert cards(store, "早", scoped=True)["cards"] == []
    assert cards(store, "早", scoped=False)["cards"] == []
    clock["t"] = at(19, 10)
    out = cards(store, "回来了", scoped=True, turn="t2")
    assert kinds(out) == ["due"] and ids(out) == [group["id"]]
    assert private["id"][:6] not in out["text"] and "体检" not in out["text"]
    assert ids(cards(store, "回来了", scoped=False, turn="t2")) == sorted(
        [group["id"], private["id"]])


def test_a_cue_card_only_for_what_the_scope_may_read(tmp_path, clock, names):
    cue = {"condition": "去甜品店", "phrasings": ["甜品店"]}
    group = bucket("c", "群里约好去那家甜品店。", direction_of_fit="telic", cue=cue,
                   sources=[rec("m_3", "group")])
    private = bucket("d", "私下说想一个人去甜品店。", direction_of_fit="telic", cue=cue,
                     sources=[rec("m_4", "private")])
    store = Store(tmp_path, [group, private])
    out = cards(store, "甜品店开门了", scoped=True)
    assert kinds(out) == ["memory"] and ids(out) == [group["id"]]
    assert private["id"][:6] not in out["text"]
    assert ids(cards(store, "甜品店开门了", scoped=False)) == sorted([group["id"], private["id"]])


def test_a_review_card_only_for_what_the_scope_may_read(tmp_path, clock, names):
    group = bucket("e", "群里定的周六集合时间。", sources=[rec("m_5", "group")])
    private = bucket("f", "私下说的周六安排。", sources=[rec("m_6", "private")])
    store = Store(tmp_path, [group, private])
    assert cards(store, "早", scoped=True)["cards"] == []
    assert cards(store, "早", scoped=False)["cards"] == []
    # Both sources come out in a new revision after the windows opened.
    for n, line in enumerate(("m_5", "m_6"), start=1):
        asyncio.run(store.sources.apply_change({
            "change_id": f"rev-{line}", "source": f"telegram:b/g#{line}", "kind": "revised",
            "host_seq": n, "revision": "r2"}))
    out = cards(store, "嗯", scoped=True, turn="t2")
    assert kinds(out) == ["review"] and ids(out) == [group["id"]]
    assert private["id"][:6] not in out["text"] and "m_6" not in out["text"]
    assert ids(cards(store, "嗯", scoped=False, turn="t2")) == sorted([group["id"], private["id"]])


def test_a_name_card_and_its_count_only_for_what_the_scope_may_read(tmp_path, clock, names):
    names("阿明:\n  instance_of: 人\n小红:\n  instance_of: 人\n")
    ming = bucket("g", "阿明做事急，但答应的都会做到。", room="MIND/TRAITS", card_of="阿明",
                  sources=[rec("m_7", "group")])
    hong = bucket("h", "小红心里有事不说。", room="MIND/TRAITS", card_of="小红",
                  sources=[rec("m_8", "private")])
    seen = bucket("i", "阿明在群里说周六有空。", subjects=["阿明"], sources=[rec("m_9", "group")])
    unseen = bucket("j", "阿明私下说最近很累。", subjects=["阿明"],
                    sources=[rec("m_10", "private")])
    store = Store(tmp_path, [ming, hong, seen, unseen])
    out = cards(store, "阿明来了，小红也来了", scoped=True)
    assert kinds(out) == ["name"] and ids(out) == [ming["id"]]
    assert "相关 1 条" in out["text"], out["text"]
    assert hong["id"][:6] not in out["text"] and "小红" not in out["text"]
    open_ = cards(store, "阿明来了，小红也来了", scoped=False)
    assert ids(open_) == sorted([ming["id"], hong["id"]]) and "相关 2 条" in open_["text"]


def test_a_bare_name_card_needs_something_the_scope_may_read(tmp_path, clock, names):
    names("小周:\n  instance_of: 人\n  member_of: [读书会]\n")
    private = bucket("k", "小周私下说想退出读书会。", subjects=["小周"],
                     sources=[rec("m_11", "private")])
    store = Store(tmp_path, [private])
    assert cards(store, "明天找小周", scoped=True)["cards"] == []
    assert kinds(cards(store, "明天找小周", scoped=False)) == ["name_bare"]
    group = bucket("l", "小周在群里分享了书单。", subjects=["小周"], sources=[rec("m_12", "group")])
    store.rows.append(group)
    out = cards(store, "明天找小周", scoped=True, turn="t2")
    assert kinds(out) == ["name_bare"] and "读书会" in out["text"] and "相关 1 条" in out["text"]


def test_a_name_page_out_of_scope_does_not_keep_a_name_card_back(tmp_path, clock, names):
    names("阿明:\n  instance_of: 人\n")
    page = bucket("m", "阿明是小周的同事，住在城东。", room="MIND/TRAITS", tags=["__档案事实__"],
                  sources=[rec("m_13", "private")])
    ming = bucket("n", "阿明做事急。", room="MIND/TRAITS", card_of="阿明",
                  sources=[rec("m_14", "group")])
    store = Store(tmp_path, [page, ming])
    assert ids(cards(store, "阿明来了", scoped=True)) == [ming["id"]]
    # Without a scope the name page is in front of the model already: no card.
    assert cards(store, "阿明来了", scoped=False)["cards"] == []
