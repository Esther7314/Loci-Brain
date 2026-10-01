# -*- coding: utf-8 -*-
"""
tests/test_regrow_mode.py — regrow says whether it supplements or overturns.

A supplement leaves everything that grew out of the old version exactly as it was. An
overturn appends an `invalidation` record to every descendant, layer by layer, and the
read by id shows it. Without a mode nothing is written. The marks are checked on disk,
through the public update()/get() surface, so the walk may be rewritten freely.
"""

import asyncio

import frontmatter
import pytest

from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.grow import rooms_path
from tools.recall import core as R
from tools.regrow import dispatch as regrow

ROOT = "Saturday we went up the hill."
CHILD = "Tired days are the days they want to go out."
GRANDCHILD = "Ask them out at the weekend without waiting to be asked."


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

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def run(coro):
    return asyncio.run(coro)


def _files(tmp_path):
    return sorted(p for p in tmp_path.rglob("*.md"))


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _derived(*ids) -> list[dict]:
    return [{"rel": "wasDerivedFrom", "target": i} for i in ids]


async def _family(store):
    root = await store.create(ROOT, room="EVENT/SELF")
    child = await store.create(CHILD, room="MIND/TRAITS", prov=_derived(root))
    grandchild = await store.create(GRANDCHILD, room="MIND/VIEWS", prov=_derived(child))
    return root, child, grandchild


async def _new_version_of(store, old):
    return (await store.get(old))["metadata"]["superseded_by"]


# ───────────────────────── the gate ─────────────────────────

def test_without_a_mode_nothing_is_written(store, tmp_path):
    async def go():
        root, _c, _g = await _family(store)
        before = _files(tmp_path)
        out = await regrow(bucket_id=root, text=ROOT + " It rained.", v=0.6, a=0.4)
        # Criterion: the refusal has to come before any write, and tell the caller what
        # to pass. A version written without a mode is a correction nobody classified.
        assert "mode" in out and "supplement" in out and "overturn" in out
        assert _files(tmp_path) == before
        assert "superseded_by" not in _disk(tmp_path, root)
    run(go())


def test_a_mode_outside_the_two_is_refused(store, tmp_path):
    async def go():
        root, _c, _g = await _family(store)
        before = _files(tmp_path)
        out = await regrow(bucket_id=root, text=ROOT + " It rained.", v=0.6, a=0.4, mode="fix")
        assert "mode" in out
        assert _files(tmp_path) == before
    run(go())


# ───────────────────────── supplement ─────────────────────────

def test_a_supplement_leaves_descendants_untouched(store, tmp_path):
    async def go():
        root, child, grandchild = await _family(store)
        child_before, grand_before = _disk(tmp_path, child), _disk(tmp_path, grandchild)
        out = await regrow(bucket_id=root, text=ROOT + " It rained halfway up.",
                           v=0.7, a=0.5, mode="supplement")
        assert "补充" in out
        # Criterion: not one field moves — not last_active, not tags, not a mark.
        assert _disk(tmp_path, child) == child_before
        assert _disk(tmp_path, grandchild) == grand_before
    run(go())


# ───────────────────────── overturn ─────────────────────────

def test_an_overturn_marks_every_descendant_with_the_record(store, tmp_path):
    async def go():
        root, child, grandchild = await _family(store)
        out = await regrow(bucket_id=root, text="Wrong: we stayed in and watched films.",
                           v=0.5, a=0.3, mode="overturn")
        new = await _new_version_of(store, root)
        assert "推翻" in out and child in out and grandchild in out
        for bid in (child, grandchild):
            [rec] = _disk(tmp_path, bid)["invalidation"]
            # Criterion: the record names the overturned basis and what replaced it,
            # on the grandchild as much as on the child (the walk is transitive).
            assert rec["kind"] == "overturn"
            assert rec["of"] == root
            assert rec["by"] == new
            assert rec["at"]
        # The versions themselves carry no mark; the descendants' prov is untouched.
        assert "invalidation" not in _disk(tmp_path, root)
        assert "invalidation" not in _disk(tmp_path, new)
        assert _disk(tmp_path, child)["prov"] == _derived(root)
    run(go())


def test_a_second_overturn_appends_and_never_overwrites(store, tmp_path):
    async def go():
        root, child, _g = await _family(store)
        await regrow(bucket_id=root, text="Wrong once.", v=0.5, a=0.3, mode="overturn")
        v2 = await _new_version_of(store, root)
        await regrow(bucket_id=v2, text="Wrong twice.", v=0.5, a=0.3, mode="overturn")
        v3 = await _new_version_of(store, v2)
        recs = _disk(tmp_path, child)["invalidation"]
        # The child's prov still names the root, so the second walk starts at v2 and
        # reaches the child through nothing: it is only marked for what it stands on.
        assert [r["of"] for r in recs] == [root]
        assert v3 != v2
        # Now a child of v2 exists: its mark says v2, and the root's child keeps its one.
        later = await store.create("Grew out of the second version.", room="MIND/VIEWS",
                                   prov=_derived(v2))
        await regrow(bucket_id=v3, text="Wrong three times.", v=0.5, a=0.3, mode="overturn")
        assert [r["of"] for r in _disk(tmp_path, child)["invalidation"]] == [root]
        assert "invalidation" not in _disk(tmp_path, later)
    run(go())


def test_a_descendant_standing_on_two_overturned_versions_keeps_both_records(store, tmp_path):
    async def go():
        root, child, _g = await _family(store)
        await regrow(bucket_id=root, text="Wrong once.", v=0.5, a=0.3, mode="overturn")
        v2 = await _new_version_of(store, root)
        # The thought is re-based onto the new version, which is then overturned too.
        assert await store.update(child, prov=_derived(root, v2))
        await regrow(bucket_id=v2, text="Wrong twice.", v=0.5, a=0.3, mode="overturn")
        recs = _disk(tmp_path, child)["invalidation"]
        assert [r["of"] for r in recs] == [root, v2]
    run(go())


def test_a_from_loop_ends_and_marks_each_once(store, tmp_path):
    async def go():
        root, child, grandchild = await _family(store)
        # A cycle written by hand: the root cites its own grandchild.
        assert await store.update(root, prov=_derived(grandchild))
        await regrow(bucket_id=root, text="Wrong.", v=0.5, a=0.3, mode="overturn")
        for bid in (child, grandchild):
            assert len(_disk(tmp_path, bid)["invalidation"]) == 1
        # The new version inherits root's prov and so cites the grandchild; it is not
        # walked into and not marked.
        new = await _new_version_of(store, root)
        assert "invalidation" not in _disk(tmp_path, new)
    run(go())


def test_the_read_by_id_shows_which_basis_moved(store):
    async def go():
        root, child, _g = await _family(store)
        await regrow(bucket_id=root, text="Wrong.", v=0.5, a=0.3, mode="overturn")
        new = await _new_version_of(store, root)
        out = await R.recall_core(when="", room="", tag="", query=child)
        line = [ln for ln in out.splitlines() if "依据变了" in ln]
        assert len(line) == 1, out
        assert root in line[0] and new in line[0]
        # And the new version itself reads clean.
        assert "依据变了" not in await R.recall_core(when="", room="", tag="", query=new)
    run(go())
