# -*- coding: utf-8 -*-
"""The pinned quota — frozen before the code around it is refactored.

WHY THESE EXIST, AND WHY THEY EXIST *FIRST*
    The pinned quota is what stops the door screen from filling up. The screen has a hard
    cap on how many principles it can show, and pinning past that does not raise anything
    — it just means something stops being visible with no sign that it did.

    So this is safety-critical code with, until now, no test at all. The next commit
    changes how it obtains the library, and the rule this project has already paid for
    twice is: **put the assertions in before the knife, not after.** An assertion written
    after a refactor can only confirm what the new code does; one written before can
    disagree with it.

WHAT IS FROZEN
    · counting: what counts toward the quota and what does not
    · the hard ceiling: at the cap, the answer is "no", and it says so in words
    · the soft threshold: a warning, and NOT a refusal
    · failure: if the library cannot be read, the quota does not block anything

WHAT IS NOT FROZEN
    The numbers themselves (cap, soft gap) — those are configuration and are meant to move.
"""
import asyncio

import pytest

from tools import _common as C
from core import runtime as rt


class Store:
    """Counts how many times the whole library is read, so the refactor is measurable."""

    def __init__(self, buckets):
        self.buckets = buckets
        self.list_all_calls = 0
        self.fail = False

    async def list_all(self, include_archive=False):
        self.list_all_calls += 1
        if self.fail:
            raise OSError("the library cannot be read right now")
        return list(self.buckets)


class Logger:
    def __init__(self):
        self.lines = []

    def _record(self, msg, *a):
        self.lines.append(str(msg) % a if a else str(msg))

    warning = info = debug = error = _record


def bucket(bid, **meta):
    m = {"id": bid}
    m.update(meta)
    return {"id": bid, "content": "…", "metadata": m}


def pinned(bid, **meta):
    return bucket(bid, pinned=True, **meta)


@pytest.fixture
def world(monkeypatch):
    def build(buckets, cap=20):
        store = Store(buckets)
        logger = Logger()
        monkeypatch.setattr(rt, "bucket_mgr", store)
        monkeypatch.setattr(rt, "logger", logger)
        monkeypatch.setattr(rt, "config", {"limits": {"max_pinned": cap}})
        return store, logger
    return build


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── what counts ─────────────────────────

def test_only_the_pinned_flag_counts(world):
    # Criterion: `metadata.pinned` is the single source of truth. `type=permanent` is a
    # first-class kind of bucket and is NOT the same as pinned — counting it would eat
    # the quota with entries the door screen never shows.
    world([pinned("a"), bucket("b"), bucket("c", type="permanent")])
    assert run(C.count_pinned()) == 1


@pytest.mark.parametrize("terminal", [
    {"deleted_at": "2026-08-20T00:00:00"},
    {"tombstone": True},
    {"type": "archived"},
])
def test_an_archived_or_deleted_bucket_does_not_hold_a_slot(world, terminal):
    # Criterion: a memory that has been archived or soft-deleted is out of sight. Holding
    # a quota slot for it means the ceiling is reached by things nobody can see, and the
    # refusal message would name a number the person cannot reconcile with their screen.
    world([pinned("a"), pinned("b", **terminal)])
    assert run(C.count_pinned()) == 1


def test_a_closed_want_still_holds_its_slot(world):
    # Criterion: recorded as it actually is, not as it might ideally be. "Terminal" here
    # means archived or deleted — `status: resolved` / `abandoned` is a different axis and
    # does NOT release the slot.
    #
    # ⚠️ Whether it should is a real question, deliberately not answered here: pinning is
    #    for principles and `status` is for wants, so the overlap is rare, and changing it
    #    would silently free slots on somebody's library the next time they pin something.
    #    Frozen as-is; the question belongs to whoever owns the quota, not to a test.
    world([pinned("a"), pinned("b", status="resolved"), pinned("c", status="abandoned")])
    assert run(C.count_pinned()) == 3


def test_the_same_id_twice_counts_once(world):
    # Criterion: the same logical memory can appear twice in a listing (an older version
    # alongside its replacement). Counting both would silently halve the real cap.
    world([pinned("a"), pinned("a"), pinned("b")])
    assert run(C.count_pinned()) == 2


def test_an_unreadable_library_counts_as_zero_rather_than_blocking(world):
    # Criterion: THE conservative choice, and the one worth stating out loud. If the
    # library cannot be read, the quota must not become a wall — being unable to count is
    # not a reason to refuse. Failing the other way would turn a transient disk error into
    # "you may not pin anything", with a message about a quota that was never checked.
    store, _ = world([pinned("a")])
    store.fail = True
    assert run(C.count_pinned()) == 0


# ───────────────────────── the ceiling ─────────────────────────

def test_below_the_cap_nothing_is_said(world):
    world([pinned(f"p{i}") for i in range(3)], cap=20)
    assert run(C.check_pinned_quota()) is None


def test_at_the_cap_it_refuses_and_says_how_to_make_room(world):
    # Criterion: a refusal that does not say what to do next is a dead end. The message
    # carries both numbers and the exact call that frees a slot.
    world([pinned(f"p{i}") for i in range(20)], cap=20)
    msg = run(C.check_pinned_quota())
    assert msg
    assert "20" in msg
    assert "trace(" in msg


def test_a_cap_of_zero_means_no_ceiling_at_all(world):
    # Criterion: 0 is "unlimited", not "nothing may be pinned". Reading it the other way
    # would make a missing config value silently forbid pinning.
    world([pinned(f"p{i}") for i in range(50)], cap=0)
    assert run(C.check_pinned_quota()) is None


# ───────────────────────── the soft threshold ─────────────────────────

def test_near_the_cap_it_warns_but_still_allows(world):
    # Criterion: the soft threshold is a heads-up, not a refusal. Turning it into one
    # would cost two slots of a twenty-slot cap for no reason.
    world([pinned(f"p{i}") for i in range(18)], cap=20)
    assert run(C.enforce_pinned_quota(True)) is True


def test_at_the_cap_it_falls_back_to_an_ordinary_bucket(world):
    # Criterion: the newer path degrades instead of refusing — the memory is still saved,
    # it is simply not pinned. Losing the write entirely would be much worse than losing
    # the pin.
    world([pinned(f"p{i}") for i in range(20)], cap=20)
    assert run(C.enforce_pinned_quota(True)) is False


def test_asking_not_to_pin_never_touches_the_library(world):
    # Criterion: `pinned=False` is the overwhelmingly common case — every ordinary write.
    # Counting the library for it would put a full scan on the hot path for a question
    # already answered by the argument.
    store, _ = world([pinned(f"p{i}") for i in range(20)], cap=20)
    assert run(C.enforce_pinned_quota(False)) is False
    assert store.list_all_calls == 0


# ───────────────────────── how many times the library is read ─────────────────────────

def test_one_quota_check_reads_the_library_once(world):
    # Criterion: the number the next commit is about. Frozen here first so the refactor
    # has something to disagree with.
    store, _ = world([pinned("a")], cap=20)
    run(C.check_pinned_quota())
    assert store.list_all_calls == 1
