# -*- coding: utf-8 -*-
"""
tests/test_regrow_carries_state.py — a new version stands where the old one stood.

save_gist writes every new version as a fresh bucket, so whatever described the old
version's place in the world has to be carried on purpose: its day, whether it is wanted
and by whom, how heavily, whether it is closed, who it is about, the user's tags, the
bookkeeping of asking and dreaming. What describes the text (name, summary, the
backfill's own tags) is computed again from the new body, and the version chain is
written fresh. These tests pin that split field by field, on disk, through regrow itself.
"""

import asyncio

import pytest

from core import _fold as F
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.grow import rooms_path
from tools.regrow import dispatch as regrow

BODY = "Promised to tell them how the interview went tonight."
NEW_BODY = "Promised to tell them how the second round went tonight."


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


async def _newest(store, old_id: str) -> dict:
    meta = (await store.get(old_id))["metadata"]
    return (await store.get(meta["superseded_by"]))["metadata"]


def test_an_event_keeps_its_day(store):
    async def go():
        old = await store.create(BODY, tags=["interview"], room="EVENT/SELF",
                                 when="2026-10-01", subjects=["Es"])
        out = await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        assert "→" in out, out
        new = await _newest(store, old)
        # Criterion: regrow used to pass when="" for anything that was not a period, so
        # a dated event lost its date and fell out of every time-gated read.
        assert new["when"] == "2026-10-01"
        assert new["subjects"] == ["Es"]
        assert "interview" in new["tags"]
    run(go())


def test_a_telic_entry_keeps_its_standing(store):
    async def go():
        old = await store.create(BODY, tags=["interview"], room="EVENT/SELF",
                                 when="2026-10-01", direction_of_fit="telic",
                                 bound=["DT", "Es"], weight=0.8)
        assert await store.update(old, status="active", last_asked="2026-09-30",
                                  last_dreamt="2026-09-29")
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        assert new["direction_of_fit"] == "telic"
        assert new["bound"] == ["DT", "Es"]
        assert new["weight"] == 0.8
        assert new["status"] == "active"
        assert new["last_asked"] == "2026-09-30"
        assert new["last_dreamt"] == "2026-09-29"
        assert new["supersedes"] == old
    run(go())


def test_a_closed_entry_stays_closed_and_remembers_who_closed_it(store):
    async def go():
        old = await store.create(BODY, room="EVENT/SELF", direction_of_fit="telic", weight=0.5)
        assert await store.update(old, status="resolved", closed_by="Es")
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        assert (new["status"], new["closed_by"]) == ("resolved", "Es")
    run(go())


def test_the_older_resolved_boolean_comes_across_as_status(store):
    async def go():
        old = await store.create(BODY, room="EVENT/SELF", direction_of_fit="telic", weight=0.5)
        assert await store.update(old, resolved=True)
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        # Criterion: a want closed under the older flag must not reopen on a rewording.
        assert new["status"] == "resolved"
    run(go())


def test_what_describes_the_text_is_not_carried(store):
    async def go():
        old = await store.create(BODY, tags=["interview", "疑似同件:abc123", "aspect:patterns"],
                                 room="EVENT/SELF", summary="an old summary")
        assert await store.update(old, name="old name", aliases=["x"])
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        assert "interview" in new["tags"]
        assert not [t for t in new["tags"] if t.startswith(("疑似同件:", "aspect:"))]
        assert new.get("summary") in (None, "")
        assert new.get("aliases") in (None, [])
        assert "old name" not in str(new.get("name"))
    run(go())


def test_a_new_version_is_not_a_gist(store):
    async def go():
        old = await store.create(BODY, tags=["interview"], room="EVENT/SELF")
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        # Criterion: the tag threw every regrown memory out of the muse and dream pools
        # and out of a period's members, as if it were machinery.
        assert F.GIST_TAG not in new["tags"]
        assert not F.is_gist(new)
    run(go())


def test_a_fold_of_several_is_still_a_gist_and_so_is_its_new_version(store):
    async def go():
        a = await store.create("first thought", room="MIND/TRAITS")
        b = await store.create("second thought", room="MIND/TRAITS")
        gist, _ = await F.save_gist("both thoughts in one", "MIND/TRAITS", 0.5, 0.5, [a, b])
        assert F.GIST_TAG in (await store.get(gist))["metadata"]["tags"]
        await regrow(bucket_id=gist, text="both thoughts, said better", v=0.5, a=0.5,
                     mode="supplement")
        g2 = await _newest(store, gist)
        assert F.GIST_TAG in g2["tags"], "a reworded gist is still the gist"
        await regrow(bucket_id=g2["id"], text="both thoughts, third wording", v=0.5, a=0.5,
                     mode="supplement")
        g3 = await _newest(store, g2["id"])
        assert F.GIST_TAG in g3["tags"], "the tag follows the chain's first version"
    run(go())


def test_a_chain_that_began_as_an_ordinary_memory_drops_the_tag_it_was_given(store):
    async def go():
        # A version written under the old rule carries the tag although it is a plain
        # re-version; the next regrow reads the chain's first version, not the tag.
        root = await store.create(BODY, room="EVENT/SELF")
        legacy = await store.create(BODY + " (rewritten)", tags=[F.GIST_TAG], room="EVENT/SELF")
        assert await store.update(legacy, supersedes=root, cover=[root])
        assert await store.update(root, superseded_by=legacy, covered_by=[legacy], dont_surface=True)
        await regrow(bucket_id=legacy, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, legacy)
        assert F.GIST_TAG not in new["tags"]
    run(go())


def test_why_it_was_kept_and_what_it_has_meant_come_across(store):
    async def go():
        old = await store.create(BODY, room="EVENT/SELF", why_remembered="the first time it worked",
                                 meaning="felt like a door opening")
        assert await store.update(old, meaning_append="and again a month later")
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        assert new["why_remembered"] == "the first time it worked"
        # Criterion: meaning is the record of every moment this memory was touched; a
        # rewording is not a reason to lose it. (update(meaning=...) regenerates its
        # vector under the new id on its own.)
        assert new["meaning"] == ["felt like a door opening", "and again a month later"]
    run(go())


def test_attachments_are_copied_under_the_new_version(store, tmp_path):
    async def go():
        picture = tmp_path / "incoming.png"
        picture.write_bytes(b"\x89PNG not really but bytes enough")
        old = await store.create(BODY, room="EVENT/SELF")
        assert await store.update(old, media=[{"path": str(picture), "title": "the hill"}])
        [kept] = (await store.get(old))["metadata"]["media"]
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        [copied] = new["media"]
        # Criterion: the attachment belongs to the memory, not to the wording. It is
        # persisted again under the new id, so removing either version cannot take the
        # file from the other.
        assert copied["sha256"] == kept["sha256"] and copied["title"] == "the hill"
        assert copied["path"] != kept["path"] and new["id"] in copied["path"]
        assert (tmp_path / copied["path"]).is_file()
    run(go())


def test_a_protected_entry_stays_protected(store):
    async def go():
        old = await store.create(BODY, room="MIND/VIEWS", protected=True)
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        # update() has no opening for protected, so it has to go in at creation; it
        # locks importance the way the pin does.
        assert new["protected"] is True
        assert new["importance"] == 10
    run(go())


def test_the_anchor_moves_like_the_pin_even_at_the_cap(store):
    async def go():
        store.ANCHOR_LIMIT = 1
        old = await store.create(BODY, room="MIND/VIEWS", source_tool="grow")
        assert (await store.set_anchor(old, True))["ok"]
        out = await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        # Criterion: the slot is scarce and the old version held it. A copy would be
        # refused at the cap; a move frees the slot first, and the receipt says so.
        assert new["anchor"] is True and new["source_tool"] == "anchor"
        old_meta = (await store.get(old))["metadata"]
        assert not old_meta.get("anchor") and old_meta["source_tool"] == "grow"
        assert await store.count_anchors() == 1
        assert "接着当" in out
    run(go())


def test_a_pin_still_moves_with_the_version(store):
    async def go():
        old = await store.create(BODY, room="MIND/VIEWS", pinned=True)
        await regrow(bucket_id=old, text=NEW_BODY, v=0.6, a=0.4, mode="supplement")
        new = await _newest(store, old)
        assert new["pinned"] is True
        assert (await store.get(old))["metadata"].get("pinned") in (False, None)
    run(go())
