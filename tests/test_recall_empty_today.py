# -*- coding: utf-8 -*-
"""
tests/test_recall_empty_today.py — an empty when="today" in the small hours points at yesterday.

A nightly run after midnight asks recall(when="today") for the day it just lived and gets
nothing, every night: "today" began a few hours ago. The empty answer says what today is
and what time it is, and names when="yesterday"; later in the day it stays as it was.
"""

import asyncio
from datetime import datetime

import pytest

from core import _when
from core import runtime as rt
from tools.recall import core as recall


class _Store:
    async def list_all(self, include_archive=False):
        return []


@pytest.fixture
def at(monkeypatch):
    monkeypatch.setattr(rt, "bucket_mgr", _Store(), raising=False)

    def set_clock(hour, minute=0):
        fixed = datetime(2026, 10, 20, hour, minute, tzinfo=_when.LOCAL_TZ)
        monkeypatch.setattr(_when, "now", lambda: fixed)
    return set_clock


def _empty(when):
    return asyncio.run(recall.recall_core(when, "", "", "", collected=([], None, None)))


def test_today_at_four_in_the_morning_names_the_date_the_time_and_yesterday(at):
    at(4, 21)
    out = _empty("today")
    assert "今天是 10-20" in out and "现在 04:21" in out and "when=yesterday" in out
    assert _empty("今天") == out.replace("when=today", "when=今天")


def test_today_in_the_evening_and_other_words_keep_the_plain_answer(at):
    at(20, 5)
    assert _empty("today") == "这儿没有东西（when=today）。门再开大一点试试。"
    at(4, 21)
    assert _empty("yesterday") == "这儿没有东西（when=yesterday）。门再开大一点试试。"
