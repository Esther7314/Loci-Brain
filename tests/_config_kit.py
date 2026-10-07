# -*- coding: utf-8 -*-
"""
tests/_config_kit.py — web/config_api's routes on a throwaway config.yaml, called in-process

What the settings tests share: a config.yaml in a temporary folder (LOCI_CONFIG_PATH), the
running config swapped for a dict the test holds, the panel gate open, and the routes as
config_api.register adds them, called with a same-origin JSON request built by hand.
"""

import asyncio
import json

from starlette.requests import Request


def config_world(tmp_path, monkeypatch, yaml_text: str, config: dict) -> dict:
    """{"call": call(method, path, payload=None) -> (status, json), "config", "path"}."""
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml_text, encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = {"buckets_dir": str(tmp_path), "transport": "stdio", **config}
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

    return {"call": call, "config": config, "path": config_path}
