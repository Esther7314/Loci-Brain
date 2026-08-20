# -*- coding: utf-8 -*-
"""Unit tests for `covering()` — the first function that had to be *changed* to be testable.

WHY THIS FILE EXISTS AT ALL
    Every other test in `tests/` covers a function that was already pure. This one
    covers a function that used to reach out and grab the whole world by itself:

        async def covering(t0, t1):
            buckets = await rt.bucket_mgr.list_all()   # <- reached for the global

    Testing that meant standing up a bucket manager, a decay engine, a vector index,
    a logger and a config first. So it was never tested.

    The signature is now `covering(buckets, t0, t1)` — the caller hands the list in.
    That single change did two things at once, and the second is the interesting one:

      1. It made the function testable (this file).
      2. It made a whole class of bug harder to write. The browse view calls this
         once per time-cell and once per day — seven or eight times per request —
         and each call used to re-scan the entire library, *invisibly to the caller*.
         With the list passed in, a caller that fetches it eight times can see itself
         doing that.

    CRITERION WORTH KEEPING: prefer the refactors where "makes it testable" and
    "makes that bug harder to write" are the same edit. Those pay twice.

WHAT IS DELIBERATELY NOT TESTED HERE
    Anything about how the caller obtains the bucket list. That is the caller's
    problem now, which is the entire point.
"""
from datetime import datetime, timedelta

from core import _bigevent as B
from core import _when as W

TAG = B.BIGEVENT_TAG


def bucket(bid: str, when: str, content: str = "", **meta):
    """Minimal shape `covering()` actually reads: a metadata dict plus content."""
    m = {"id": bid, "when": when, "tags": [TAG], **meta}
    return {"id": bid, "content": content or f"period {bid}", "metadata": m}


def day(s: str) -> datetime:
    return W.parse_date(s)


# ───────────────────────── the plain path ─────────────────────────

def test_overlap_not_containment_is_the_rule():
    # Criterion: periods overlap each other; they are not a relay race where one
    # ends and the next begins. A period that merely *clips* the window counts.
    # If this ever tightened to containment, every long-running period would
    # silently stop covering the short windows inside it.
    early = bucket("a", "2026-07-01..2026-07-10")
    middle = bucket("b", "2026-07-05..2026-07-20")
    late = bucket("c", "2026-08-01..2026-08-10")
    hits = {t[2] for t in B.covering([early, middle, late], day("2026-07-08"), day("2026-07-09"))}
    assert hits == {"a", "b"}


def test_window_edges_are_half_open():
    # A period that ends exactly when the window starts does NOT count; one that
    # starts exactly when the window ends does NOT count. Both boundaries are
    # exclusive on the far side, which is what keeps two adjacent windows from
    # both claiming the same period.
    ends_at_window_start = bucket("before", "2026-07-01..2026-07-07")   # end is exclusive → 07-08 00:00
    starts_at_window_end = bucket("after", "2026-07-10..2026-07-20")
    hits = {t[2] for t in B.covering([ends_at_window_start, starts_at_window_end],
                                     day("2026-07-08"), day("2026-07-10"))}
    assert hits == set()


def test_no_window_means_now_so_finished_periods_drop_out():
    # Both bounds absent means "what is covering me right now". A period that has
    # already ended must not answer that question — this is the whole reason the
    # end date lives in the data instead of being maintained by hand.
    finished = bucket("done", "2020-01-01..2020-02-01")
    still_running = bucket("open", "2020-01-01..")
    hits = {t[2] for t in B.covering([finished, still_running], None, None)}
    assert hits == {"open"}


def test_newest_first():
    # The browse view takes only the first result per cell, so the ordering is not
    # cosmetic — it decides which period gets shown at all.
    older = bucket("old", "2026-06-01..2026-06-30")
    newer = bucket("new", "2026-07-01..2026-07-30")
    out = B.covering([older, newer], day("2026-06-01"), day("2026-08-01"))
    assert [t[2] for t in out] == ["new", "old"]


# ───────────────────── what must never leak through ─────────────────────

def test_only_periods_not_ordinary_memories():
    # `covering` answers "which periods span this window". An ordinary memory that
    # happens to carry a date is not a period, and letting one through would put a
    # random memory in the slot reserved for "what were we doing back then".
    ordinary = {"id": "plain", "content": "an ordinary memory",
              "metadata": {"id": "plain", "when": "2026-07-05..2026-07-06", "tags": []}}
    period = bucket("period", "2026-07-01..2026-07-10")
    hits = {t[2] for t in B.covering([ordinary, period], day("2026-07-05"), day("2026-07-06"))}
    assert hits == {"period"}


def test_superseded_covered_resolved_archived_all_drop_out():
    # Four different ways of "this one no longer speaks for itself". They are tested
    # together because they share one consequence: if any of them leaked, the browse
    # view would show a period the user already replaced, folded away, closed, or
    # archived — and it would look exactly like a live one.
    live = bucket("live", "2026-07-01..2026-07-10")
    superseded = bucket("superseded", "2026-07-01..2026-07-10", superseded_by="x")
    covered = bucket("covered", "2026-07-01..2026-07-10", covered_by=["x"])
    closed = bucket("resolved", "2026-07-01..2026-07-10", status="resolved")
    archived = bucket("archived", "2026-07-01..2026-07-10", type="archived")
    deleted = bucket("deleted", "2026-07-01..2026-07-10", deleted_at="2026-07-11")
    hits = {t[2] for t in B.covering(
        [live, superseded, covered, closed, archived, deleted], day("2026-07-05"), day("2026-07-06"))}
    assert hits == {"live"}


def test_a_period_whose_dates_are_unreadable_is_skipped_not_fatal():
    # A date that looks right but does not exist (September has no 31st) used to
    # raise straight out of here, taking the whole recall down with it — not one
    # missing period, the entire request. It is now skipped like any other
    # unusable entry. See `_when.parse_date_or_none` for the other half of this.
    malformed = bucket("broken", "2026-09-31..2026-10-01")
    well_formed = bucket("fine", "2026-09-01..2026-10-01")
    hits = {t[2] for t in B.covering([malformed, well_formed], day("2026-09-15"), day("2026-09-16"))}
    assert hits == {"fine"}


def test_limit_is_honoured_after_sorting_not_before():
    # The cap exists so a library with hundreds of periods cannot flood one cell.
    # It must be applied to the *sorted* list: capping first and sorting after
    # would return an arbitrary subset that merely looks ordered.
    buckets = [bucket(f"p{i:02}", f"2026-{i:02}-01..2026-{i:02}-28") for i in range(1, 13)]
    out = B.covering(buckets, day("2026-01-01"), day("2027-01-01"), limit=3)
    assert [t[2] for t in out] == ["p12", "p11", "p10"]


# ─────────────── the property the refactor was actually for ───────────────

def test_the_caller_owns_the_list_so_the_function_reads_nothing_else():
    # This is the test that would have been impossible before the change, and it is
    # the one that states the new contract: hand it an empty list and it returns
    # nothing, no matter what the real library happens to contain. Any future edit
    # that quietly reaches for the global again turns this red.
    assert B.covering([], None, None) == []
    assert B.covering([], day("2020-01-01"), day("2030-01-01")) == []


def test_it_is_not_a_coroutine_anymore():
    # It touches neither disk nor network now, so it must not be awaitable. A stray
    # `await` at a call site would otherwise hand back a coroutine that gets used as
    # a list — and the failure would be loud, but only at runtime, only on that path.
    import inspect
    assert not inspect.iscoroutinefunction(B.covering)


def test_content_and_id_come_back_verbatim():
    # The browse view renders straight from what this returns, so the text must be
    # the stored text — not a summary, not a truncation. Same rule as everywhere
    # else in this system: a memory's own words do not pass through anything.
    body = "The stretch where we moved the memory system onto a name of our own.\nsecond line"
    out = B.covering([bucket("x", "2026-07-01..2026-07-10", body)], day("2026-07-05"), day("2026-07-06"))
    assert len(out) == 1
    meta, content, bid = out[0]
    assert content == body and bid == "x" and meta["when"] == "2026-07-01..2026-07-10"


def test_an_open_ended_period_covers_anything_after_its_start():
    # "Still going" is stored as an empty end, not as a far-future date. A window
    # years ahead still has to match, or an ongoing period would quietly stop
    # covering the present the moment the calendar moved past whatever we guessed.
    open_ended = bucket("running", "2026-07-01..")
    hits = {t[2] for t in B.covering([open_ended], day("2030-01-01"), day("2030-01-02"))}
    assert hits == {"running"}


def test_a_period_starting_after_the_window_does_not_count():
    # Guards the direction of the comparison. Getting this backwards would make
    # every future period show up on every past window — and it would look like a
    # feature ("look how much context we have") rather than a bug.
    later_on = bucket("later", "2026-09-01..2026-09-30")
    hits = {t[2] for t in B.covering([later_on], day("2026-07-01"), day("2026-07-31"))}
    assert hits == set()


def test_only_the_start_bound_given():
    # Half-open windows are a real call shape from the browse view (the far cell).
    # A period that ended before the window starts drops; everything later stays.
    long_finished = bucket("past", "2026-01-01..2026-02-01")
    still_inside = bucket("inside", "2026-07-01..2026-08-01")
    hits = {t[2] for t in B.covering([long_finished, still_inside], day("2026-06-01"), None)}
    assert hits == {"inside"}


def test_zero_length_window_still_matches_a_period_spanning_it():
    # `_cell_span` can hand back t0 == t1 for a single-entry cell. A period that
    # spans that instant must still be found — otherwise the thinnest cells, which
    # are exactly the ones with least context of their own, would lose their label.
    moment = day("2026-07-05")
    period = bucket("span", "2026-07-01..2026-07-10")
    assert {t[2] for t in B.covering([period], moment, moment)} == {"span"}


def test_a_period_with_no_when_at_all_falls_back_to_created():
    # Hand-written entries from before the range format existed have no `when`.
    # They are not discarded: `parse_span` falls back to `created` and treats them
    # as still running, so the oldest periods keep working.
    old_bucket = {"id": "ancient", "content": "from before the format existed",
            "metadata": {"id": "ancient", "tags": [TAG], "created": "2026-07-01"}}
    assert {t[2] for t in B.covering([old_bucket], day("2026-08-01"), day("2026-08-02"))} == {"ancient"}


def test_the_input_list_is_not_mutated():
    # The caller now owns the list and hands the same one to seven or eight calls in
    # a row. If this sorted or trimmed it in place, every later call would receive a
    # different list than the caller thinks it passed — and the bug would surface as
    # "periods go missing further down the page", nowhere near the cause.
    buckets = [bucket("a", "2026-07-01..2026-07-10"), bucket("b", "2026-06-01..2026-06-30")]
    verbatim = [b["id"] for b in buckets]
    B.covering(buckets, None, None)
    assert [b["id"] for b in buckets] == verbatim
    assert len(buckets) == 2


def test_the_far_future_window_is_not_special_cased():
    # Guards against someone "helpfully" treating an absent end date as now(),
    # which would make an ongoing period stop covering tomorrow.
    open_ended = bucket("running", "2026-07-01..")
    tomorrow = W.now() + timedelta(days=1)
    assert {t[2] for t in B.covering([open_ended], tomorrow, tomorrow + timedelta(days=1))} == {"running"}
