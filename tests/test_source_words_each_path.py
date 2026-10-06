# -*- coding: utf-8 -*-
"""
tests/test_source_words_each_path.py — the places a withdrawal clears by matching, one
planted artefact per way of matching.

The dream records, the dehydration cache and the usage log are cleared of what was written
from the withdrawn material by more than one road: by the entry's id, by the source itself,
by the dehydrator's key for the body, and by the words of the body, the name and the summary
(core/_source_change._words). Each artefact below matches by exactly one road, so a road
that stops working leaves its artefact behind; and one artefact per place carries only a
tag the entry shares with the rest of the library, which must stay (tags are labels, not
the material's words).
"""

import asyncio
import json
import sqlite3

import pytest

from core import _dream
from core import _source_change as SC
from core.bucket_manager import BucketManager
from core.scope import Host

M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003"}
M_STR = "lento:home/private:U#m_0003"
HOST = Host("life", scope_mode="open", may_restore=True)

BODY = "小周说暗号是青柠汽水，周六要去海边。"
BODY_WORDS = "暗号是青柠汽水"
NAME_WORDS = "海鸥灯塔守夜人"
SUMMARY_WORDS = "蓝莓松饼配方单"
TAG = "夏日露营清单"


def run(coro):
    return asyncio.run(coro)


class _Dehydrator:
    """What the cache place asks of the dehydrator: the key it stores a body's summary by."""

    @staticmethod
    def _content_key(text: str) -> str:
        return "key-of-" + str(len(text))


@pytest.fixture
def planted(tmp_path, monkeypatch):
    from core import runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    e = run(store.create(BODY, name=f"{NAME_WORDS}", summary=f"{SUMMARY_WORDS}", tags=[TAG],
                         sources=[M], room="EVENT/WORLD"))
    other = run(store.create("周日在家看书。", room="EVENT/SELF", tags=[TAG]))

    def dream(did: str, text: str, used=(), fed=()):
        _dream.save_record({"id": did, "完整": text, "碎片": "", "来源": list(fed),
                            "素材": {"压在心头": list(used), "想不明白": []}}, str(tmp_path))
    dream("d00000000001", "梦见下雨。", used=[e])                       # by id
    dream("d00000000002", "梦见下雪。", fed=[M_STR])                    # by the source
    dream("d00000000003", f"梦里有人说{BODY_WORDS}。")                 # by the body's words
    dream("d00000000004", f"梦见{NAME_WORDS}在唱歌。")                 # by the name's words
    dream("d00000000005", f"梦见一张{SUMMARY_WORDS}。")                # by the summary's words
    dream("d00000000006", f"梦见去{TAG}捡贝壳。")                      # a shared tag only
    with sqlite3.connect(tmp_path / "dehydration_cache.db") as c:
        c.execute("CREATE TABLE dehydration_cache (content_hash TEXT PRIMARY KEY, "
                  "summary TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT)")
        rows = [(_Dehydrator._content_key(BODY), "别的话"),               # by the key
                ("k-body", f"关于{BODY_WORDS}"),                          # by the body's words
                ("k-name", f"关于{NAME_WORDS}"),                          # by the name's words
                ("k-tag", f"关于{TAG}"),                                  # a shared tag only
                ("k-other", "毫不相干")]
        c.executemany("INSERT INTO dehydration_cache VALUES (?, ?, 'm', '')", rows)
    store.usage.record("found", [e], "recall.search", query="天气", gates={"when": ""})
    store.usage.record("found", [other], "recall.search", query=NAME_WORDS, gates={})
    store.usage.record("found", [other], "recall.search", query=SUMMARY_WORDS, gates={})
    store.usage.record("found", [other], "recall.search", query=TAG, gates={})
    return store, e, other, tmp_path


def _withdraw(store, **kw):
    status, out = run(SC.handle(store, {"change_id": "c-1", "source": M_STR, "host_seq": 1,
                                        "change": "withdrawn"}, HOST, **kw))
    assert status == 200 and out["status"] == "applied", out
    return out


def test_each_road_clears_its_own_artefact_and_a_shared_tag_clears_nothing(planted):
    store, _e, _other, root = planted
    _withdraw(store, dehydrator=_Dehydrator())
    assert [r["id"] for r in _dream.load_dreams(str(root))] == ["d00000000006"]
    with sqlite3.connect(root / "dehydration_cache.db") as c:
        left = sorted(r[0] for r in c.execute("SELECT content_hash FROM dehydration_cache"))
    assert left == ["k-other", "k-tag"]
    queries = [r.get("query") for r in store.usage.read() if r.get("road") == "recall.search"]
    assert queries == [None, None, None, TAG]


def test_without_the_dehydrator_its_key_is_not_known(planted):
    store, _e, _other, root = planted
    _withdraw(store)
    with sqlite3.connect(root / "dehydration_cache.db") as c:
        left = sorted(r[0] for r in c.execute("SELECT content_hash FROM dehydration_cache"))
    assert left == sorted([_Dehydrator._content_key(BODY), "k-other", "k-tag"])


def test_the_words_are_the_body_name_and_summary_and_not_the_tags(planted):
    store, e, _other, _root = planted
    _bodies, words = run(SC._words(store, [e]))
    for text in (BODY_WORDS, NAME_WORDS, SUMMARY_WORDS):
        assert words.hit(f"……{text}……"), text
    assert not words.hit(f"去{TAG}")
    assert json.dumps(_bodies, ensure_ascii=False).count(BODY_WORDS) == 1
