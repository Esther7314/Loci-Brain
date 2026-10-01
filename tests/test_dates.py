# -*- coding: utf-8 -*-
"""
tests/test_dates.py — core/_dates: a copied Chinese time phrase, read against a fixed day.

The side model copies the phrase; this table is the whole of what code reads out of it.
"Today" is Wednesday 2026-10-07 on the Asia/Shanghai calendar, so every relative form has
one right answer, and a phrase that fits no form comes back as None — never a guess.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from core import _dates

try:
    from zoneinfo import ZoneInfo
    SHANGHAI = ZoneInfo("Asia/Shanghai")
except Exception:          # no tzdata on this machine: the same fixed +8 core/_when falls back to
    SHANGHAI = timezone(timedelta(hours=8))

TODAY = date(2026, 10, 7)
assert TODAY.isoweekday() == 3, "the table is written for a Wednesday"

CASES = [
    # phrase,            when it resolves to,         yearly
    ("今天",             "2026-10-07",                False),
    ("明天",             "2026-10-08",                False),
    ("后天",             "2026-10-09",                False),
    ("大后天",           "2026-10-10",                False),
    ("昨天",             "2026-10-06",                False),
    ("下周一",           "2026-10-12",                False),
    ("周一",             "2026-10-12",                False),   # the next Monday, not this week's
    ("这周一",           "2026-10-05",                False),
    ("这周五",           "2026-10-09",                False),
    ("星期天",           "2026-10-11",                False),
    ("这周末",           "2026-10-10",                False),
    ("下周末",           "2026-10-17",                False),
    ("月底",             "2026-10-31",                False),
    ("月底前",           "2026-10-31",                False),
    ("下月初",           "2026-11-01",                False),
    ("下个月5号",        "2026-11-05",                False),
    ("十一月三号",       "2026-11-03",                False),
    ("二十三号",         "2026-10-23",                False),
    ("2027年1月3日",     "2027-01-03",                False),
    ("下午两点",         "2026-10-07T14:00+08:00",    False),
    ("两点",             "2026-10-07T14:00+08:00",    False),   # 1-6 o'clock alone is afternoon
    ("十一点半",         "2026-10-07T11:30+08:00",    False),
    ("后天晚上八点",     "2026-10-09T20:00+08:00",    False),
    ("明晚八点",         "2026-10-08T20:00+08:00",    False),
    ("下周一两点",       "2026-10-12T14:00+08:00",    False),
    ("明天上午9:30",     "2026-10-08T09:30+08:00",    False),
    ("每年8月7号",       "2026-08-07",                True),
    ("每年的十月一日",   "2026-10-01",                True),
]

UNREADABLE = ["过几天", "有空的时候", "下次一定", "2月30号", "晚上十二点", "每年明天", ""]


@pytest.mark.parametrize("phrase, want, yearly", CASES)
def test_each_form_resolves_against_the_fixed_day(phrase, want, yearly):
    got = _dates.resolve_phrase(phrase, TODAY, tz=SHANGHAI)
    assert got is not None, f"{phrase!r} should be read"
    assert got.stamp(SHANGHAI) == want
    assert got.yearly is yearly


@pytest.mark.parametrize("phrase", UNREADABLE)
def test_a_phrase_no_form_fits_is_not_guessed(phrase):
    assert _dates.resolve_phrase(phrase, TODAY, tz=SHANGHAI) is None


def test_the_table_is_big_enough_to_mean_something():
    assert len(CASES) >= 15 and len([p for p in UNREADABLE if p]) >= 3


def test_yearly_from_the_side_model_needs_a_month_and_a_day():
    got = _dates.resolve_phrase("8月7号", TODAY, tz=SHANGHAI, yearly=True)
    assert (got.day, got.yearly) == (date(2026, 8, 7), True)
    assert _dates.resolve_phrase("下周一", TODAY, tz=SHANGHAI, yearly=True) is None


def test_a_stated_year_is_kept_for_a_yearly_date():
    got = _dates.resolve_phrase("2019年8月7号", TODAY, tz=SHANGHAI, yearly=True)
    assert got.stamp(SHANGHAI) == "2019-08-07" and got.yearly


def test_today_given_as_a_stored_utc_stamp_is_put_on_the_local_calendar():
    # `created` is naive UTC: 18:30 on the 6th is already the 7th at +08, so 后天 is the 9th.
    created = datetime(2026, 10, 6, 18, 30)
    got = _dates.resolve_phrase("后天", created, tz=SHANGHAI)
    assert got.day == date(2026, 10, 9)
    assert _dates.local_day("2026-10-06T18:30:00", SHANGHAI) == date(2026, 10, 7)
    assert _dates.local_day("", SHANGHAI) is None
