# -*- coding: utf-8 -*-
"""
tests/test_config_api_hold_review_days.py — 阈值 · 条子默认挂几天, set from the panel.

`surfacing.hold_review_days` is what core/_holds.review_days reads when a hold set aside
with no date is written (the day breath asks whether it is still aside; 0 = never asks).
GET /api/config gives the value it runs on; POST takes it like the neighbouring surfacing
numbers: clamped into 0..365, a word skipped, applied live and persisted on request.
"""

import pytest
import yaml

from core import _holds as H

from _config_kit import config_world


@pytest.fixture
def world(tmp_path, monkeypatch):
    return config_world(tmp_path, monkeypatch, "surfacing:\n  breath_max_results: 20\n",
                        {"surfacing": {"breath_max_results": 20}})


def test_get_gives_the_default_holds_run_on(world):
    status, out = world["call"]("GET", "/api/config")
    assert status == 200
    assert out["surfacing"]["hold_review_days"] == H.DEFAULT_REVIEW_DAYS == 7


def test_it_is_set_live_and_persisted(world):
    status, out = world["call"]("POST", "/api/config",
                                {"persist": True, "surfacing": {"hold_review_days": "3"}})
    assert status == 200, out
    assert "surfacing.hold_review_days" in out["updated"]
    assert H.review_days(world["config"]) == 3
    saved = yaml.safe_load(world["path"].read_text(encoding="utf-8"))["surfacing"]
    assert saved == {"breath_max_results": 20, "hold_review_days": 3}
    assert world["call"]("GET", "/api/config")[1]["surfacing"]["hold_review_days"] == 3


def test_it_is_clamped_and_a_word_is_skipped(world):
    world["call"]("POST", "/api/config", {"surfacing": {"hold_review_days": 9999}})
    assert H.review_days(world["config"]) == 365
    world["call"]("POST", "/api/config", {"surfacing": {"hold_review_days": -2}})
    assert H.review_days(world["config"]) == 0, "0 = never ask"
    status, out = world["call"]("POST", "/api/config", {"surfacing": {"hold_review_days": "soon"}})
    assert status == 200 and "surfacing.hold_review_days" not in out["updated"]
    assert H.review_days(world["config"]) == 0
