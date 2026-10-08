# -*- coding: utf-8 -*-
"""
tests/test_breath_recent_window.py — how many days breath's recent block covers

`surfacing.breath_recent_days` (core/profile.breath_settings: 3 unless set, kept within
1–30) is the window of recall's overview in breath's recent block: the overview runs over
that many days and the title names them (近七天). The breath object carries the window
(`recent.days`); the copy kept for the panel keeps it, and the panel renders the last
breath's card and title over the window that breath was handed out with, not today's
setting. A copy that does not say was handed out over three days.

The window is not `awake_recent_days` (written this recently counts as awake); that one
names its own window in surface's words (「近五天写的」).
"""

import json
from datetime import date, timedelta

import pytest

from _panel_kit import make_store, routes, run
from core import _when as W
from core import breath_snapshot as S
from core.profile import (RECENT, BreathSettings, awake_words, breath_settings, days_words,
                          near_days)
from tools.breath import awaken as A


# ── the words ───────────────────────────────────────────────────────────────

def test_days_are_said_the_way_the_text_around_them_reads():
    said = {n: days_words(n) for n in (1, 2, 3, 7, 10, 14, 20, 21, 30, 45)}
    assert said == {1: "一", 2: "两", 3: "三", 7: "七", 10: "十", 14: "十四", 20: "二十",
                    21: "二十一", 30: "三十", 45: " 45 "}
    assert near_days(3) == "近三天" and near_days(7) == "近七天" and near_days(45) == "近 45 天"
    assert A.recent_title(14) == "近十四天"


# ── the setting ─────────────────────────────────────────────────────────────

def test_the_setting_is_three_unless_set_and_kept_within_one_to_thirty():
    assert BreathSettings().recent_window_days == 3
    assert breath_settings({}).recent_window_days == 3
    days = lambda v: breath_settings({"surfacing": {"breath_recent_days": v}}).recent_window_days
    assert (days(7), days("14"), days(0), days(-4), days(99), days("soon"), days(None)) == (
        7, 14, 1, 1, 30, 3, 3)


def test_the_window_is_not_the_awake_days():
    s = breath_settings({"surfacing": {"awake_recent_days": 5}})
    assert (s.recent_days, s.recent_window_days) == (5, 3)
    s = breath_settings({"surfacing": {"breath_recent_days": 9}})
    assert (s.recent_days, s.recent_window_days) == (3, 9)


def test_surface_names_the_awake_window_not_breaths():
    today = date(2026, 10, 14)
    assert awake_words(RECENT, {}, today) == "近三天写的"
    assert awake_words(RECENT, {}, today, settings=BreathSettings(recent_days=5)) == "近五天写的"
    assert awake_words(RECENT, {}, today,
                       settings=BreathSettings(recent_window_days=9)) == "近三天写的"


# ── breath ──────────────────────────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    return make_store(tmp_path, monkeypatch)


SECRET = "the body nobody should find in the copy"


def _ago(days: int) -> str:
    return (W.now() - timedelta(days=days)).strftime("%Y-%m-%d")


def _seed(store):
    """One entry from five days ago, one from today."""
    async def go():
        old = await store.create(f"Planted the bulbs. {SECRET}", room="EVENT/SELF",
                                 name="planted bulbs", when=_ago(5))
        new = await store.create(f"Baked bread. {SECRET}", room="EVENT/SELF", name="baked bread")
        return old, new
    return run(go())


def _set_days(store, days):
    from core import runtime as rt
    rt.config.setdefault("surfacing", {})["breath_recent_days"] = days


def test_breath_covers_three_days_unless_set(store):
    old, new = _seed(store)
    b = run(A.build_breath())
    assert b["recent"]["days"] == 3
    assert [it["id"] for it in b["recent"]["items"]] == [new]
    text = A.render_breath(b)
    assert "═══ 近三天 ═══" in text


def test_breath_covers_the_set_window_and_its_title_says_so(store, monkeypatch):
    old, new = _seed(store)
    _set_days(store, 7)
    asked = []
    real = A.recall_text_and_data

    async def spy(**kw):
        asked.append(kw["when"])
        return await real(**kw)
    monkeypatch.setattr(A, "recall_text_and_data", spy)
    b = run(A.build_breath())
    assert asked == ["7d"]
    assert b["recent"]["days"] == 7
    assert {it["id"] for it in b["recent"]["items"]} == {old, new}
    text = A.render_breath(b)
    assert "═══ 近七天 ═══" in text and "近三天" not in text
    assert "planted bulbs" in text
    # The JSON skin carries the window, so the text made from it says the same.
    assert A.render_breath(json.loads(json.dumps(b, ensure_ascii=False))) == text


def test_an_empty_window_says_its_own_days(store):
    _set_days(store, 14)
    text = A.render_breath(run(A.build_breath()))
    assert "═══ 近十四天 ═══\n（这十四天没存东西）" in text


# ── the copy kept for the panel ─────────────────────────────────────────────

def test_the_copy_keeps_its_window_and_the_page_renders_with_it(store, tmp_path, monkeypatch):
    # Criterion: the page shows the card the breath handed out — over its seven days —
    # after the setting went back to three.
    old, new = _seed(store)
    _set_days(store, 7)
    b = run(A.build_breath())
    A.handed_out(b, A.render_breath(b))
    assert S.load(str(tmp_path))["breath"]["recent"]["days"] == 7
    _set_days(store, 3)
    recent = routes(monkeypatch)("GET", "/api/loci/breath/last").json["breath"]["recent"]
    assert (recent["days"], recent["title"]) == (7, "近七天")
    assert recent["text"] == b["recent"]["text"]
    assert {it["id"] for it in recent["items"]} == {old, new}
    assert all(it["in_card"] for it in recent["items"])


def test_a_copy_that_does_not_say_its_window_was_three_days(store, tmp_path, monkeypatch):
    _seed(store)
    run(A.surface_awaken())
    path = tmp_path / "_state" / S.FILE
    data = json.loads(path.read_text(encoding="utf-8"))
    for entry in data["hosts"].values():
        entry["breath"]["recent"].pop("days")
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    _set_days(store, 10)
    recent = routes(monkeypatch)("GET", "/api/loci/breath/last").json["breath"]["recent"]
    assert (recent["days"], recent["title"]) == (3, "近三天")


def test_recent_days_reads_only_a_window_of_a_day_or_more():
    for breath in ({}, {"recent": {}}, {"recent": {"days": None}}, {"recent": {"days": "x"}},
                   {"recent": {"days": 0}}, {"recent": {"days": True}}, None):
        assert S.recent_days(breath) == 3
    assert S.recent_days({"recent": {"days": 12}}) == 12
