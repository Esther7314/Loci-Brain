# -*- coding: utf-8 -*-
"""
tests/test_when.py — "her today" (core/_when.py)

The overall criterion this file guards is the three-row table at the top of `_when.py`.
Mixing the three readings up is **worse than not fixing anything** (it shifts the whole
historical dataset by 8 hours), so every one of them needs an assertion holding it down:

| what it looks like on disk | how it should be read |
|---|---|
| no suffix (in the container, `datetime.now().isoformat()`) | **as UTC**, then converted to local |
| carries `Z` / `+08:00` | at its own word |
| a bare date `YYYY-MM-DD` | that is "which day", not "which moment" — **on the local calendar** |

The original disease: there is no TZ in the container, so things stored in the small hours
were invisible under "today" the next day. She is a night owl, so this is something she ran
into every single day.
"""

from datetime import datetime, timedelta, timezone

import pytest

from core import _when as _w

UTC = timezone.utc

# This file runs on "her +08" by default (with LOCI_TZ unset, _when picks Asia/Shanghai).
# A few of the assertions are only meaningful given "local != UTC": if local *is* UTC, then
# the two ways of getting it wrong — "read as UTC" and "read as local" — give the same answer,
# and nothing is being tested at all.
_offset_from_utc = pytest.mark.skipif(
    _w.now().utcoffset() == timedelta(0),
    reason="LOCI_TZ has been set to UTC — under that configuration the timezone-reading "
           "assertions cannot tell right from wrong",
)


# ============================================================
# now / today —— the local calendar
# ============================================================

def test_now_always_carries_a_timezone():
    # Criterion: the entire reason this module exists is "stop using a naive datetime.now()".
    # A "now" with no timezone gets treated as UTC downstream, and the whole timeline shifts
    # by 8 hours.
    moment = _w.now()
    assert moment.tzinfo is not None
    assert moment.utcoffset() == _w.LOCAL_TZ.utcoffset(moment.replace(tzinfo=None))


def test_today_is_midnight_on_the_local_calendar_not_midnight_UTC():
    # Criterion: "today" is counted on **her calendar**.
    # Count it in UTC and anything stored between midnight and 8am Beijing time is invisible
    # under "today" the next day — the moment this disease was found was 1:47am on 8-04.
    today = _w.today()
    assert (today.hour, today.minute, today.second, today.microsecond) == (0, 0, 0, 0)
    assert today.tzinfo is not None
    assert today.date() == _w.now().date()      # the same day as the local "now"
    assert today <= _w.now()                    # midnight is before this moment, always


# ============================================================
# to_local —— the deadliest of the three readings
# ============================================================

def test_anything_without_a_timezone_is_read_as_UTC():
    # Criterion: the first row of the table. `created` / `last_active` are written by
    # `datetime.now().isoformat()` inside the container, and the container has no TZ, so those
    # digits are UTC.
    # What is asserted here is "the same instant", not "the same digits" — whether the
    # conversion is right is judged by whether the point on the timeline moved, not by what
    # the field looks like.
    naive = datetime(2026, 8, 3, 17, 47, 0)
    assert _w.to_local(naive) == naive.replace(tzinfo=UTC)


def test_anything_with_a_timezone_is_taken_at_its_own_word():
    # Criterion: the second row of the table. For a timestamp that states its own timezone,
    # this layer **changes the representation, not the instant**.
    # Get this wrong and you are reinterpreting a timestamp that already said what timezone it
    # was in, moving it several hours out of nowhere.
    for original in (datetime(2026, 8, 4, 1, 47, tzinfo=UTC),
                     datetime(2026, 8, 4, 1, 47, tzinfo=timezone(timedelta(hours=8))),
                     datetime(2026, 8, 4, 1, 47, tzinfo=timezone(timedelta(hours=-5)))):
        assert _w.to_local(original) == original


def test_everything_comes_out_as_local_aware():
    # Criterion: this function has exactly one shape of output. Without a uniform output,
    # downstream code sometimes gets a naive datetime and sometimes one in another timezone,
    # and comparisons go wrong silently (no exception raised).
    for original in (datetime(2026, 8, 4, 1, 47),
                     datetime(2026, 8, 4, 1, 47, tzinfo=UTC)):
        converted = _w.to_local(original)
        assert converted.tzinfo is _w.LOCAL_TZ


@_offset_from_utc
def test_the_small_hours_case_the_containers_UTC_reads_back_as_her_next_day_small_hours():
    # Criterion: this file was written for this scenario. She stored something at 1:47am on
    # 8-04; what the container wrote to disk was the UTC string `2026-08-03T17:47:00`
    # (no suffix). Only by reading it as UTC and then converting to local do you get back
    # "the small hours of 8-04" — read straight as local it is the afternoon of 8-03, and that
    # memory is missing from her "today".
    back = _w.parse_stamp("2026-08-03T17:47:00")
    assert (back.year, back.month, back.day) == (2026, 8, 4)
    assert (back.hour, back.minute) == (1, 47)


# ============================================================
# parse_date —— a date is a calendar, not a moment
# ============================================================

def test_a_bare_date_lands_on_local_midnight_of_that_day():
    # Criterion: the third row of the table. `when="2026-08-04"` says "which day", not "which
    # moment", so it is midnight of that day on the **local calendar**.
    that_day = _w.parse_date("2026-08-04")
    assert (that_day.year, that_day.month, that_day.day) == (2026, 8, 4)
    assert (that_day.hour, that_day.minute) == (0, 0)
    assert that_day.tzinfo is _w.LOCAL_TZ


@_offset_from_utc
def test_a_bare_date_is_not_UTC_midnight_converted_over():
    # Criterion: telling the two ways of being wrong apart. Treat `2026-08-04` as UTC midnight
    # and convert to local and you get 8am local — the boundaries of the whole day shift by
    # 8 hours with it, so "the day of 8-04" would miss her first 8 hours of 8-04 and wrongly
    # include the first 8 hours of 8-05.
    assert _w.parse_date("2026-08-04") != datetime(2026, 8, 4, tzinfo=UTC).astimezone(_w.LOCAL_TZ)


def test_a_date_with_a_time_hanging_off_it_still_yields_just_that_day():
    # Criterion: this function only eats the first 10 characters (`s[:10]`).
    # Hand it a full timestamp and what it gives back is **midnight of that day**, not that
    # moment — it is called parse_date, so the output should be the start of a whole day; it
    # can't be a moment sometimes and midnight other times.
    assert _w.parse_date("2026-08-04T13:22:31+08:00") == _w.parse_date("2026-08-04")


# ============================================================
# parse_stamp —— reading back the time strings that were written to disk
# ============================================================

def test_a_bare_date_goes_down_the_calendar_path():
    # Criterion: when `parse_stamp` sees a bare date it has to take the `parse_date` path (the
    # local calendar), not hand it to fromisoformat as a naive value and then read it as UTC.
    # The two paths differ by 8 hours, and both of them look like they "work".
    assert _w.parse_stamp("2026-08-04") == _w.parse_date("2026-08-04")


def test_both_the_Z_suffix_and_the_plus_08_00_suffix_are_honored_and_not_sliced_off():
    # Criterion: the spot the docstring names as codex #4 — no more `s[:19]` slicing, which
    # cuts off `Z` and `+08:00` along with everything else, i.e. forcibly turns a timestamp
    # that stated its timezone into "timezone unknown".
    # These two strings refer to **the same instant** and must read back equal; with the suffix
    # sliced off they turn into two times 8 hours apart.
    assert _w.parse_stamp("2026-08-03T17:47:00Z") == _w.parse_stamp("2026-08-04T01:47:00+08:00")


def test_both_microseconds_and_a_space_separator_can_be_read():
    # Criterion: the strings written to disk come out of `datetime.isoformat()`, so they
    # **carry microseconds**; hand-written ones, or ones imported from elsewhere, may use a
    # space separator. These two commonest shapes must not be unreadable — unreadable returns
    # None, and None upstream means "this one has no time", so the whole memory falls off the
    # timeline.
    assert _w.parse_stamp("2026-08-03T17:47:00.123456") is not None
    assert _w.parse_stamp("2026-08-03 17:47:00") == _w.parse_stamp("2026-08-03T17:47:00")


def test_empty_values_all_give_None_without_blowing_up():
    # Criterion: this is a read path, what gets fed in is old data off the disk, and missing
    # fields are the norm. A missing field should quietly become None (upstream skips that
    # entry); it must not raise and take the whole recall down with it.
    for value in (None, "", "   ", 0):
        assert _w.parse_stamp(value) is None


def test_when_other_words_trail_the_date_only_the_leading_date_is_taken():
    # Criterion: `when` is free text I write myself, and it is often something like
    # "2026-07-15 that afternoon". If a date can be fished out, don't throw the whole thing
    # away — the price of throwing it away is that this memory has no time and never enters
    # the timeline.
    assert _w.parse_stamp("2026-07-15 that afternoon") == _w.parse_date("2026-07-15")


def test_a_date_that_is_not_at_the_start_is_refused_None_is_preferred():
    # Criterion: it uses `.match` (anchored at the start), not `.search`.
    # Hunting for a date anywhere in the string would take some year mentioned in passing in
    # the body and make it this memory's timestamp — and a guessed-wrong time is far worse
    # than no time: no time only means it doesn't show up, while a guessed-wrong time sorts
    # into the wrong position and looks perfectly normal doing it.
    assert _w.parse_stamp("it was 2026-07-15, that day") is None


def test_something_completely_unreadable_gives_None():
    # Criterion: the first line of the docstring, "unreadable gives None".
    assert _w.parse_stamp("last month") is None
    assert _w.parse_stamp("garbage") is None


def test_a_date_shaped_string_for_a_day_that_does_not_exist_should_also_give_None():
    # Criterion: the first line of the docstring, "unreadable gives None" — **gives None, does
    # not raise**. Upstream (`_ts_of` / `_bigevent.parse_span`) is written throughout on the
    # assumption that "None = this one has no time", and not one caller wraps this function in
    # a try. So a single bucket like this would flip the entire recall over, rather than
    # quietly falling off the timeline by itself.
    # The input picked is **the kind that looks most real**: September has no 31st, and since
    # `when` is hand-written by me, writing `2026-09-31` is entirely possible — it looks
    # exactly like a valid date and nothing about it would make you suspicious.
    # 🔴 This one is currently **red**: the `_DATE_ONLY` branch calls parse_date directly with
    #    no try, while the "other words trailing" branch below it is wrapped in a try — one
    #    function, two temperaments.
    #    Written up in its own section of the handover report.
    assert _w.parse_stamp("2026-09-31") is None


# ============================================================
# year_week / year_month —— the time-gradient view segments by calendar week / calendar month
# ============================================================

def test_calendar_weeks_follow_ISO_and_a_week_straddling_new_year_belongs_to_the_earlier_year():
    # Criterion: it uses ISO weeks, and ISO weeks are **not cut at New Year's Day**:
    # 2027-01-01 is a Friday, and the Thursday of the week it belongs to is still in 2026, so
    # the whole week counts as week 53 of 2026.
    # What this holds down is "one week must not be split in half by the year boundary" — split
    # it and the days around New Year turn into two fragments of two or three entries each
    # when looking back by week.
    assert _w.year_week(datetime(2027, 1, 1)) == (2026, 53)
    assert _w.year_week(datetime(2026, 12, 31)) == (2026, 53)


def test_calendar_weeks_are_cut_at_Monday_not_seven_days_from_some_arbitrary_start():
    # Criterion: what recall's `_split_calendar` replaced was exactly the "7-day block counted
    # from the oldest entry" scheme. When people say "by week" they mean the week on the
    # calendar. 8-16 is a Sunday and 8-17 is a Monday: those two days have to land in
    # **different** weeks, and 8-17 and 8-23 (the next Sunday) have to land in the same one.
    assert _w.year_week(datetime(2026, 8, 16)) != _w.year_week(datetime(2026, 8, 17))
    assert _w.year_week(datetime(2026, 8, 17)) == _w.year_week(datetime(2026, 8, 23))


def test_calendar_months_follow_the_calendar_month_end_of_month_and_the_1st_may_not_share_a_slot():
    # Criterion: same as above (codex review #8) — it used to be 30-day blocks, with the result
    # that January 31st and February 1st could land in the same "month".
    assert _w.year_month(datetime(2026, 1, 31)) == (2026, 1)
    assert _w.year_month(datetime(2026, 2, 1)) == (2026, 2)


def test_the_same_month_number_in_different_years_may_not_be_merged_into_one_slot():
    # Criterion: the segmentation key has to carry the year, otherwise last August and this
    # August stack on top of each other — while looking back, there is a whole year between
    # those two stretches.
    assert _w.year_month(datetime(2025, 8, 19)) != _w.year_month(datetime(2026, 8, 19))


@_offset_from_utc
def test_week_and_month_bucketing_use_the_literal_calendar_passed_in_and_convert_no_timezones_for_the_caller():
    # Criterion: these two functions **do not call to_local**; they only read the literal
    # fields off the datetime. So "convert to local first" is the caller's responsibility
    # (recall's `_split_calendar` takes what comes out of `_ts_of()`, which is local aware, so
    # it is correct today).
    # This assertion nails that implicit contract down: whoever one day passes a UTC time into
    # these two functions will have the last few hours of the month sorted into the previous
    # month, and the result will look perfectly normal.
    month_end_utc = datetime(2026, 8, 31, 20, 0, tzinfo=UTC)     # locally it is already the small hours of 9-01
    assert _w.year_month(month_end_utc) == (2026, 8)
    assert _w.year_month(_w.to_local(month_end_utc)) == (2026, 9)
