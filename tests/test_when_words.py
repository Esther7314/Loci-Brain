# -*- coding: utf-8 -*-
"""The `when` parameter's word table: every stretch has a Chinese spelling and an English one.

WHY THIS EXISTS
    `when` is the first parameter anyone reaches for, and for a long time it accepted only
    Chinese words — 今天, 上周, 本月 — while the tool description, the README and every
    comment around it were in English. So the obvious first call, `recall(when="today")`,
    failed with "when 看不懂".

    Nothing was broken and nothing raised. The tool simply said one thing and did another,
    which is the fault this codebase is careful about everywhere else and had committed
    against its own most-used parameter.

    English spellings were added as pure aliases. **No Chinese spelling was removed**, and
    that half matters as much as the addition: the memories, the panel and the person using
    it are all still Chinese, so dropping the Chinese forms to "finish the translation"
    would have broken the thing in daily use to tidy up a thing nobody had complained about.

WHAT THIS GUARDS
    · both spellings resolve to exactly the same stretch — not merely "both work"
    · no Chinese spelling was lost
    · nonsense is still rejected, and the rejection still lists what IS accepted
"""
from datetime import timedelta

import pytest

from tools.recall.core import _parse_when

# Each pair is one stretch of time under both its names.
PAIRS = [
    ("今天", "today"),
    ("昨天", "yesterday"),
    ("前天", "day before yesterday"),
    ("本周", "this week"),
    ("上周", "last week"),
    ("本月", "this month"),
    ("上月", "last month"),
    ("今年", "this year"),
]


@pytest.mark.parametrize("chinese,english", PAIRS)
def test_both_spellings_mean_the_same_stretch(chinese, english):
    # Criterion: "both are accepted" is not enough — they have to resolve to the SAME
    # window, or the two spellings quietly become two different features and a person
    # switching between them gets different answers to the same question.
    #
    # ⚠️ The end of an open-ended stretch is "now", which moves between the two calls, so
    #    the start is compared exactly and the end within a second. Comparing both exactly
    #    fails on `今天` for a reason that has nothing to do with the table.
    a1, b1, err1 = _parse_when(chinese)
    a2, b2, err2 = _parse_when(english)
    assert not err1 and not err2
    assert a1 == a2
    assert abs((b1 - b2).total_seconds()) < 1


@pytest.mark.parametrize("chinese,_english", PAIRS)
def test_no_chinese_spelling_was_lost(chinese, _english):
    # Criterion: the half of this change that is easy to get wrong. The library, the panel
    # and the person are all in Chinese; "translating" this table rather than extending it
    # would break daily use to fix something nobody had hit.
    _, _, err = _parse_when(chinese)
    assert not err


@pytest.mark.parametrize("spelling", ["lastweek", "thisweek", "thismonth", "lastmonth",
                                      "thisyear", "dayBeforeYesterday"])
def test_the_spaceless_spellings_work_too(spelling):
    # Criterion: a parameter value gets typed, and "last week" is as likely to arrive
    # without the space as with it. Accepting only one of the two would make the failure
    # depend on a space, which is the least guessable kind.
    _, _, err = _parse_when(spelling)
    assert not err


@pytest.mark.parametrize("other", ["48h", "7d", "2026-07", "2026-07-15",
                                   "2026-07-01..2026-07-15"])
def test_the_non_word_forms_are_untouched(other):
    # Criterion: durations and dates were always accepted and are not part of this change.
    _, _, err = _parse_when(other)
    assert not err


def test_nonsense_is_still_refused():
    # Criterion: adding aliases must not turn the table into something that accepts
    # anything. A `when` that silently matched nothing would return an empty stretch and
    # read as "there are no memories then" rather than "that is not a time I understand".
    a, b, err = _parse_when("那阵子")
    assert a is None and b is None
    assert err


def test_the_refusal_lists_both_alphabets():
    # Criterion: the error message is where someone learns what to type instead, so it has
    # to show the forms that now exist. An accurate table with a stale error message would
    # leave an English reader exactly where they started.
    _, _, err = _parse_when("那阵子")
    assert "今天" in err and "today" in err
    assert "last week" in err
