# -*- coding: utf-8 -*-
"""Every judgement the awakening screen makes lives in `door_note()`. None of them
were checked by anything.

WHY THIS FILE EXISTS
    `core/profile.py` is where the door decides what a rule is, what deserves a
    reminder, and what counts as still weighing on you. It runs on **every single
    breath()**, it is a pure function over a list of buckets and a timestamp — the
    easiest thing in this repo to test — and it had **zero** coverage.

    That is worse than it sounds, because the comments in that file record four bugs
    which already happened here, each of them invisible while it was happening:

      · every entry stored today shouted "that is today!" and pushed the real reminders
        out of all three slots
      · `float(meta.get("weight") or 0.5)` — a want whose weight had been **deliberately
        zeroed** was ranked 0.5 and went on pressing, which silently undid the one
        outcome dreaming exists to produce
      · three pinned notes showed **not one character** at the door because a secondary
        filter demanded they live in a MIND room. No error. Just absent.
      · a rule that had been re-versioned or folded away kept being displayed as current

    All four are the same kind of failure: **nothing goes wrong, something merely fails
    to appear** (or appears when it should not). There is no exception to notice and no
    output to diff against — which is why they need assertions rather than care.

WHAT THIS DOES NOT CHECK
    The rendering in `tools/breath/awaken.py` — the text skin around these results.
    door_note deliberately makes no rendering decisions, so this file draws the line in
    the same place the code does.
"""
from datetime import datetime, timedelta

import pytest

from core.profile import door_note, _PROFILE_TAG, _BIGEVENT_TAG, _REMIND_DAYS

NOW = datetime(2026, 8, 22, 21, 0, 0)


def bucket(bid, content="…", *, tags=None, room="EVENT/SELF", created=None, **meta):
    """A bucket in the shape the store hands out. Only what a test names is set."""
    m = {"id": bid, "room": room, "tags": list(tags or []),
         "created": (created or (NOW - timedelta(days=1))).strftime("%Y-%m-%dT%H:%M:%S")}
    m.update(meta)
    return {"content": content, "metadata": m}


def day(offset):
    return (NOW + timedelta(days=offset)).strftime("%Y-%m-%d")


def ids(rows):
    return [r["id"] for r in rows]


# ── reminders ────────────────────────────────────────────────────────────────

def test_a_date_inside_the_window_is_a_reminder_and_the_countdown_is_right():
    out = door_note([bucket("aaa", when=day(3), direction_of_fit="telic")], NOW)
    assert ids(out["reminders"]) == ["aaa"]
    assert out["reminders"][0]["days"] == 3


def test_todays_date_reminds_only_when_it_is_something_you_want():
    """The one that emptied all three reminder slots.

    A want ringing on its day is the point. An event carrying today's `when` is a
    record of something that already happened — and since almost everything stored
    today carries when=今天, letting those in buries the real reminders.
    """
    out = door_note([
        bucket("want_today", when=day(0), direction_of_fit="telic"),
        bucket("event_today", when=day(0)),
    ], NOW)
    assert ids(out["reminders"]) == ["want_today"]


def test_a_date_that_has_passed_does_not_remind():
    out = door_note([bucket("aaa", when=day(-1), direction_of_fit="telic")], NOW)
    assert out["reminders"] == []


def test_beyond_the_window_does_not_remind():
    inside = door_note([bucket("aaa", when=day(_REMIND_DAYS), direction_of_fit="telic")], NOW)
    outside = door_note([bucket("aaa", when=day(_REMIND_DAYS + 1), direction_of_fit="telic")], NOW)
    assert ids(inside["reminders"]) == ["aaa"]
    assert outside["reminders"] == []


@pytest.mark.parametrize("silenced", [
    {"status": "resolved"},
    {"status": "abandoned"},
    {"dont_surface": 1},
    {"superseded_by": "bbbbbbbbbbbb"},
])
def test_things_that_have_been_put_down_stop_reminding(silenced):
    out = door_note([bucket("aaa", when=day(3), **silenced)], NOW)
    assert out["reminders"] == [], f"{silenced} should not ring"


# ── what is weighing on me ───────────────────────────────────────────────────

def test_a_want_with_no_date_weighs_on_you():
    out = door_note([bucket("aaa", direction_of_fit="telic")], NOW)
    assert ids(out["heavy"]) == ["aaa"]


def test_a_want_whose_day_went_by_unclosed_weighs_on_you():
    out = door_note([bucket("aaa", when=day(-5), direction_of_fit="telic")], NOW)
    assert ids(out["heavy"]) == ["aaa"]


def test_a_want_already_ringing_is_not_also_weighing():
    """Otherwise one entry takes up two slots on the same screen."""
    out = door_note([bucket("aaa", when=day(3), direction_of_fit="telic")], NOW)
    assert ids(out["reminders"]) == ["aaa"]
    assert out["heavy"] == []


def test_a_weight_of_zero_is_zero_and_not_the_default():
    """`meta.get("weight") or 0.5` reads 0.0 as missing.

    Clearing the weight is how dreaming says "this has stopped pressing on me". Under
    the falsy fallback that outcome was eaten whole: field cleared, still ranked 0.5,
    still pressing in plain view.
    """
    out = door_note([
        bucket("zeroed", direction_of_fit="telic", weight=0),
        bucket("unset", direction_of_fit="telic"),
        bucket("empty_string", direction_of_fit="telic", weight=""),
    ], NOW)
    by_id = {h["id"]: h["weight"] for h in out["heavy"]}
    assert by_id["zeroed"] == 0.0, "a deliberately zeroed weight must stay zero"
    assert by_id["unset"] == 0.5
    assert by_id["empty_string"] == 0.5


def test_the_list_ranks_by_weight_but_the_question_asks_the_oldest():
    """Two different questions, computed separately on purpose.

    "Which matters most" orders the list; "which has gone unanswered longest" is what
    earns the single spoken question. Deriving one from the other loses the other.
    """
    out = door_note([
        bucket("heavy_and_new", direction_of_fit="telic", weight=0.9,
               created=NOW - timedelta(days=2)),
        bucket("light_and_old", direction_of_fit="telic", weight=0.1,
               created=NOW - timedelta(days=60)),
    ], NOW)
    assert ids(out["heavy"]) == ["heavy_and_new", "light_and_old"]
    assert out["heavy_question_id"] == "light_and_old"


def test_held_counts_the_days_it_has_hung():
    """⚠️ Asserted as a **difference**, not an absolute, and that is not fussiness.

    `held` is `now.date() - parse_stamp(created).date()`, and the two sides do not
    come from the same clock: `created` is stored without a suffix and read as UTC,
    while `now` is local. So an entry written at 21:00 local reads as one day younger
    from UTC+8 than the calendar says. **The gap between two entries is unaffected**
    (both go through the same conversion), which is what this pins down. Asserting the
    absolute number would pass here and fail on any machine in another timezone.
    """
    out = door_note([
        bucket("old", direction_of_fit="telic", created=NOW - timedelta(days=60)),
        bucket("recent", direction_of_fit="telic", created=NOW - timedelta(days=10)),
    ], NOW)
    held = {h["id"]: h["held"] for h in out["heavy"]}
    assert held["old"] - held["recent"] == 50


# ── rules on the door ────────────────────────────────────────────────────────

def test_pinned_is_the_whole_test_and_the_room_has_no_say():
    """Three pinned notes once showed nothing at the door for landing in EVENT/SELF."""
    out = door_note([
        bucket("in_mind", room="MIND/TRAITS", pinned=1),
        bucket("in_event", room="EVENT/SELF", pinned=1),
        bucket("no_room_at_all", room="", pinned=1),
    ], NOW)
    assert set(ids(out["rules"])) == {"in_mind", "in_event", "no_room_at_all"}


@pytest.mark.parametrize("covered", [
    {"covered_by": "cccccccccccc"},
    {"superseded_by": "dddddddddddd"},
])
def test_a_rule_you_have_since_changed_your_mind_about_is_not_a_rule(covered):
    out = door_note([bucket("aaa", pinned=1, **covered)], NOW)
    assert out["rules"] == [], "a folded/re-versioned pin must not be shown as current"


def test_not_pinned_is_not_a_rule():
    # The floor under the three tests above: without this, a door_note that returned
    # every entry as a rule would pass all of them.
    out = door_note([bucket("aaa", room="MIND/TRAITS")], NOW)
    assert out["rules"] == []


# ── the profile page and periods ─────────────────────────────────────────────

def test_profile_facts_come_back_earliest_first_so_a_duplicate_is_visible():
    out = door_note([
        bucket("later", tags=[_PROFILE_TAG], created=NOW - timedelta(days=1)),
        bucket("earlier", tags=[_PROFILE_TAG], created=NOW - timedelta(days=30)),
    ], NOW)
    assert ids(out["facts"]) == ["earlier", "later"]


@pytest.mark.parametrize("covered", [
    {"superseded_by": "newpage00000"},
    {"covered_by": ["gist00000000"]},
])
def test_a_profile_page_that_was_replaced_is_not_the_door(covered):
    """The page is found by its tag, and a replaced page keeps its tag on disk.

    Being older, it sorts ahead of its successor — so without this gate the door shows
    the old text indefinitely, with no warning, because there is still exactly one
    page it counts as current.
    """
    out = door_note([
        bucket("old_page", "old text", tags=[_PROFILE_TAG],
               created=NOW - timedelta(days=30), **covered),
        bucket("new_page", "new text", tags=[_PROFILE_TAG],
               created=NOW - timedelta(days=1)),
    ], NOW)
    assert ids(out["facts"]) == ["new_page"]
    assert ids(out["facts_covered"]) == ["old_page"]


def test_a_replaced_page_with_nothing_taking_over_is_reported_by_name():
    """The successor was written without the tag. The cell is empty either way; what
    this pins down is that the empty cell can say why, instead of reading like a store
    that never had a page."""
    out = door_note([
        bucket("old_page", tags=[_PROFILE_TAG], superseded_by="untagged0000"),
        bucket("untagged0000", "new text"),
    ], NOW)
    assert out["facts"] == []
    assert out["facts_covered"] == [{"id": "old_page", "by": ["untagged0000"]}]


def test_a_finished_period_is_not_listed():
    out = door_note([
        bucket("running", tags=[_BIGEVENT_TAG], when="2026-07-31.."),
        bucket("done", tags=[_BIGEVENT_TAG], when="2026-06-01..2026-06-30",
               status="resolved"),
    ], NOW)
    assert ids(out["big"]) == ["running"]


def test_profile_and_period_buckets_stay_out_of_the_entry_pool():
    """`entries` feeds the random pick. A name page turning up as "out of the blue"
    would be the door quoting its own header back at me."""
    out = door_note([
        bucket("profile", tags=[_PROFILE_TAG]),
        bucket("period", tags=[_BIGEVENT_TAG]),
        bucket("ordinary"),
    ], NOW)
    assert ids(out["entries"]) == ["ordinary"]
