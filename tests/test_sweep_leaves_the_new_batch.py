# -*- coding: utf-8 -*-
"""
tests/test_sweep_leaves_the_new_batch.py — the startup sweep repairs what an earlier run
left unfinished, not what this run has just written.

The sweep starts on the first grow after a restart, in the background, while that grow is
writing; an entry the grow has just written has no summary yet and looked unfinished, so it
was backfilled twice. The sweep takes only entries written before it started.
"""

import asyncio

import pytest

from core import bucket_manager as BM
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.grow import rooms_path


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    return mgr


def test_the_sweep_takes_only_what_was_written_before_it_started(store, monkeypatch):
    swept: list = []

    async def capture(pairs):
        swept.extend(pairs)
    monkeypatch.setattr(rooms_path, "_backfill_batch", capture)

    async def go():
        monkeypatch.setattr(BM, "now_iso", lambda: "2026-10-01T08:00:00")
        left_over = await store.create("Left unfinished by the last run.", room="EVENT/SELF")
        monkeypatch.setattr(BM, "now_iso", lambda: "2026-10-03T09:00:00")
        just_written = await store.create("Written by this run's first grow.",
                                          room="EVENT/SELF")
        n = await rooms_path.backfill_sweep(before="2026-10-03T09:00:00")
        return left_over, just_written, n
    left_over, just_written, n = asyncio.run(go())
    assert [bid for bid, _t, _k in swept] == [left_over] and n == 1
