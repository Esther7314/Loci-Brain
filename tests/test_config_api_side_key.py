# -*- coding: utf-8 -*-
"""
tests/test_config_api_side_key.py — a side-model key saved from the panel survives a restart.

POST /api/config with `persist: true` writes `dehydration.api_key` to config.yaml, where
keys live; a restart reads it back through utils.load_config. An empty key keeps the saved
one. GET /api/config shows it masked only, and a backup of the library leaves config.yaml
out (core/schema), so the key does not travel with it.
"""

import asyncio
import json
import zipfile

import pytest
import yaml
from starlette.requests import Request

FAKE_KEY = "test-side-model-key-0123456789"


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    for name in ("LOCI_COMPRESS_API_KEY", "LOCI_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("dehydration:\n  model: side-model\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = {"buckets_dir": str(tmp_path), "transport": "stdio",
              "dehydration": {"model": "side-model"}}
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)

    class _Dehydrator:
        model = "side-model"
        base_url = ""
        max_tokens = 1024
        temperature = 0.1
        timeout_seconds = 120.0
        api_format = "anthropic"
        api_key = ""
        api_available = False
        client = None
    monkeypatch.setattr(sh, "dehydrator", _Dehydrator())

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
    return {"call": call, "path": config_path, "dir": tmp_path}


def _saved(world) -> dict:
    return yaml.safe_load(world["path"].read_text(encoding="utf-8")) or {}


def test_the_key_is_written_and_read_back_after_a_restart(world):
    status, out = world["call"]("POST", "/api/config",
                                {"persist": True, "dehydration": {"api_key": FAKE_KEY}})
    assert status == 200, out
    assert _saved(world)["dehydration"]["api_key"] == FAKE_KEY

    from utils import load_config
    assert load_config(str(world["path"]))["dehydration"]["api_key"] == FAKE_KEY


def test_an_empty_key_keeps_the_saved_one(world):
    world["call"]("POST", "/api/config", {"persist": True, "dehydration": {"api_key": FAKE_KEY}})
    world["call"]("POST", "/api/config",
                  {"persist": True, "dehydration": {"api_key": "", "model": "other-model"}})
    saved = _saved(world)["dehydration"]
    assert saved["api_key"] == FAKE_KEY and saved["model"] == "other-model"


def test_get_masks_it_and_a_backup_leaves_it_out(world):
    world["call"]("POST", "/api/config", {"persist": True, "dehydration": {"api_key": FAKE_KEY}})
    status, out = world["call"]("GET", "/api/config")
    assert status == 200 and FAKE_KEY not in json.dumps(out, ensure_ascii=False)

    (world["dir"] / "a_memory.md").write_text("---\nid: a\n---\nhello\n", encoding="utf-8")
    from core import schema as S
    with zipfile.ZipFile(S.backup(world["dir"], "keytest")) as zf:
        assert not any(n.endswith("config.yaml") for n in zf.namelist())
        assert all(FAKE_KEY.encode() not in zf.read(n) for n in zf.namelist())
