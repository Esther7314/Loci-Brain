# -*- coding: utf-8 -*-
"""
tests/test_grow_needs_kind.py — grow without a kind is refused, and the batch ceilings
guard the event path.

The kind-less items path merged new text into existing entries and fed the old plan's
auto-closing; it is gone (7, 09-28). The item-count and batch-size ceilings used to sit
only on that path, so the path everything really goes through had none.
"""

import asyncio

import pytest

import tools.grow as grow_mod
from tools import _common as C
from tools import _runtime as rt


class _Engine:
    async def ensure_started(self):
        pass


@pytest.fixture
def no_store(monkeypatch):
    calls = []

    async def fake_grow_event(items, **kw):
        calls.append(items)
        return "stored"

    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    monkeypatch.setattr(grow_mod, "grow_event", fake_grow_event)
    return calls


ITEM = {"room": "EVENT/SELF", "text": "She fixed the kettle.", "v": 0.6, "a": 0.4}


def test_items_without_a_kind_are_refused_with_what_to_write(no_store):
    out = asyncio.run(grow_mod.dispatch(items=[ITEM]))
    assert 'kind="event"' in out and 'kind="mind"' in out
    assert no_store == []


def test_the_item_count_ceiling_applies_to_events(no_store, monkeypatch):
    monkeypatch.setattr(C, "max_grow_items", lambda: 2)
    out = asyncio.run(grow_mod.dispatch(kind="event", items=[ITEM, ITEM, ITEM]))
    assert "过多" in out
    assert no_store == []
    assert asyncio.run(grow_mod.dispatch(kind="event", items=[ITEM, ITEM])) == "stored"
