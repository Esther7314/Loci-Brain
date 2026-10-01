# -*- coding: utf-8 -*-
"""tests/test_decay_immunity.py — the never-sink list of core/decay_engine.py

WHY THIS FILE EXISTS
    decay_engine had **zero** coverage. It is also the one module in the store whose
    failure mode is silence: nothing throws, nothing is logged where anyone looks, and
    the symptom is that a memory simply stops turning up — months later, in a browse
    that nobody was auditing. "It didn't come up" and "it isn't there" look identical
    from the outside.

    The list itself is the load-bearing part. Everything on it is something a person put
    there **on purpose** — a pin, a rule, a name given to a stretch of days —
    and the whole point of those gestures is that they outlive the forgetting curve. One
    name missing from the list is not a degraded feature, it is the system quietly
    undoing a deliberate act.

    Which is exactly what had happened: a period (`__大event__` — the name the user gives
    to a stretch of days) was **not** on the list, so it aged like an ordinary event and
    sank. This file was written against the unfixed engine first, with the period case
    red, and the fix landed on top of it.

WHAT IS BEING TESTED
    The criterion, not the list. Every assertion here is of the form "this bucket is
    still visible after N days without being recalled" — never "the string X appears in
    _never_decays". A rewrite that reorganises how immunity is decided but keeps the
    behaviour must stay green; a rewrite that keeps a tidy-looking list and loses the
    behaviour must go red.

    Each immune bucket below is built as hostile as the format allows: valence 0,
    arousal 0 (the shortest possible holding period) and `status: resolved` (which halves
    it again). So an immune bucket that lost its immunity does not merely fade sooner —
    it sinks about as fast as anything in this store can, which is what makes the
    control case and the mutation check meaningful.
"""
from datetime import datetime, timedelta

import pytest

from core.decay_engine import DecayEngine


# How long ago the bucket was last recalled. The three points are chosen against the
# baselines in the engine (fade at 45 days × emotion factor, sink at 180 × the same):
# with the hostile v/a and status used here the holding factor is 0.6 × 0.5 = 0.3, so an
# unprotected bucket fades at ~13 days and sinks at ~54.
AGES_IN_DAYS = [
    30,     # past fading, not yet sunk
    120,    # past sinking
    3650,   # ten years — no curve survives this
]


def _stamp(days_ago: float) -> str:
    return (datetime.now() - timedelta(days=days_ago)).isoformat()


def _meta(days_ago: float, **overrides) -> dict:
    """A bucket last recalled `days_ago`, with the least survivable emotion possible.

    v=0 / a=0 is the floor of the emotion factor and `resolved` halves the holding days
    on top of it — so anything that stays alive here stays alive because it is on the
    never-sink list, not because the numbers were kind to it.
    """
    meta = {
        "id": "test0001",
        "room": "EVENT/SELF",
        "valence": 0.0,
        "arousal": 0.0,
        "status": "resolved",
        "last_active": _stamp(days_ago),
        "created": _stamp(days_ago),
    }
    meta.update(overrides)
    return meta


# Each entry: (name, the fields that are supposed to confer immunity).
# 📌 The names are for the failure message only — the assertion never reads them.
IMMUNE_KINDS = [
    ("pinned by hand",            {"pinned": True}),
    ("protected",                 {"protected": True}),
    ("permanent",                 {"type": "permanent"}),
    ("a seed",                    {"type": "seed"}),
    ("an insight (MIND/TRAITS)",  {"room": "MIND/TRAITS"}),
    ("an insight (MIND/VIEWS)",   {"room": "MIND/VIEWS"}),
    ("a profile fact",            {"tags": ["__档案事实__"]}),
    ("a period (__大event__)",     {"tags": ["__大event__"]}),
    ("comes back every year",     {"recurrence": "FREQ=YEARLY"}),
]


# ============================================================
# The list itself: none of these ever stops being visible
# ============================================================

@pytest.mark.parametrize("days", AGES_IN_DAYS)
@pytest.mark.parametrize("name,fields", IMMUNE_KINDS, ids=[k[0] for k in IMMUNE_KINDS])
def test_a_bucket_on_the_never_sink_list_is_still_visible_however_long_it_is_left(
        name, fields, days):
    # Criterion: THE assertion of this file. Every one of these is a deliberate gesture —
    # a pin, a rule, the name given to a stretch of days — and the meaning of
    # the gesture is "this one is not subject to the curve". Visibility is what is
    # asserted, because visibility is what the user loses.
    stage = DecayEngine.stage_of(_meta(days, **fields))
    assert stage == "alive", (
        f"{name} was left {days} days without being recalled and came back {stage!r}. "
        f"It is on the never-sink list precisely because somebody put it there on "
        f"purpose; sinking it undoes that silently, one browse at a time.")


def test_immunity_does_not_depend_on_the_bucket_being_open():
    # Criterion: `resolved` halves the holding days for everyone else, and the fixture
    # above leans on that. Spelled out separately so that a future change making closed
    # buckets sink outright cannot pass by accident on the parametrised cases alone.
    for name, fields in IMMUNE_KINDS:
        meta = _meta(3650, **fields)
        meta["status"] = "resolved"
        assert DecayEngine.stage_of(meta) == "alive", name


# ============================================================
# The control: without one of those marks, decay really does happen
# ============================================================
# Without these, the file above would still be green with `stage_of` hardwired to return
# "alive" — i.e. with forgetting switched off altogether, which is a far worse bug than
# the one being fixed.

def test_an_ordinary_event_is_alive_while_it_is_still_recent():
    assert DecayEngine.stage_of(_meta(0)) == "alive"


def test_an_ordinary_event_fades_once_it_has_been_left_long_enough():
    # Criterion: the middle stage exists and is reachable. Fading is not sinking — the
    # full text is still there, it just stops turning up while browsing.
    assert DecayEngine.stage_of(_meta(30)) == "faded"


@pytest.mark.parametrize("days", [120, 3650])
def test_an_ordinary_event_really_does_sink(days):
    # Criterion: the counterweight to every assertion above. An ordinary memory left
    # alone for months **must** sink, otherwise "immune" means nothing.
    assert DecayEngine.stage_of(_meta(days)) == "sunk"


def test_a_lookalike_tag_confers_nothing():
    # Criterion: immunity is granted by the exact system tag, not by something that
    # merely contains it. A substring test here would hand immunity to any bucket the
    # user happened to tag with a word ending in the same characters.
    assert DecayEngine.stage_of(_meta(3650, tags=["__大event__x"])) == "sunk"
    assert DecayEngine.stage_of(_meta(3650, tags=["前缀__大event__"])) == "sunk"


def test_the_immune_marks_are_read_from_this_bucket_and_not_guessed():
    # Criterion: a bucket carrying no mark at all sinks even though it sits in the same
    # room as one that is immune. Guards against immunity being decided by something
    # ambient (a global flag, a cached previous call) rather than by this metadata.
    DecayEngine.stage_of(_meta(3650, pinned=True))
    assert DecayEngine.stage_of(_meta(3650)) == "sunk"


# ============================================================
# Owed and open: immune until it is closed, then it ages like anything else
# ============================================================

OWED = {"direction_of_fit": "telic", "bound": ["DT"]}


def _open(days: float, **fields) -> dict:
    meta = _meta(days, **fields)
    meta.pop("status", None)          # the fixture closes everything; open is no status
    return meta


@pytest.mark.parametrize("days", AGES_IN_DAYS)
def test_something_owed_and_open_never_sinks(days):
    assert DecayEngine.stage_of(_open(days, **OWED)) == "alive"


def test_once_closed_what_was_owed_ages_again():
    assert DecayEngine.stage_of(_meta(3650, **OWED)) != "alive"


def test_a_wish_nobody_owes_ages_like_anything_else():
    assert DecayEngine.stage_of(_open(3650, direction_of_fit="telic")) != "alive"
