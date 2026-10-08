# -*- coding: utf-8 -*-
"""
tests/test_panel_muse_settings.py — muse's 高级设置: the reminder the panel sets.

GET /api/config gives the `muse` reminder as it runs (醒来的时候提一句 · 攒够几团才提 ·
最老的放了几天才提) with its defaults; POST /api/config takes `muse` like the surfacing
numbers: clamped into range, a value that is not a number skipped, applied live and
written to config.yaml when persisted. With 醒来的时候提一句 off, the muse count the poke
reads (build_muse_pending) never says it is time.
"""

import asyncio
import json
from datetime import timedelta

import pytest
import yaml
from starlette.requests import Request

from core import _muse as M
from core import _when as W


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    config_path = tmp_path / "config.yaml"
    config_path.write_text("muse:\n  sim_line: 0.76\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = {"buckets_dir": str(tmp_path), "transport": "stdio", "muse": {"sim_line": 0.76}}
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


def test_get_gives_the_reminder_and_its_defaults(world):
    status, out = world["call"]("GET", "/api/config")
    assert status == 200
    muse = out["muse"]
    assert (muse["poke_on_wake"], muse["poke_min_clusters"], muse["poke_min_age_days"]) == (
        True, M.MUSE_DEFAULTS["poke_min_clusters"], M.MUSE_DEFAULTS["poke_min_age_days"])
    assert muse["defaults"] == {"poke_min_clusters": 2, "poke_min_age_days": 3,
                                "poke_on_wake": True}


def test_the_reminder_is_set_live_and_persisted(world):
    status, out = world["call"]("POST", "/api/config", {"persist": True, "muse": {
        "poke_on_wake": False, "poke_min_clusters": "4", "poke_min_age_days": 6}})
    assert status == 200, out
    for key in ("poke_on_wake", "poke_min_clusters", "poke_min_age_days"):
        assert f"muse.{key}" in out["updated"]
    live = M.muse_config(world["config"])
    assert (live["poke_on_wake"], live["poke_min_clusters"], live["poke_min_age_days"]) == (
        False, 4, 6)
    saved = yaml.safe_load(world["config_path"].read_text(encoding="utf-8"))["muse"]
    assert saved == {"sim_line": 0.76, "poke_on_wake": False, "poke_min_clusters": 4,
                     "poke_min_age_days": 6}
    assert world["call"]("GET", "/api/config")[1]["muse"]["poke_min_clusters"] == 4


def test_out_of_range_is_clamped_and_a_word_is_skipped(world):
    status, out = world["call"]("POST", "/api/config", {"muse": {
        "poke_min_clusters": 0, "poke_min_age_days": "soon"}})
    assert status == 200, out
    assert world["config"]["muse"]["poke_min_clusters"] == 1
    assert "poke_min_age_days" not in world["config"]["muse"]
    assert "muse.poke_min_age_days" not in out["updated"]
    assert world["call"]("POST", "/api/config", {"muse": [1]})[0] == 400


def test_with_the_switch_off_pending_never_says_it_is_time(world, monkeypatch):
    from web import loci_dream as L

    old = W.now() - timedelta(days=10)
    item = M.Item(id="a1c3e5f7b9d2", room="MIND/TRAITS", ts=None, created=old, v=0.6,
                  a=0.3, tags=[], text="")
    clusters = [M.Cluster(ids=[item.id], items=[item], shelf_v=0.6, shelf_a=0.3,
                          from_core=[], semantic_add=[]) for _ in range(3)]

    async def both_sides(force=False, scope=None):
        return clusters, 0, 0, {}, {}
    monkeypatch.setattr(M, "both_sides", both_sides)

    assert asyncio.run(L.build_muse_pending())["worth_poking"] is True
    world["call"]("POST", "/api/config", {"muse": {"poke_on_wake": False}})
    assert asyncio.run(L.build_muse_pending())["worth_poking"] is False


def _waiting(monkeypatch, n_clusters: int, n_fingers: int, days_old: int = 10) -> None:
    old = W.now() - timedelta(days=days_old)
    item = M.Item(id="a1c3e5f7b9d2", room="MIND/TRAITS", ts=None, created=old, v=0.6,
                  a=0.3, tags=[], text="")
    clusters = [M.Cluster(ids=[item.id], items=[item], shelf_v=0.6, shelf_a=0.3,
                          from_core=[], semantic_add=[]) for _ in range(n_clusters)]
    fingers = {"空白记账": [M.Finger(name="空白记账", ids=[item.id], items=[item], start=old,
                                    end=old) for _ in range(n_fingers)]}

    async def both_sides(force=False, scope=None):
        return clusters, 0, 0, fingers, {}
    monkeypatch.setattr(M, "both_sides", both_sides)


def test_days_and_thoughts_are_counted_together_against_the_threshold(world, monkeypatch):
    # Criterion: 「攒够几团才提」 counts 「日子和想法加在一起」 — one day group plus one
    # thought cluster meets the default of two, while either one alone does not.
    from web import loci_dream as L

    assert M.MUSE_DEFAULTS["poke_min_clusters"] == 2
    _waiting(monkeypatch, 1, 1)
    pending = asyncio.run(L.build_muse_pending())
    assert (pending["mind_clusters"], pending["gist_fingers"], pending["worth_poking"]) == (
        1, 1, True)
    # The poke reports the same count the threshold counted, so non-zero means it is time.
    assert asyncio.run(L.build_poke())["muse_pending"] == 2

    _waiting(monkeypatch, 0, 2)
    assert asyncio.run(L.build_muse_pending())["worth_poking"] is True
    assert asyncio.run(L.build_poke())["muse_pending"] == 2

    for clusters, fingers in ((1, 0), (0, 1)):
        _waiting(monkeypatch, clusters, fingers)
        assert asyncio.run(L.build_muse_pending())["worth_poking"] is False
        assert asyncio.run(L.build_poke())["muse_pending"] == 0

    # The age condition still holds alongside the sum.
    _waiting(monkeypatch, 1, 1, days_old=1)
    assert asyncio.run(L.build_muse_pending())["worth_poking"] is False
