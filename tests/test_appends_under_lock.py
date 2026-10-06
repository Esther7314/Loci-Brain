# -*- coding: utf-8 -*-
"""
tests/test_appends_under_lock.py — two appends to one entry at once both land.

An append (a source, a covered id, a cover, an invalidation record) used to be built from
a copy of the list read outside the bucket's lock and then written whole, so the second
of two concurrent writers wrote back a list without the first one's item. Each append is
now decided on the list as it is on disk, under the lock (BucketManager `revise`).

The race is made deterministic: reads of the shared entry wait until both writers have
read (or a short timeout passes), which is exactly the window the old code lost an item
in. Real store on a temp dir; no model is called.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _fold as F
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.fold import dispatch as fold
from tools.grow import rooms_path
from tools.regrow import dispatch as regrow
from tools.trace import dispatch as trace

REC = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0142",
       "fingerprint": "sha256:aa", "fingerprint_by": "adapter"}


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def _hold_reads(monkeypatch, store, shared: set):
    """Reads of an id in `shared` are let through in pairs: each one returns once the
    other writer has made its matching read (or after a wait), so both writers hold the
    same copy before either writes."""
    real_get = store.get
    seen: dict[str, int] = {}

    async def get(bucket_id):
        out = await real_get(bucket_id)
        if bucket_id in shared:
            seen[bucket_id] = seen.get(bucket_id, 0) + 1
            target = seen[bucket_id] + seen[bucket_id] % 2
            for _ in range(200):
                if seen[bucket_id] >= target:
                    break
                await asyncio.sleep(0.01)
        return out
    monkeypatch.setattr(store, "get", get)


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def test_two_sources_appends_at_once_both_land(store, tmp_path, monkeypatch):
    async def go():
        bid = await store.create("We talked about the trip.", room="EVENT/SELF")
        _hold_reads(monkeypatch, store, {bid})
        outs = await asyncio.gather(
            trace(bucket_id=bid, sources_append=[REC]),
            trace(bucket_id=bid, sources_append=[{**REC, "id": "m_0150"}]))
        return bid, outs
    bid, outs = asyncio.run(go())
    assert all(o.startswith("已修改记忆桶") for o in outs), outs
    meta = _disk(tmp_path, bid)
    assert sorted(r["id"] for r in meta["sources"]) == ["m_0142", "m_0150"]
    quoted = sorted(ln["target"] for ln in meta["prov"] if ln["rel"] == "wasQuotedFrom")
    assert quoted == ["lento:home/private:U#m_0142", "lento:home/private:U#m_0150"]


async def _minds(store, n):
    return [await store.create(f"thought {i}", room="MIND/VIEWS") for i in range(n)]


async def _gist(store, cover):
    gid = await store.create("these thoughts in one line", room="MIND/VIEWS",
                             tags=[F.GIST_TAG])
    await store.update(gid, cover=list(cover))
    for cid in cover:
        await store.update(cid, covered_by=[gid])
    return gid


def test_two_folds_appends_to_one_gist_both_land(store, tmp_path, monkeypatch):
    async def go():
        m1, m2, m3 = await _minds(store, 3)
        gid = await _gist(store, [m1])
        _hold_reads(monkeypatch, store, {gid})
        outs = await asyncio.gather(trace(bucket_id=gid, folds_append=[m2]),
                                    trace(bucket_id=gid, folds_append=[m3]))
        return gid, (m1, m2, m3), outs
    gid, (m1, m2, m3), outs = asyncio.run(go())
    assert all(o.startswith("已修改记忆桶") for o in outs), outs
    assert sorted(_disk(tmp_path, gid)["cover"]) == sorted([m1, m2, m3])


def test_two_gists_covering_one_entry_at_once_both_stand(store, tmp_path, monkeypatch):
    async def go():
        m1, m2, m3, shared = await _minds(store, 4)
        ga = await _gist(store, [m1])
        gb = await _gist(store, [m2])
        _hold_reads(monkeypatch, store, {shared})
        await asyncio.gather(trace(bucket_id=ga, folds_append=[shared]),
                             trace(bucket_id=gb, folds_append=[shared]))
        return shared, ga, gb
    shared, ga, gb = asyncio.run(go())
    assert sorted(_disk(tmp_path, shared)["covered_by"]) == sorted([ga, gb])


def test_two_folds_over_one_entry_at_once_both_cover_it(store, tmp_path, monkeypatch):
    async def go():
        m1, m2, shared = await _minds(store, 3)
        _hold_reads(monkeypatch, store, {shared})
        outs = await asyncio.gather(
            fold(text="one line", room="MIND/VIEWS", v=0.5, a=0.3, cover=[m1, shared]),
            fold(text="another line", room="MIND/VIEWS", v=0.5, a=0.3, cover=[m2, shared]))
        return shared, outs
    shared, outs = asyncio.run(go())
    assert len(_disk(tmp_path, shared)["covered_by"]) == 2, outs


def test_two_overturns_reaching_one_entry_both_mark_it(store, tmp_path, monkeypatch):
    async def go():
        a = await store.create("Saturday we went up the hill.", room="EVENT/SELF")
        b = await store.create("Sunday it rained.", room="EVENT/SELF")
        child = await store.create("Weekends are for going out.", room="MIND/TRAITS",
                                   prov=[{"rel": "wasDerivedFrom", "target": a},
                                         {"rel": "wasDerivedFrom", "target": b}])
        _hold_reads(monkeypatch, store, {child})
        await asyncio.gather(
            regrow(bucket_id=a, text="We stayed in.", v=0.5, a=0.3, mode="overturn"),
            regrow(bucket_id=b, text="It was sunny.", v=0.5, a=0.3, mode="overturn"))
        return a, b, child
    a, b, child = asyncio.run(go())
    records = _disk(tmp_path, child)["invalidation"]
    assert sorted(r["of"] for r in records) == sorted([a, b])
