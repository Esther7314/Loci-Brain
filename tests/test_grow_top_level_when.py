# -*- coding: utf-8 -*-
"""
tests/test_grow_top_level_when.py — grow's top-level `when` reaches the items.

The tool face documents grow(kind="event", when="2026-09-01", items=[...]). dispatch()
accepted that `when` and never passed it on, so the deadline in the documented example
was silently lost; only items[i].when was stored.
"""

import asyncio

import pytest

import tools.grow as grow_mod
from tools import _runtime as rt


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
