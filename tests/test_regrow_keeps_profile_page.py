# -*- coding: utf-8 -*-
"""A new version of the profile page is still the profile page.

WHY THIS FILE EXISTS
    The door finds its page by one tag and nothing else. `save_gist` writes every new
    version as a fresh bucket, so whatever the old bucket was *for* has to be carried
    across on purpose. The pin is; these tests hold the tag to the same standard.
    Without it, regrowing the page reports success and the door keeps showing the old
    text — or, with the door skipping superseded pages, shows nothing.

WHAT THIS DOES NOT CHECK
    Backfill, which runs in the background and is stubbed out here. The tag has to survive
    it too, and that half lives in test_contract_backfill_failure_keeps_body.py
    (a similarity hint written on top of the existing tags). How the door reads a covered
    page is test_door_note.py's business; the last test here only joins the two ends once,
    because the failure lived in the gap between them.
"""
import asyncio
from datetime import datetime

import pytest

from core import _fold as F
from core.profile import door_note, _PROFILE_TAG
from tools import _runtime as rt
import tools.grow.rooms_path as rooms_path

PAGE = "page00000000"


class Store:
    """Just enough of the bucket manager for save_gist: get, create, update, touch."""

    def __init__(self, buckets):
        self.meta = {bid: dict(m, id=bid) for bid, (_content, m) in buckets.items()}
        self.content = {bid: content for bid, (content, _m) in buckets.items()}

    async def get(self, bid):
        if bid not in self.meta:
            return None
        return {"id": bid, "content": self.content[bid], "metadata": self.meta[bid]}

    async def create(self, content, tags, **kw):
        bid = f"new{len(self.meta):09d}"
        self.meta[bid] = {"id": bid, "tags": list(tags), **kw}
        self.content[bid] = content
        return bid

    async def update(self, bid, **kw):
        if bid not in self.meta:
            return False
        self.meta[bid].update(kw)
        return True

    async def touch_many(self, ids):
        return None

    def as_buckets(self):
        return [{"id": b, "content": self.content[b], "metadata": m}
                for b, m in self.meta.items()]


class Logger:
    def _record(self, msg, *a):
        pass

    warning = info = debug = error = _record


@pytest.fixture
def world(monkeypatch):
    async def no_backfill(items):
        return None

    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)

    def build(buckets):
        store = Store(buckets)
        monkeypatch.setattr(rt, "bucket_mgr", store)
        monkeypatch.setattr(rt, "logger", Logger())
        return store
    return build


def regrow(bucket_id, text="new text"):
    """save_gist the way regrow calls it: one id, covered and superseded."""
    return asyncio.run(F.save_gist(text, "EVENT/SELF", 0.6, 0.3, [bucket_id],
                                   supersedes=bucket_id))


def test_the_new_version_of_the_page_carries_the_tag(world):
    store = world({PAGE: ("old text", {"tags": [_PROFILE_TAG, "names"], "pinned": True})})
    new_id, report = regrow(PAGE)
    assert _PROFILE_TAG in store.meta[new_id]["tags"]
    assert store.meta[PAGE]["superseded_by"] == new_id
    assert report.get("接着当门口") is True, "the regrow has to say the page moved"


def test_an_ordinary_entry_does_not_become_the_page_by_being_regrown(world):
    # The floor under the test above: a save_gist that tagged every new version would
    # pass it, and would put every regrown memory on the door.
    store = world({"note00000000": ("a note", {"tags": ["names"]})})
    new_id, report = regrow("note00000000")
    assert _PROFILE_TAG not in store.meta[new_id]["tags"]
    assert not report.get("接着当门口")


def test_folding_the_page_together_with_others_does_not_hand_the_job_on(world):
    # A fold of several has no "previous version", so nothing inherits the page's job.
    # The door then reports the page as covered instead of silently showing nothing.
    store = world({PAGE: ("old text", {"tags": [_PROFILE_TAG]}),
                   "other0000000": ("another", {})})
    new_id, _ = asyncio.run(F.save_gist("summary", "MIND/TRAITS", 0.5, 0.5,
                                        [PAGE, "other0000000"]))
    assert _PROFILE_TAG not in store.meta[new_id]["tags"]


def test_after_regrowing_the_page_the_door_shows_the_new_text(world):
    store = world({PAGE: ("old text", {"tags": [_PROFILE_TAG], "pinned": True,
                                       "created": "2026-08-03T04:16:54"})})
    regrow(PAGE, text="new text")
    door = door_note(store.as_buckets(), datetime(2026, 9, 13, 14, 0))
    assert [f["content"] for f in door["facts"]] == ["new text"]
    assert [c["id"] for c in door["facts_covered"]] == [PAGE]
