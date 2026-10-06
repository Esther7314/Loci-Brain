# -*- coding: utf-8 -*-
"""
tests/test_grow_telic.py — grow and trace speak direction_of_fit, not tense="want".

Pins the tool-layer rules of v2 part 1: telic and its companions land in the same write
as the body; lived events happen only in the past; bound names a side instead of a
pronoun; status only says open or closed.
"""

import asyncio
from datetime import timedelta

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _when
from core.bucket_manager import BucketManager
from core import runtime as rt
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
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    monkeypatch.setenv("AI_NAME", "DT")
    monkeypatch.setenv("LOCI_OWNER_NAME", "Es")
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def _only_meta(tmp_path) -> dict:
    [path] = list(tmp_path.rglob("*.md"))
    return frontmatter.load(path).metadata


def _day(offset: int) -> str:
    return (_when.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


ITEM = {"room": "EVENT/SELF", "text": "Finish her gift before her birthday.", "v": 0.7, "a": 0.6}


def test_a_telic_event_is_written_whole_in_one_go(store):
    out = run(grow_mod.dispatch(kind="event", items=[dict(ITEM, when=_day(10))],
                                direction_of_fit="telic", bound=["我", "你"], weight=0.8))
    assert "[telic]" in out
    meta = _only_meta(store)
    assert meta["direction_of_fit"] == "telic"
    assert meta["bound"] == ["DT", "Es"], "我 is the AI, 你 is the owner"
    assert meta["weight"] == 0.8
    assert "status" not in meta, "open is the absence of a closing status"


def test_a_duration_is_a_when_only_for_something_wanted(store):
    ok = run(grow_mod.dispatch(kind="event", items=[dict(ITEM, when="3w")],
                               direction_of_fit="telic"))
    assert "[telic]" in ok
    refused = run(grow_mod.dispatch(kind="event", items=[dict(ITEM, text="x", when="3w")]))
    assert "when 格式无效" in refused


def test_a_lived_event_in_the_future_is_refused(store):
    out = run(grow_mod.dispatch(kind="event", items=[dict(ITEM, when=_day(3))]))
    assert "亲历只能在过去" in out
    assert not list(store.rglob("*.md"))


def test_the_future_is_fine_when_imagined_or_heard(store):
    imagined = run(grow_mod.dispatch(
        kind="event", internally_generated=True,
        items=[dict(ITEM, text="One day we live by the sea.", when=_day(400))]))
    assert "已落盘" in imagined
    heard = run(grow_mod.dispatch(
        kind="event",
        items=[dict(ITEM, room="EVENT/WORLD", text="Her brother arrives next week.",
                    when=_day(7))]))
    assert "已落盘" in heard


@pytest.mark.parametrize("kwargs,needle", [
    ({"bound": ["我"]}, "bound 只跟"),
    ({"weight": 0.5}, "weight 只跟"),
    ({"evidential": "inference"}, "evidential 只给 mind"),
    ({"direction_of_fit": "want"}, "direction_of_fit 只有两个值"),
    ({"direction_of_fit": "telic", "bound": ["他"]}, "bound 里写名字"),
])
def test_misplaced_companions_are_refused(store, kwargs, needle):
    out = run(grow_mod.dispatch(kind="event", items=[ITEM], **kwargs))
    assert needle in out
    assert not list(store.rglob("*.md"))


def test_trace_turns_status_want_away_and_sets_telic(store):
    bid = run(rt.bucket_mgr.create("She likes the blue mug.", tags=["t"]))
    out = run(trace_core(bucket_id=bid, status="want"))
    assert "direction_of_fit" in out
    out = run(trace_core(bucket_id=bid, direction_of_fit="telic", bound=["我"], when="10d"))
    assert "已修改" in out, out
    meta = _only_meta(store)
    assert (meta["direction_of_fit"], meta["bound"], meta["when"]) == ("telic", ["DT"], "10d")
    out = run(trace_core(bucket_id=bid, status="resolved"))
    assert _only_meta(store)["status"] == "resolved"
