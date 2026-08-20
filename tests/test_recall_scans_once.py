# -*- coding: utf-8 -*-
"""One browse reads the library once. Not twice, not once per section.

WHY THIS FILE EXISTS
    A function that fetches the whole library for itself is invisible to whoever called
    it. Each such call looks perfectly reasonable on its own, and several of them in one
    request quietly add up to scanning everything N times — with nothing in the code
    showing the total anywhere.

    Browsing did exactly that. `_collect()` fetched the library to filter it down, and
    then `_render_browse()` fetched it again for the periods (which genuinely need the
    WHOLE library, not the filtered result). Two full scans per browse, and neither half
    could see the other one.

    Both now take the list from their caller, so `recall_core` fetches once, on the one
    path that needs it, and a second fetch would be a second visible line.

WHAT MAKES THIS TEST THE POINT OF THE REFACTOR
    Passing the list in is what makes the function testable — but it is also what makes
    the repetition countable, and this file is the count. Without an assertion on the
    number, "pass it in" is a tidier signature and nothing more: someone adds one more
    section that needs the library, fetches it there, and the old shape is back with
    nothing going red.

    So the assertion is on the NUMBER OF READS, not on the output.
"""
import asyncio
from datetime import datetime, timedelta

import pytest

from core import _when as W
from tools import _runtime as rt
from tools.recall import core as R


class CountingStore:
    """A bucket store that keeps count of how many times the whole library was read."""

    def __init__(self, buckets):
        self.buckets = buckets
        self.list_all_calls = 0

    async def list_all(self, include_archive=False):
        self.list_all_calls += 1
        return list(self.buckets)

    async def get(self, bucket_id):
        for b in self.buckets:
            if b["id"] == bucket_id:
                return b
        return None

    async def touch_many(self, ids):
        return None

    embedding_engine = None


class SilentLogger:
    def _noop(self, *a, **k):
        return None
    warning = info = debug = error = _noop


def a_bucket(bid, days_ago=1, room="EVENT/SELF", text="something happened"):
    when = (W.now() - timedelta(days=days_ago)).strftime("%Y-%m-%d")
    return {
        "id": bid,
        "content": text,
        "metadata": {"id": bid, "room": room, "when": when, "created": when,
                     "tags": [], "valence": 0.6, "arousal": 0.4, "summary": text[:20]},
    }


@pytest.fixture
def store(monkeypatch):
    s = CountingStore([a_bucket(f"b{i:02d}", days_ago=i) for i in range(1, 9)])
    monkeypatch.setattr(rt, "bucket_mgr", s)
    monkeypatch.setattr(rt, "logger", SilentLogger())
    monkeypatch.setattr(rt, "config", {"buckets_dir": "."})
    return s


def test_browsing_reads_the_library_exactly_once(store):
    # Criterion: THE assertion. Two is the number this refactor removed, and two is what
    # comes back the moment a new section fetches for itself instead of taking what it
    # was handed.
    asyncio.run(R.recall_core(when="7d", room="", tag="", query=""))
    assert store.list_all_calls == 1, (
        f"a single browse read the whole library {store.list_all_calls} times; "
        f"each reader is supposed to take the list it was handed")


def test_a_query_does_not_read_the_whole_library_at_all(store):
    # Criterion: the other half of the same idea. A search has its own hits, so fetching
    # everything would be pure waste — and the waste would be invisible, which is how it
    # survived last time.
    asyncio.run(R.recall_core(when="", room="", tag="", query="something"))
    assert store.list_all_calls == 0, (
        f"a query read the whole library {store.list_all_calls} times; it should not "
        f"need to at all")


def test_the_helpers_do_not_reach_for_the_store_when_handed_a_list(store):
    # Criterion: the property that makes them unit-testable. Given the list, neither one
    # touches the store — which is what lets a test call them with nothing but data.
    entries, err, ledger = asyncio.run(
        R._collect("7d", "", "", "", all_buckets=list(store.buckets)))
    assert not err
    assert store.list_all_calls == 0

    asyncio.run(R._render_browse(entries, "when=7d", "", "", all_buckets=list(store.buckets)))
    assert store.list_all_calls == 0


def test_the_helpers_still_work_when_nobody_hands_them_a_list(store):
    # Criterion: the fallback exists on purpose — `_collect` has three callers, and the
    # two that are not `recall_core` were not touched by this change. Removing the
    # fallback would have broken them silently at exactly the moment they browse.
    entries, err, _ = asyncio.run(R._collect("7d", "", "", ""))
    assert not err and entries
    assert store.list_all_calls == 1


def test_browsing_still_returns_something_readable(store):
    # Criterion: the count would also be 1 if browsing had stopped working entirely. An
    # efficiency assertion needs a companion that says the thing still does its job.
    out = asyncio.run(R.recall_core(when="7d", room="", tag="", query=""))
    assert isinstance(out, str) and out.strip()
    assert store.list_all_calls == 1
