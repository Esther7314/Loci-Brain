"""
core/paging.py — the one reading of offset / limit / as_of every panel list pages by
(contract 「面板接口」 §一.2), and the page it builds.
"""

from datetime import datetime, timedelta

import pytest

from core import _when as W
from core import paging as P

NOW = datetime(2026, 10, 7, 21, 4, 11, 654321, tzinfo=W.LOCAL_TZ)


def test_defaults_are_offset_0_limit_5_and_now_to_the_second():
    assert P.page_args(now=NOW) == (0, 5, NOW.replace(microsecond=0))
    assert P.page_args("", "", "", now=NOW) == (0, 5, NOW.replace(microsecond=0))


def test_the_limit_is_held_to_50():
    assert P.page_args(limit="999", now=NOW)[1] == 50


@pytest.mark.parametrize("args", [
    {"offset": "x"}, {"limit": "x"}, {"offset": "-1"}, {"limit": "0"}, {"limit": "-3"},
    {"as_of": "yesterday-ish"},
])
def test_an_argument_that_does_not_read_is_refused(args):
    with pytest.raises(P.BadPage):
        P.page_args(now=NOW, **args)


def test_as_of_comes_back_as_it_went_out_even_with_its_plus_sent_as_a_space():
    handed = P.stamp(NOW)
    assert handed == "2026-10-07T21:04:11+08:00"
    assert P.page_args(as_of=handed)[2] == NOW.replace(microsecond=0)
    assert P.page_args(as_of=handed.replace("+", " "))[2] == NOW.replace(microsecond=0)


def test_args_of_reads_a_query_mapping():
    assert P.args_of({"offset": "5", "limit": "2"}, now=NOW)[:2] == (5, 2)


def test_a_row_of_as_ofs_own_second_stays_and_a_later_one_is_cut():
    as_of = NOW.replace(microsecond=0)
    rows = [{"at": NOW + timedelta(seconds=1)}, {"at": NOW}, {"at": None}]
    assert P.cut(rows, as_of, lambda r: r["at"]) == rows[1:]
    assert P.cut(rows, None, lambda r: r["at"]) == rows


def test_the_page_says_where_the_next_one_starts_and_null_at_the_end():
    as_of = NOW.replace(microsecond=0)
    first = P.page(list(range(7)), 0, 5, as_of)
    assert first == {"items": [0, 1, 2, 3, 4], "total": 7, "offset": 0, "limit": 5,
                     "next_offset": 5, "as_of": "2026-10-07T21:04:11+08:00"}
    assert P.page(list(range(7)), 5, 5, as_of)["next_offset"] is None
