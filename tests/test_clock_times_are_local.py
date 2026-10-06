# -*- coding: utf-8 -*-
"""
tests/test_clock_times_are_local.py — a clock time the model writes is a local time.

A stored stamp without an offset is read as UTC (core/_when.py), so `2026-10-03 20:00`
written as given would read eight hours late and `waits_for_clock` / `due_now` would fire
at the wrong hour. grow and trace write the local offset out; trace takes the same shape,
so an hour the backfill read (`2026-10-12T14:00+08:00`) can be corrected.
"""

import asyncio
from datetime import datetime

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _when
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.trace.core import trace_core


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def run(coro):
    return asyncio.run(coro)


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _local(stamp) -> tuple:
    t = _when.parse_stamp(stamp)
    return (t.date().isoformat(), t.hour, t.minute)


def test_grow_writes_a_clock_time_with_the_local_offset(store, tmp_path):
    out = run(grow(kind="event", direction_of_fit="telic",
                   items=[{"room": "EVENT/SELF", "text": "Call the shop.", "v": 0.5,
                           "a": 0.3, "when": "2031-10-03 20:00"}]))
    bid = out.split("📝", 1)[1].split()[0]
    when = str(_disk(tmp_path, bid)["when"])
    assert when == datetime(2031, 10, 3, 20, 0, tzinfo=_when.LOCAL_TZ).isoformat(
        timespec="minutes"), when
    assert _local(when) == ("2031-10-03", 20, 0)


def test_a_bare_date_stays_a_date(store, tmp_path):
    out = run(grow(kind="event", direction_of_fit="telic",
                   items=[{"room": "EVENT/SELF", "text": "Renew it.", "v": 0.5, "a": 0.3,
                           "when": "2031-10-03"}]))
    bid = out.split("📝", 1)[1].split()[0]
    assert str(_disk(tmp_path, bid)["when"]) == "2031-10-03"


@pytest.mark.parametrize("given", ["2031-10-12 14:00", "2031-10-12T14:00+08:00"])
def test_trace_takes_a_clock_time_on_a_want(store, tmp_path, given):
    bid = run(store.create("Pick up the cake.", room="EVENT/SELF",
                           direction_of_fit="telic", when="2031-10-12T09:00+08:00"))
    out = run(trace_core(bid, when=given))
    assert out.startswith("已修改记忆桶"), out
    assert _local(_disk(tmp_path, bid)["when"]) == ("2031-10-12", 14, 0)


def test_trace_corrects_the_hour_of_a_lived_event(store, tmp_path):
    bid = run(store.create("We met at the station.", room="EVENT/SELF",
                           when="2026-09-01"))
    out = run(trace_core(bid, when="2026-09-01 18:30"))
    assert out.startswith("已修改记忆桶"), out
    assert _local(_disk(tmp_path, bid)["when"]) == ("2026-09-01", 18, 30)
