# -*- coding: utf-8 -*-
"""
tests/test_cover_needs_a_live_coverer.py — a cover only counts while the coverer lives.

`covered_by` / `superseded_by` stay on disk when the gist or the newer version that
wrote them is archived or deleted. The cover check used to read the field alone, so the
covered entry stayed hidden behind something no longer in the store (plan hole 1).
"""

import asyncio

import pytest

from core import _fold as F
from core.bucket_manager import BucketManager
from tools import _runtime as rt

BODY = "She fixed the kettle herself this morning and was proud of it."


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    return mgr


def run(coro):
    return asyncio.run(coro)


async def _meta(store, bid) -> dict:
    return (await store.get_including_archive(bid))["metadata"]


def test_archiving_the_gist_uncovers_and_restoring_covers_again(store):
    async def go():
        entry = await store.create(BODY, tags=["t"])
        gist = await store.create("A gist over the kettle morning.", tags=["t"])
        assert await store.update(entry, covered_by=[gist])
        assert F.is_covered(await _meta(store, entry))

        assert await store.archive(gist)
        meta = await _meta(store, entry)
        assert meta["covered_by"] == [gist], "the field on disk is left alone"
        assert not F.is_covered(meta)
        assert F.covers_of(meta) == []

        restored = await store.restore_archived(gist)
        assert restored.get("ok"), restored
        assert F.is_covered(await _meta(store, entry))
    run(go())


def test_deleting_the_newer_version_uncovers_the_old_one(store):
    async def go():
        old = await store.create(BODY, tags=["t"])
        new = await store.create(BODY + " (rewritten)", tags=["t"])
        assert await store.update(old, superseded_by=new)
        assert F.is_covered(await _meta(store, old))
        assert await store.delete(new)
        assert not F.is_covered(await _meta(store, old))
    run(go())


def test_one_dead_cover_leaves_the_live_one_counting(store):
    async def go():
        entry = await store.create(BODY, tags=["t"])
        g1 = await store.create("First gist.", tags=["t"])
        g2 = await store.create("Second gist.", tags=["t"])
        assert await store.update(entry, covered_by=[g1, g2])
        assert await store.delete(g1)
        meta = await _meta(store, entry)
        assert F.covers_of(meta) == [g2]
        assert F.is_covered(meta)
    run(go())


def test_a_cover_pointing_at_nothing_does_not_count(store):
    async def go():
        entry = await store.create(BODY, tags=["t"])
        assert await store.update(entry, covered_by=["0000deadbeef"])
        assert not F.is_covered(await _meta(store, entry))
    run(go())
