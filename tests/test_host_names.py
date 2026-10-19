"""
GET /api/v2/names — the two names a host labels the lines it hands over with.

The setting page's 称呼 lives in Loci's config. A gateway started without
LOCI_OWNER_NAME / LOCI_AI_NAME asks here, so the lines Loci later shows the model say who
spoke instead of 「用户」 / 「AI」. It is a hook route: behind the host key once the panel is
locked, like the rest of /api/v2.
"""
import asyncio
import json


def _route(monkeypatch, locked=True):
    from starlette.requests import Request
    import utils
    import web
    from web import loci as Wb
    from web import panel_auth as PA

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setattr(PA, "gate_needed", lambda: locked)
    monkeypatch.setattr(PA, "has_session", lambda r: False)
    monkeypatch.setattr(PA, "hook_token", lambda: "s3cret")
    monkeypatch.setattr(utils, "_persisted_names", lambda: {"owner_name": "Ada", "ai_name": "Lin"})

    def call(key=None):
        headers = [(b"x-loci-hook-token", key.encode())] if key else []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        req = Request({"type": "http", "method": "GET", "path": "/api/v2/names",
                       "headers": headers, "query_string": b""}, receive)
        return asyncio.run(routes[("GET", "/api/v2/names")](req))
    return call


def test_names_are_served_behind_the_hook_key(monkeypatch):
    call = _route(monkeypatch)
    assert call().status_code == 401
    out = call(key="s3cret")
    assert out.status_code == 200
    assert json.loads(out.body) == {"owner": "Ada", "ai": "Lin"}


def test_names_is_a_hook_route():
    from web import panel_auth as PA
    assert PA.is_hook("/api/v2/names")
