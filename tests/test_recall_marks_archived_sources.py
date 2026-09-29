# -*- coding: utf-8 -*-
"""
tests/test_recall_marks_archived_sources.py — looking up an id marks sources that sank.

The entry's own line said "在归档区" when it was archived, but the "来源:" lines under it
did not: a thought standing on a deleted event read as if the event were current (plan
hole 2).
"""

import asyncio

import pytest

from core.bucket_manager import BucketManager
from tools import _runtime as rt
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


def _source_lines(out: str) -> list[str]:
    lines = out.splitlines()
    start = lines.index("来源:") + 1
    return [ln for ln in lines[start:] if ln.startswith("  ← ")]


def test_a_deleted_source_is_marked_and_a_live_one_is_not(store):
    async def go():
        gone = await store.create("She dropped the blue mug.", tags=["t"])
        kept = await store.create("She bought a new mug.", tags=["t"])
        thought = await store.create("She replaces things quickly.", tags=["t"])
        assert await store.update(thought, **{"from": f"{gone},{kept}"})
        assert await store.delete(gone)

        out = await R.recall_core(when="", room="", tag="", query=thought)
        by_id = {ln.split()[1]: ln for ln in _source_lines(out)}
        assert "在归档区" in by_id[gone]
        assert "在归档区" not in by_id[kept]
    asyncio.run(go())
