# -*- coding: utf-8 -*-
"""
tests/test_config_api_awake_days.py — the panel can set the three awake days breath reads.

`surfacing.awake_recent_days` / `awake_date_days` / `awake_cue_days` are what
core/profile.breath_settings reads. POST /api/config takes them like the neighbouring
surfacing numbers: an integer clamped into range (0 turns that reason off), a value that is
not a number skipped; the clamped value is what runs and what is written to config.yaml.
GET /api/config gives back the values breath runs on.
"""

import asyncio
import json

import pytest
import yaml
from starlette.requests import Request

from core.profile import breath_settings


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    config_path = tmp_path / "config.yaml"
    config_path.write_text("surfacing:\n  breath_max_results: 20\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = {"buckets_dir": str(tmp_path), "transport": "stdio",
              "surfacing": {"breath_max_results": 20}}
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)

    found = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                found[(methods[0], path)] = fn
                return fn
            return keep
    CA.register(_Mcp())

    def call(method, path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def go():
            req = Request({"type": "http", "method": method, "path": path,
                           "query_string": b"",
                           "headers": [(b"host", b"127.0.0.1:8000"),
                                       (b"origin", b"http://127.0.0.1:8000"),
                                       (b"content-type", b"application/json")]}, receive)
            resp = await found[(method, path)](req)
            return resp.status_code, json.loads(resp.body)
        return asyncio.run(go())

    return {"call": call, "config": config, "config_path": config_path}


def test_get_gives_the_defaults_breath_runs_on(world):
    status, out = world["call"]("GET", "/api/config")
    assert status == 200
    sf = out["surfacing"]
    defaults = breath_settings({})
    assert (sf["awake_recent_days"], sf["awake_date_days"], sf["awake_cue_days"]) == (
        defaults.recent_days, defaults.date_days, defaults.cue_days)


def test_the_three_are_set_live_and_persisted(world):
    status, out = world["call"]("POST", "/api/config", {"persist": True, "surfacing": {
        "awake_recent_days": 5, "awake_date_days": "14", "awake_cue_days": 2}})
    assert status == 200, out
    for key in ("awake_recent_days", "awake_date_days", "awake_cue_days"):
        assert f"surfacing.{key}" in out["updated"]

    s = breath_settings(world["config"])
    assert (s.recent_days, s.date_days, s.cue_days) == (5, 14, 2)

    saved = yaml.safe_load(world["config_path"].read_text(encoding="utf-8"))
    assert saved["surfacing"]["awake_recent_days"] == 5
    assert saved["surfacing"]["awake_date_days"] == 14
    assert saved["surfacing"]["awake_cue_days"] == 2
    assert saved["surfacing"]["breath_max_results"] == 20

    status, out = world["call"]("GET", "/api/config")
    assert out["surfacing"]["awake_date_days"] == 14


def test_out_of_range_is_clamped_and_the_clamped_value_is_persisted(world):
    status, out = world["call"]("POST", "/api/config", {"persist": True, "surfacing": {
        "awake_recent_days": -3, "awake_cue_days": 100000, "breath_max_results": 999}})
    assert status == 200, out
    sf = world["config"]["surfacing"]
    assert sf["awake_recent_days"] == 0
    assert sf["awake_cue_days"] == 365
    assert sf["breath_max_results"] == 50
    saved = yaml.safe_load(world["config_path"].read_text(encoding="utf-8"))["surfacing"]
    assert (saved["awake_recent_days"], saved["awake_cue_days"],
            saved["breath_max_results"]) == (0, 365, 50)


def test_a_value_that_is_not_a_number_is_skipped(world):
    world["config"]["surfacing"]["awake_date_days"] = 21
    status, out = world["call"]("POST", "/api/config", {"surfacing": {
        "awake_date_days": "soon", "awake_cue_days": None, "awake_recent_days": 4}})
    assert status == 200, out
    assert "surfacing.awake_date_days" not in out["updated"]
    assert "surfacing.awake_cue_days" not in out["updated"]
    assert world["config"]["surfacing"]["awake_date_days"] == 21
    assert "awake_cue_days" not in world["config"]["surfacing"]
    assert world["config"]["surfacing"]["awake_recent_days"] == 4
