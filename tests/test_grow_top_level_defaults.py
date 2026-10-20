# -*- coding: utf-8 -*-
"""
tests/test_grow_top_level_defaults.py — grow's top-level `when` and `room` reach the items.

The tool face documents grow(kind="event", when="2026-09-01", items=[...]): a top-level
`when` is the default for every item without its own. A top-level `room` is the same: a
model that writes room once above its events means it for each of them, and refusing
that as "items[0]: room 必填" made it write the whole call again.
"""

import asyncio

import pytest

import tools.grow as grow_mod
from core import runtime as rt


class _Engine:
    async def ensure_started(self):
        pass


@pytest.fixture
def captured(monkeypatch):
    seen: list[list] = []

    async def fake_grow_event(items, **kw):
        seen.append(items)
        return "ok"

    async def no_sweep():
        pass

    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(grow_mod, "grow_event", fake_grow_event)
    monkeypatch.setattr(grow_mod, "backfill_sweep", no_sweep)
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    return seen


def test_a_top_level_when_fills_every_item_without_one(captured):
    asyncio.run(grow_mod.dispatch(
        kind="event", when="2026-09-01",
        items=[{"room": "EVENT/SELF", "text": "Finish her gift."},
               {"room": "EVENT/SELF", "text": "Wrap it."}]))
    assert [i["when"] for i in captured[0]] == ["2026-09-01", "2026-09-01"]


def test_an_item_keeps_its_own_when(captured):
    asyncio.run(grow_mod.dispatch(
        kind="event", when="2026-09-01",
        items=[{"room": "EVENT/SELF", "text": "a", "when": "2026-08-15"},
               {"room": "EVENT/SELF", "text": "b"}]))
    assert [i["when"] for i in captured[0]] == ["2026-08-15", "2026-09-01"]


def test_no_top_level_when_leaves_items_alone(captured):
    items = [{"room": "EVENT/SELF", "text": "a"}]
    asyncio.run(grow_mod.dispatch(kind="event", items=items))
    assert "when" not in captured[0][0]


def test_a_top_level_room_fills_items_without_one_and_an_items_own_room_stands(captured):
    asyncio.run(grow_mod.dispatch(
        kind="event", room="EVENT/SELF",
        items=[{"text": "a"}, {"room": "EVENT/WORLD", "text": "b"}, {"room": "", "text": "c"}]))
    assert [i["room"] for i in captured[0]] == ["EVENT/SELF", "EVENT/WORLD", "EVENT/SELF"]


def test_no_top_level_room_leaves_a_missing_room_missing(captured):
    asyncio.run(grow_mod.dispatch(kind="event", items=[{"text": "a"}]))
    assert "room" not in captured[0][0]
