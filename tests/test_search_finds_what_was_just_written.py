# -*- coding: utf-8 -*-
"""
tests/test_search_finds_what_was_just_written.py — a write is literally searchable on the
very next search (plan hole 5).

The literal (BM25) index used to be rebuilt in the background after a write, and the search
that triggered the rebuild scored against the old index: what was just written was not
found by the next search. Now a search brings a built index level with the store before it
scores, re-tokenising only what changed — a write costs one entry's tokens, not a rebuild.
The queries below are not whole substrings of the text, so only BM25 can find them.
"""

import asyncio

import pytest

from core import bm25_index as B
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.recall import core as R


class SilentLogger:
    def _noop(self, *a, **k):
        return None
    warning = info = debug = error = _noop


def _manager(path) -> BucketManager:
    # Poll the disk on every list, as a second process's writes are only seen through it.
    return BucketManager({"buckets_dir": str(path),
                          "storage": {"external_change_poll_seconds": 0}})


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = _manager(tmp_path)
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", SilentLogger())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return mgr


_FILLER = ["楼下的面包店换了老板。", "今天去菜市场买了点青菜。", "下午下了一场雷阵雨。",
           "小周在读一本讲航海的书。", "周末把书架重新理了一遍。", "晚饭做了番茄炒蛋。"]


async def _built(mgr: BucketManager) -> None:
    """Some unrelated entries (BM25 gives a word no weight when half the store has it), then
    the first build, which happens in the background; wait for it."""
    for text in _FILLER:
        await mgr.create(text, tags=["t"])
    await mgr.search("任何")
    for _ in range(500):
        if mgr._bm25.built and not mgr._bm25_rebuilding:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("the first BM25 build never landed")


async def _hit(mgr: BucketManager, query: str, bid: str) -> dict | None:
    return next((h for h in await mgr.search(query, limit=50) if h["id"] == bid), None)


def test_what_was_just_written_is_found_by_the_next_search(store):
    async def go():
        await _built(store)
        bid = await store.create("那只蓝色的杯子被小周摔碎了。", tags=["t"])
        hit = await _hit(store, "蓝色 杯子", bid)
        assert hit and hit["bm25_hit"] and not hit["literal_hit"] and hit["score"] > 0

        out = await R.recall_core(when="", room="", tag="", query="蓝色 杯子")
        assert f"({bid[:6]})" in out and "部分字面" in out, out
    asyncio.run(go())


def test_an_edit_to_the_text_is_found_by_the_next_search(store):
    async def go():
        bid = await store.create("那只蓝色的杯子被小周摔碎了。", tags=["t"])
        await _built(store)
        assert await store.update(bid, content="那只绿色的茶壶被小周摔碎了。")
        assert await _hit(store, "绿色 茶壶", bid)
        assert not await _hit(store, "蓝色 杯子", bid)
    asyncio.run(go())


def test_another_process_writing_is_found_the_same_way(store, tmp_path):
    async def go():
        await _built(store)
        other = _manager(tmp_path)
        bid = await other.create("那只蓝色的杯子被小周摔碎了。", tags=["t"])
        assert await _hit(store, "蓝色 杯子", bid)
    asyncio.run(go())


def test_a_write_retokenises_one_entry_not_the_store(store, monkeypatch):
    async def go():
        for i in range(20):
            await store.create(f"第 {i} 条：今天去菜市场买了点青菜。", tags=["t"])
        await _built(store)
        calls: list[str] = []
        real = B._tokenize
        monkeypatch.setattr(B, "_tokenize", lambda text: calls.append(text) or real(text))
        bid = await store.create("那只蓝色的杯子被小周摔碎了。", tags=["t"])
        assert await _hit(store, "蓝色 杯子", bid)
        # One entry and the query; not the twenty that did not change.
        assert len(calls) == 2, calls
    asyncio.run(go())


def test_a_sync_over_an_older_list_never_takes_back_a_newer_one():
    idx = B.BM25Index()
    old = [{"id": f"f{i}", "content": text, "metadata": {}} for i, text in enumerate(_FILLER)]
    new = old + [{"id": "b", "content": "蓝色的杯子摔碎了", "metadata": {}}]
    idx.sync(new, stamp=2)
    assert idx.sync(old, stamp=1) == 0
    assert "b" in idx.score("杯子")
