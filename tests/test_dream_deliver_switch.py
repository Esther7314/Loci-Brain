# -*- coding: utf-8 -*-
"""
tests/test_dream_deliver_switch.py — dream's 高级设置 「提醒」: 醒来的时候递给他.

The switch is `deliver_on_wake` in config.yaml's `dream:` section, set through
/api/config `dream` like the rules beside it, on by default (a woven dream is handed to
the host's wake, as it always was). Off, /api/loci/poke hands the host no dream — so the
gateway delivers none and sends no wake signal after it — and the dream stays as it is
for him to fetch himself (/api/dream/current).
"""

import asyncio

import pytest
import yaml

from core import _dream as D
from core import _when as W
from core import runtime as rt
from web import loci_dream as L

from _config_kit import config_world


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = config_world(tmp_path, monkeypatch, "dream:\n  half_life_days: 7\n",
                     {"dream": {"half_life_days": 7}})
    monkeypatch.setattr(rt, "config", w["config"])
    monkeypatch.setattr(rt, "logger", _Log())

    async def no_muse():
        return {"worth_poking": False}
    monkeypatch.setattr(L, "build_muse_pending", no_muse)
    stamp = W.now().isoformat(timespec="seconds")
    D.save_record({"id": "d0000000feed", "织于": stamp, "起算点": stamp, "回想次数": 0,
                   "轮次": 0, "碎片": "走廊。钥匙。", "完整": "我站在一条很长的走廊里。",
                   "v": 0.4, "a": 0.6, "nightmare": False,
                   "素材": {"压在心头": [], "想不明白": [], "几个词": []}})
    return w


def _poked() -> list:
    return asyncio.run(L.build_poke())["dreams"]


def test_on_by_default_and_the_wake_is_handed_the_dream(world):
    assert D.DREAM_DEFAULTS["deliver_on_wake"] is True
    dream = world["call"]("GET", "/api/config")[1]["dream"]
    assert dream["deliver_on_wake"] is True and dream["defaults"]["deliver_on_wake"] is True
    assert [d["id"] for d in _poked()] == ["d0000000feed"]


def test_off_the_wake_is_handed_no_dream_and_he_can_still_fetch_it(world):
    status, out = world["call"]("POST", "/api/config",
                                {"persist": True, "dream": {"deliver_on_wake": False}})
    assert status == 200 and "dream.deliver_on_wake" in out["updated"], out
    assert world["call"]("GET", "/api/config")[1]["dream"]["deliver_on_wake"] is False
    saved = yaml.safe_load(world["path"].read_text(encoding="utf-8"))["dream"]
    assert saved["deliver_on_wake"] is False and saved["half_life_days"] == 7

    assert _poked() == []
    # Left as it is: fetched by him, it is there, whole.
    got = asyncio.run(D.current_dream(recall=False))
    assert got["id"] == "d0000000feed" and got["层"] == "完整"
    assert [r["id"] for r in D.load_dreams()] == ["d0000000feed"]

    world["call"]("POST", "/api/config", {"dream": {"deliver_on_wake": True}})
    assert [d["id"] for d in _poked()] == ["d0000000feed"]


def test_the_switch_reads_as_a_bool(world):
    assert D.dream_config({"dream": {"deliver_on_wake": "false"}})["deliver_on_wake"] is False
    assert D.dream_config({"dream": {"deliver_on_wake": "nonsense"}})["deliver_on_wake"] is True
    status, out = world["call"]("POST", "/api/config", {"dream": {"deliver_on_wake": "maybe"}})
    assert status == 400, out
    assert "deliver_on_wake" not in world["config"]["dream"]


def test_the_rules_reset_leaves_the_switch_alone_in_the_view(world):
    # 规矩's 恢复默认 posts the number defaults only (pages/dream.js); the view keeps the
    # switch's default beside them for the 提醒 block.
    dream = world["call"]("GET", "/api/config")[1]["dream"]
    numbers = {k: v for k, v in dream["defaults"].items() if k != "deliver_on_wake"}
    world["call"]("POST", "/api/config", {"dream": {"deliver_on_wake": False}})
    status, _ = world["call"]("POST", "/api/config", {"dream": numbers})
    assert status == 200
    assert world["call"]("GET", "/api/config")[1]["dream"]["deliver_on_wake"] is False
