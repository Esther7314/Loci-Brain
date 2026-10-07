# -*- coding: utf-8 -*-
"""
tests/test_config_api_same_origin.py — the engine-settings POSTs refuse another origin.

POST /api/config, /api/test/dehydration, /api/test/embedding and /api/models read their
body through `_guards._write_body`, like every other panel write: a page on another port
of the same machine (same site, so the browser still sends the panel cookie) is refused
with 403 before anything changes or any provider is called; a request without Origin is
refused; a body that is not `application/json` is a 400; the panel's own same-origin JSON
request goes through.
"""

import asyncio
import json

import pytest
from starlette.requests import Request

HOST = "127.0.0.1:8000"
SAME = "http://127.0.0.1:8000"
OTHER_PORT = "http://127.0.0.1:9999"


class _NoCalls:
    """Stands in for httpx.AsyncClient: any provider call fails the test."""

    def __init__(self, *a, **kw):
        raise AssertionError("a refused request must not reach the provider")


@pytest.fixture
def routes(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    config_path = tmp_path / "config.yaml"
    config_path.write_text("merge_threshold: 75\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = {"buckets_dir": str(tmp_path), "transport": "stdio", "merge_threshold": 75,
              "dehydration": {"api_key": "sk-test-0000", "base_url": "http://127.0.0.1:9/v1",
                              "model": "m"}}
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(sh, "embedding_engine", None)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(CA.httpx, "AsyncClient", _NoCalls)
    import httpx
    monkeypatch.setattr(httpx, "AsyncClient", _NoCalls)

    found = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                found[(methods[0], path)] = fn
                return fn
            return keep
    CA.register(_Mcp())
    return {"routes": found, "config": config}


def _post(routes, path, payload, origin=SAME, content_type="application/json"):
    raw = json.dumps(payload).encode()
    headers = [(b"host", HOST.encode())]
    if origin is not None:
        headers.append((b"origin", origin.encode()))
    if content_type is not None:
        headers.append((b"content-type", content_type.encode()))

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}

    async def go():
        req = Request({"type": "http", "method": "POST", "path": path, "query_string": b"",
                       "headers": headers}, receive)
        resp = await routes["routes"][("POST", path)](req)
        return resp.status_code, json.loads(resp.body)
    return asyncio.run(go())


GUARDED = ["/api/config", "/api/test/dehydration", "/api/test/embedding", "/api/models"]


@pytest.mark.parametrize("path", GUARDED)
def test_another_port_on_the_same_machine_is_refused(routes, path):
    status, out = _post(routes, path, {"merge_threshold": 10, "api_key": "x"},
                        origin=OTHER_PORT)
    assert status == 403
    assert "Origin" in out["error"]
    assert routes["config"]["merge_threshold"] == 75


@pytest.mark.parametrize("path", GUARDED)
def test_a_post_without_origin_is_refused(routes, path):
    status, _ = _post(routes, path, {"merge_threshold": 10}, origin=None)
    assert status == 403
    assert routes["config"]["merge_threshold"] == 75


@pytest.mark.parametrize("path", GUARDED)
def test_a_text_plain_body_is_refused(routes, path):
    status, out = _post(routes, path, {"merge_threshold": 10}, content_type="text/plain")
    assert status == 400
    assert "application/json" in out["error"]
    assert routes["config"]["merge_threshold"] == 75


def test_same_origin_config_post_is_applied(routes):
    status, out = _post(routes, "/api/config", {"merge_threshold": 80})
    assert status == 200, out
    assert out["ok"] is True
    assert "merge_threshold" in out["updated"]
    assert routes["config"]["merge_threshold"] == 80


def test_same_origin_tests_and_models_reach_their_route(routes):
    # Past the guard, each answers from its own logic (no provider is called here).
    status, out = _post(routes, "/api/test/embedding", {})
    assert status == 200 and out["ok"] is False and "standby" in out["error"]

    routes["config"]["dehydration"]["api_key"] = ""
    status, out = _post(routes, "/api/test/dehydration", {})
    assert status == 400 and out["error"] == "未设置 API Key"

    status, out = _post(routes, "/api/models", {"api_key": "", "base_url": ""})
    assert status == 400 and "api_key" in out["error"]
