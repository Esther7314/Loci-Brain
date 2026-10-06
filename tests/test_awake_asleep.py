# -*- coding: utf-8 -*-
"""
tests/test_awake_asleep.py — awake or asleep, computed on every read.

An entry is awake while one of five conditions holds: promised and open · a date within
the window, or past and still wanted · written in the last few days · touched by a
strong-reminder card in the last week · a live hold itself. Everything else sleeps: still
there, searchable, it just does not come up by itself. Each condition gets an entry that
is awake because of it alone, and the entry just outside it that is not. Then the one that
matters most for how the system feels: searching for a sleeping entry, again and again,
does not wake it — finding is not touching.
"""

import asyncio
from datetime import datetime, timedelta

import pytest

from core import _when as W
from core.bucket_manager import BucketManager
from core.profile import (CUED, DATED, HOLD, PROMISED, RECENT, BreathSettings,
                          awake_reasons, breath_settings, is_accessible)
from core import runtime as rt
from tools.recall import core as R

NOW = datetime(2026, 10, 14, 10, 0, 0, tzinfo=W.LOCAL_TZ)
OLD = NOW - timedelta(days=60)


def day(offset: int) -> str:
    return (NOW + timedelta(days=offset)).strftime("%Y-%m-%d")


def entry(bid="aaaaaaaaaaaa", created=OLD, **meta) -> dict:
    m = {"id": bid, "room": "EVENT/SELF", "created": created.isoformat(timespec="seconds")}
    m.update(meta)
    return m


def reasons(meta, **kw):
    return awake_reasons(meta, NOW, **kw)


# ── 1 promised ──────────────────────────────────────────────────────────────

def test_promised_and_open_is_awake_with_no_date_and_long_ago():
    owed = entry(direction_of_fit="telic", bound=["AI"])
    assert reasons(owed) == (PROMISED,)
    # The edges: nobody bound is a wish, closed is done.
    assert reasons(entry(direction_of_fit="telic")) == ()
    assert reasons({**owed, "status": "resolved"}) == ()


# ── 2 dated ─────────────────────────────────────────────────────────────────

def test_a_date_inside_the_window_or_past_and_still_wanted_is_awake():
    assert reasons(entry(direction_of_fit="telic", when=day(10))) == (DATED,)
    assert reasons(entry(room="EVENT/WORLD", when=day(30))) == (DATED,)    # heard about the future
    assert reasons(entry(direction_of_fit="telic", when=day(-12))) == (DATED,)  # overdue, open
    # The edges: past the window, a past date on something that happened, overdue but closed.
    assert reasons(entry(direction_of_fit="telic", when=day(31))) == ()
    assert reasons(entry(when=day(-12))) == ()
    assert reasons(entry(direction_of_fit="telic", when=day(-12), status="abandoned")) == ()


def test_a_yearly_date_counts_by_its_next_occurrence():
    birthday = (NOW + timedelta(days=5)).replace(year=2019).strftime("%Y-%m-%d")
    assert reasons(entry(room="EVENT/WORLD", when=birthday, recurrence="FREQ=YEARLY")) == (DATED,)
    assert reasons(entry(room="EVENT/WORLD", when=birthday)) == ()


def test_a_length_counts_from_the_day_it_was_written():
    # "within three weeks", written 10 days ago: due in 11 days.
    assert reasons(entry(direction_of_fit="telic", when="3w",
                         created=NOW - timedelta(days=10))) == (DATED,)
    assert reasons(entry(direction_of_fit="telic", when="2m",
                         created=NOW - timedelta(days=10))) == ()


# ── 3 recent ────────────────────────────────────────────────────────────────

def test_written_in_the_last_three_days_is_awake_events_and_minds_alike():
    assert reasons(entry(created=NOW - timedelta(days=2))) == (RECENT,)
    assert reasons(entry(room="MIND/VIEWS", created=NOW - timedelta(days=2))) == (RECENT,)
    assert reasons(entry(created=NOW - timedelta(days=4))) == ()
    # The window is the host's setting.
    wide = BreathSettings(recent_days=7)
    assert reasons(entry(created=NOW - timedelta(days=4)), settings=wide) == (RECENT,)


# ── 4 cued ──────────────────────────────────────────────────────────────────

def test_a_card_delivered_this_week_keeps_it_awake_through_the_ledger_seam():
    e = entry()
    assert reasons(e, delivered_at={e["id"]: NOW - timedelta(days=3)}) == (CUED,)
    assert reasons(e, delivered_at=lambda bid: NOW - timedelta(days=3)) == (CUED,)
    assert reasons(e, delivered_at={e["id"]: NOW - timedelta(days=8)}) == ()
    # No ledger yet (5.5): the condition never holds.
    assert reasons(e) == ()


# ── 5 a live hold ───────────────────────────────────────────────────────────

def test_a_live_hold_itself_is_awake_and_a_spent_one_is_not():
    hold = entry("bbbbbbbbbbbb", direction_of_fit="telic", exception_of="aaaaaaaaaaaa",
                 hold="defer", when=f"{day(-1)}..{day(3)}")
    assert HOLD in reasons(hold)
    spent = {**hold, "when": f"{day(-9)}..{day(-2)}"}
    assert reasons(spent) == ()


# ── what is never awake ─────────────────────────────────────────────────────

def test_an_archived_deleted_or_replaced_entry_is_not_awake():
    owed = entry(direction_of_fit="telic", bound=["AI"], created=NOW - timedelta(days=1))
    assert is_accessible(owed, NOW)
    assert not is_accessible({**owed, "type": "archived"}, NOW)
    assert not is_accessible({**owed, "deleted_at": day(-1)}, NOW)
    assert not is_accessible({**owed, "superseded_by": "cccccccccccc"}, NOW)


def test_dont_surface_is_not_asleep():
    # dont_surface closes the roads that come up by themselves (the gate's), it does not
    # put an entry to sleep.
    assert reasons(entry(direction_of_fit="telic", bound=["AI"], dont_surface=True)) == (PROMISED,)


def test_the_numbers_come_from_the_config_with_the_plan_as_default():
    s = breath_settings({"surfacing": {"awake_date_days": 14, "awake_cue_days": "bad"}})
    assert (s.date_days, s.recent_days, s.cue_days) == (14, 3, 7)
    assert breath_settings(None) == BreathSettings()
    assert reasons(entry(direction_of_fit="telic", when=day(20)), settings=s) == ()


# ── searched ten times, still asleep ────────────────────────────────────────

class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


def test_searching_a_sleeping_entry_ten_times_does_not_wake_it(tmp_path, monkeypatch):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})

    async def go():
        bid = await store.create("Some day, learn to bake sourdough bread.", tags=["t"],
                                 room="EVENT/SELF", direction_of_fit="telic")
        before = dict((await store.get(bid))["metadata"])
        outs = [await R.recall_core(when="", room="", tag="", query="sourdough")
                for _ in range(10)]
        return bid, before, dict((await store.get(bid))["metadata"]), outs
    bid, before, after, outs = asyncio.run(go())
    assert all(bid[:6] in out for out in outs), "the search has to find it each time"
    # Criterion: nothing about it moved, and a month and a half on it sleeps — written
    # long ago, nobody owes it, no date, no card.
    for key in ("last_active", "activation_count", "status", "weight", "decay_stage"):
        assert after.get(key) == before.get(key), key
    later = W.now() + timedelta(days=45)
    assert awake_reasons(after, later) == ()
