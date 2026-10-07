# -*- coding: utf-8 -*-
"""
tests/test_config_api_dream.py — dream's 高级设置 「规矩」: the numbers the panel sets.

GET /api/config gives the `dream` rules as core/_dream.dream_config runs them, with their
defaults (DREAM_DEFAULTS) for 恢复默认. POST /api/config takes `dream` like `muse`: each
number clamped into its range and read as its default's type, a value that is not a number
skipped, applied live (dream_config reads the running config) and written to config.yaml
when persisted. A dream fades to its fragment before its one line, so a one-line limit
below its fragment limit refuses the request and changes nothing.
"""

import pytest
import yaml

from core import _dream as D

from _config_kit import config_world

BOARD = ("pressure_line", "dull_line", "per_day", "dream_cooldown_days",
         "fragment_minutes", "fragment_turns", "oneline_minutes", "oneline_turns")


@pytest.fixture
def world(tmp_path, monkeypatch):
    return config_world(tmp_path, monkeypatch, "dream:\n  half_life_days: 7\n",
                        {"dream": {"half_life_days": 7}})


def _saved(world) -> dict:
    return yaml.safe_load(world["path"].read_text(encoding="utf-8")) or {}


def test_get_gives_the_rules_and_their_defaults(world):
    status, out = world["call"]("GET", "/api/config")
    assert status == 200
    dream = out["dream"]
    for key in BOARD:
        assert dream[key] == D.DREAM_DEFAULTS[key]
        assert dream["defaults"][key] == D.DREAM_DEFAULTS[key]
    assert (dream["pressure_line"], dream["dull_line"], dream["per_day"]) == (0.65, 0.35, 1)
    assert "temperature" not in dream, "only the rules the page sets"


def test_the_rules_are_set_live_and_persisted(world):
    status, out = world["call"]("POST", "/api/config", {"persist": True, "dream": {
        "pressure_line": "0.8", "dull_line": 0.2, "per_day": 2, "dream_cooldown_days": 3,
        "fragment_minutes": 20, "fragment_turns": "10", "oneline_minutes": 90,
        "oneline_turns": 40}})
    assert status == 200, out
    for key in BOARD:
        assert f"dream.{key}" in out["updated"]
    live = D.dream_config(world["config"])
    assert (live["pressure_line"], live["dull_line"], live["per_day"]) == (0.8, 0.2, 2)
    assert (live["fragment_minutes"], live["fragment_turns"]) == (20, 10)
    assert (live["oneline_minutes"], live["oneline_turns"]) == (90, 40)
    saved = _saved(world)["dream"]
    assert saved["half_life_days"] == 7, "the keys the page does not set are kept"
    assert saved["pressure_line"] == 0.8 and saved["fragment_turns"] == 10
    assert isinstance(saved["per_day"], int) and isinstance(saved["dull_line"], float)
    assert world["call"]("GET", "/api/config")[1]["dream"]["oneline_minutes"] == 90


def test_out_of_range_is_clamped_and_a_word_is_skipped(world):
    status, out = world["call"]("POST", "/api/config", {"dream": {
        "pressure_line": 3, "dull_line": -1, "per_day": 99, "dream_cooldown_days": 0,
        "fragment_minutes": "soon", "oneline_turns": True}})
    assert status == 200, out
    section = world["config"]["dream"]
    assert (section["pressure_line"], section["dull_line"]) == (1.0, 0.0)
    assert (section["per_day"], section["dream_cooldown_days"]) == (5, 1)
    assert "fragment_minutes" not in section and "dream.fragment_minutes" not in out["updated"]
    assert "oneline_turns" not in section, "a switch is not a number"


def test_the_one_line_cannot_come_before_the_fragment(world):
    status, out = world["call"]("POST", "/api/config", {"persist": True, "dream": {
        "per_day": 3, "fragment_minutes": 50, "oneline_minutes": 40}})
    assert status == 400 and "先散成碎片" in out["error"]
    assert "per_day" not in world["config"]["dream"], "nothing in the request is applied"
    assert "per_day" not in _saved(world)["dream"]
    # Against the running rules: a fragment limit past the default one-line limit refuses too.
    assert world["call"]("POST", "/api/config", {"dream": {"fragment_turns": 31}})[0] == 400
    assert world["call"]("POST", "/api/config", {"dream": {"fragment_turns": 30}})[0] == 200


def test_dream_must_be_an_object(world):
    status, out = world["call"]("POST", "/api/config", {"dream": [1]})
    assert status == 400 and "dream" in out["error"]
