# -*- coding: utf-8 -*-
"""
tests/_panel_kit.py — a real store and the panel's routes, called in-process

What the panel-page tests share: a BucketManager on a temporary folder wired into the
runtime and the web layer, and the routes as `web/loci.register` assembles them behind the
gate (`web._Gated`), called with a Starlette Request built by hand — a GET with a query, a
POST with a body and its Origin / Content-Type headers.
"""

import asyncio
import json

from core import runtime as rt
from core.bucket_manager import BucketManager


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = exception = _n


def run(coro):
    return asyncio.run(coro)


def make_store(tmp_path, monkeypatch):
    from tools.grow import rooms_path
    from web import _shared as sh

    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    cfg = {"buckets_dir": str(tmp_path)}
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", cfg)
    monkeypatch.setattr(sh, "bucket_mgr", mgr)
    monkeypatch.setattr(sh, "config", cfg)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


class Reply:
    def __init__(self, response):
        self.status = response.status_code
        self.raw = response.body
        try:
            self.json = json.loads(response.body)
        except ValueError:
            self.json = None


def routes(monkeypatch, *, locked=False):
    """call(method, path, query=b"", body=None, origin=True, ctype="application/json",
    key=None) -> Reply."""
    from starlette.requests import Request
    import web
    from web import loci as Wb
    from web import panel_auth as PA

    table = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                table[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setattr(PA, "gate_needed", lambda: locked)
    monkeypatch.setattr(PA, "has_session", lambda r: False)
    monkeypatch.setattr(PA, "hook_token", lambda: "s3cret")

    def call(method, path, query=b"", body=None, origin=True, ctype="application/json",
             key=None):
        headers = [(b"host", b"testserver")]
        if origin:
            headers.append((b"origin", b"http://testserver"))
        if ctype:
            headers.append((b"content-type", ctype.encode()))
        if key:
            headers.append((b"x-loci-hook-token", key.encode()))
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
        if isinstance(query, str):
            query = query.encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "headers": headers,
                       "query_string": query}, receive)
        return Reply(asyncio.run(table[(method, path)](req)))
    return call
