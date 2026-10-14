# -*- coding: utf-8 -*-
"""
tests/test_slicer_own_endpoint.py — the slicer on an endpoint of its own
(`dehydration.slicer`, core/_slicer.own_slicer_settings / side_model).

  · with `dehydration.slicer` the slicer's call goes to that base_url with that model,
    its key read from the environment variable `api_key_env` names, and the backfill's
    endpoint, key and `slicer_model` are not used;
  · without it (absent or empty) the slicer rides the backfill's endpoint as before,
    `slicer_model` included;
  · the same settings reuse one client; a changed key builds a new one.

The OpenAI-compatible client is a stand-in: no socket is opened.
"""

import asyncio
import json
from types import SimpleNamespace

import pytest

from core import _slicer as SL
from core import dehydrator as D

SLICER_KEY = "slicer-door-key-7c1e"


def run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


class Endpoint:
    """Stands in for the OpenAI-compatible client: records which endpoint, key and
    request each call was made with, and answers with a fixed slice list."""

    def __init__(self):
        self.seen = []

    def client(self, api_key, base_url, timeout):
        seen = self.seen

        async def create(**kwargs):
            seen.append({"base_url": base_url, "key": api_key, "body": kwargs})
            message = SimpleNamespace(content='[{"from": 1, "to": 2, "gist": "吃面"}]')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))


@pytest.fixture
def endpoint(monkeypatch):
    fake = Endpoint()
    monkeypatch.setattr(D, "openai_client", fake.client)
    monkeypatch.setenv("SLICER_DOOR_KEY", SLICER_KEY)
    SL._OWN_SLICERS.clear()
    yield fake
    for d in SL._OWN_SLICERS.values():
        d.close()
    SL._OWN_SLICERS.clear()


def _config(tmp_path, slicer=None):
    dehy = {"api_format": "openai_compat", "base_url": "https://backfill.example/v1",
            "api_key": "sk-backfill", "model": "backfill-m", "slicer_model": "backfill-slicer"}
    if slicer is not None:
        dehy["slicer"] = slicer
    return {"buckets_dir": str(tmp_path), "dehydration": dehy}


OWN = {"api_format": "openai_compat", "base_url": "http://127.0.0.1:3010/api/loci/side/v1",
       "api_key_env": "SLICER_DOOR_KEY", "model": "sonnet", "timeout_seconds": 300}

LINES = [{"id": "m_1", "text": "今天吃了面"}, {"id": "m_2", "text": "面很好吃"}]


def test_the_slicer_goes_to_its_own_endpoint_with_its_own_key_and_model(tmp_path, endpoint):
    config = _config(tmp_path, OWN)
    backfill = D.Dehydrator(config)
    try:
        model = SL.side_model(backfill, config)
        slices = run(SL.slice_lines(LINES, model=model))
    finally:
        backfill.close()
    assert [(s.first, s.last, s.gist) for s in slices] == [("m_1", "m_2", "吃面")]
    assert model.model_name == "sonnet"
    got = endpoint.seen[0]
    assert got["base_url"] == "http://127.0.0.1:3010/api/loci/side/v1"
    assert got["key"] == SLICER_KEY
    assert got["body"]["model"] == "sonnet"
    assert "sk-backfill" not in json.dumps(endpoint.seen)


@pytest.mark.parametrize("slicer", [None, {}])
def test_without_its_own_endpoint_the_slicer_rides_the_backfills(tmp_path, endpoint, slicer):
    config = _config(tmp_path, slicer)
    backfill = D.Dehydrator(config)
    try:
        model = SL.side_model(backfill, config)
        run(SL.slice_lines(LINES, model=model))
    finally:
        backfill.close()
    got = endpoint.seen[0]
    assert got["base_url"] == "https://backfill.example/v1"
    assert got["key"] == "sk-backfill"
    assert got["body"]["model"] == "backfill-slicer"
    assert SL._OWN_SLICERS == {}


def test_one_client_per_setting_and_a_new_key_builds_a_new_one(tmp_path, endpoint, monkeypatch):
    config = _config(tmp_path, OWN)
    SL.side_model(None, config)
    SL.side_model(None, config)
    assert len(SL._OWN_SLICERS) == 1
    monkeypatch.setenv("SLICER_DOOR_KEY", "a-new-key")
    run(SL.side_model(None, config)("s", "u"))
    assert len(SL._OWN_SLICERS) == 2
    assert endpoint.seen[-1]["key"] == "a-new-key"
