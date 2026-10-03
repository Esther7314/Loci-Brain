# -*- coding: utf-8 -*-
"""
tests/test_versions_keep_what_was_said.py — what a write was told is kept, and what was only
read off old words is read again.

· grow(kind="mind") keeps the `when` of a wanted thought, and refuses one on a thought that
  is not wanted instead of dropping it.
· regrow carries a deliberate `dont_surface` onto the new version.
· trace on an old version is refused and names the live one: an edit there would leave the
  live version as it was.
· regrow does not carry what the backfill read off the old wording (a promise mark, "dreamt",
  a yearly date): the new words are read again. What the main model said is carried.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.regrow import dispatch as regrow
from tools.trace.core import trace_core


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


def run(coro):
    return asyncio.run(coro)


def _files(tmp_path):
    return sorted(tmp_path.rglob("*.md"))


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def test_a_wanted_thought_keeps_its_when(store, tmp_path):
    src = run(store.create("We talked about the trip.", room="EVENT/SELF"))
    out = run(grow(kind="mind", room="MIND/VIEWS", text="I want to plan the trip by then.",
                   from_=[src], v=0.6, a=0.4, direction_of_fit="telic", when="2031-05-01"))
    bid = out.split("🧠mind→", 1)[1].split()[0]
    assert str(_disk(tmp_path, bid)["when"]) == "2031-05-01"


def test_a_when_on_a_thought_that_is_not_wanted_is_refused(store, tmp_path):
    src = run(store.create("We talked about the trip.", room="EVENT/SELF"))
    before = _files(tmp_path)
    out = run(grow(kind="mind", room="MIND/VIEWS", text="Trips make us closer.",
                   from_=[src], v=0.6, a=0.4, when="2031-05-01"))
    assert "when" in out and "🧠mind→" not in out
    assert _files(tmp_path) == before


def test_a_deliberate_dont_surface_comes_across(store, tmp_path):
    old = run(store.create("Quiet mornings sort the week out.", room="MIND/VIEWS"))
    assert run(store.update(old, dont_surface=True))
    run(regrow(bucket_id=old, text="Quiet mornings sort the week out, mostly.",
               v=0.5, a=0.3, mode="supplement"))
    new = _disk(tmp_path, old)["superseded_by"]
    assert _disk(tmp_path, new).get("dont_surface") is True


def test_a_new_version_of_a_surfacing_entry_surfaces(store, tmp_path):
    old = run(store.create("Quiet mornings sort the week out.", room="MIND/VIEWS"))
    run(regrow(bucket_id=old, text="Quiet mornings sort the week out, mostly.",
               v=0.5, a=0.3, mode="supplement"))
    new = _disk(tmp_path, old)["superseded_by"]
    assert not _disk(tmp_path, new).get("dont_surface")


def test_trace_on_an_old_version_is_refused_and_names_the_live_one(store, tmp_path):
    old = run(store.create("Fix the bike.", room="EVENT/SELF", direction_of_fit="telic",
                           bound=["AI"]))
    run(regrow(bucket_id=old, text="Fix the bike before Sunday.", v=0.5, a=0.3,
               mode="supplement"))
    new = _disk(tmp_path, old)["superseded_by"]
    run(regrow(bucket_id=new, text="Fix the bike before Saturday.", v=0.5, a=0.3,
               mode="supplement"))
    live = _disk(tmp_path, new)["superseded_by"]
    out = run(trace_core(old, status="resolved"))
    assert live in out and not out.startswith("已修改记忆桶")
    assert not _disk(tmp_path, old).get("status")
    assert _disk(tmp_path, live).get("status") in (None, "", "active")


def test_what_the_backfill_read_off_the_old_words_is_read_again(store, tmp_path):
    old = run(store.create("I'll bring the umbrella back, every year on the 7th.",
                           room="EVENT/SELF", looks_like_promise=True,
                           internally_generated=True, recurrence="FREQ=YEARLY",
                           when="2026-08-07",
                           backfilled=["looks_like_promise", "internally_generated",
                                       "recurrence", "summary"]))
    run(regrow(bucket_id=old, text="Brought the umbrella back.", v=0.5, a=0.3,
               mode="supplement"))
    new = _disk(tmp_path, _disk(tmp_path, old)["superseded_by"])
    for read_off in ("looks_like_promise", "internally_generated", "recurrence"):
        assert read_off not in new, read_off
        assert read_off not in (new.get("backfilled") or [])
    assert str(new["when"]) == "2026-08-07", "when is where it hangs, not its wording"


def test_what_the_main_model_said_comes_across(store, tmp_path):
    old = run(store.create("Dreamt we were at the sea.", room="EVENT/SELF",
                           internally_generated=True))
    run(regrow(bucket_id=old, text="Dreamt we were at the sea, at night.", v=0.5, a=0.3,
               mode="supplement"))
    new = _disk(tmp_path, _disk(tmp_path, old)["superseded_by"])
    assert new.get("internally_generated") is True
