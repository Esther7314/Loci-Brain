# -*- coding: utf-8 -*-
"""
tests/test_host_table_lock.py — a `hosts:` table locks the doors it opens.

With a table, nobody is anybody without a credential: no caller is taken for the legacy
host because it presented nothing, a request carrying a host's credential is never the
panel, and the panel itself works only once it is locked. A host with a ceiling is let in
only when the panel is locked and MCP auth is on — otherwise its credential is refused, the
log says why and the setup screen shows it red. The export answers only its own page. With
no table, nothing changes.
"""

import asyncio
import json

import pytest
from starlette.requests import Request
from starlette.responses import JSONResponse

import web
from core import scope as SC
from web import _shared as sh
from web import panel_auth as PA

TABLE = {"hosts": {"legacy": {"token_env": "T_L", "scope_mode": "open"},
                   "bot": {"token_env": "T_B", "max_grant": [{"system": "telegram"}]}}}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def deploy(monkeypatch):
    """Set the deployment: its config, whether a password is set, whether a browser holds
    a session."""
    monkeypatch.setenv("T_L", "life")
    monkeypatch.setenv("T_B", "bot-key")
    monkeypatch.delenv("LOCI_HOOK_TOKEN", raising=False)
    monkeypatch.setattr(PA, "_lock_logged", {"message": "", "at": 0.0})

    def set_(config, *, password=False, session=False):
        monkeypatch.setattr(sh, "config", dict(config))
        monkeypatch.setattr(sh, "_load_password_hash", lambda: "h" if password else None)
        monkeypatch.setattr(PA, "has_session", lambda r: session)
    return set_


def _routes():
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    gated = web._Gated(_Mcp())

    async def panel_route(request):
        return JSONResponse({"panel": True})

    async def hook_route(request):
        req = request.state.loci_request
        return JSONResponse({"host": req.host.name if req.host else None,
                             "open": req.whole_library})
    gated.custom_route("/api/loci/export", methods=["GET"])(panel_route)
    gated.custom_route("/api/v2/breath", methods=["GET"])(hook_route)
    return routes


def _call(path, headers=()):
    routes = _routes()

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    req = Request({"type": "http", "method": "GET", "path": path, "query_string": b"",
                   "headers": [(k.lower().encode(), v.encode()) for k, v in headers]}, receive)
    resp = run(routes[("GET", path)](req))
    return resp.status_code, json.loads(resp.body)


# ───────────────────────── the panel's own routes ─────────────────────────

def test_without_a_table_the_panel_is_as_it_was(deploy):
    deploy({})
    assert _call("/api/loci/export") == (200, {"panel": True}), "unlocked: open, as today"
    assert _call("/api/loci/export", [("x-loci-hook-token", "x")])[0] == 200
    deploy({}, password=True)
    assert _call("/api/loci/export")[0] == 401
    deploy({}, password=True, session=True)
    assert _call("/api/loci/export")[0] == 200


def test_with_a_table_the_panel_stays_shut_until_it_is_locked(deploy):
    deploy(TABLE)
    status, out = _call("/api/loci/export")
    assert status == 401 and "口令" in out["error"], out
    deploy(TABLE, password=True)
    assert _call("/api/loci/export")[0] == 401
    deploy(TABLE, password=True, session=True)
    assert _call("/api/loci/export")[0] == 200


def test_a_host_credential_is_never_the_panel(deploy):
    deploy(TABLE, password=True, session=True)
    for headers in ([("x-loci-hook-token", "bot-key")], [("x-loci-hook-token", "stale")],
                    [("authorization", "Bearer bot-key")], [("loci-mcp-token", "life")]):
        status, out = _call("/api/loci/export", headers)
        assert status == 403 and "宿主" in out["error"], headers


# ───────────────────────── the hook routes ─────────────────────────

def test_with_a_table_a_caller_without_a_credential_is_nobody(deploy):
    deploy(TABLE)
    status, out = _call("/api/v2/breath")
    assert status == 401 and "hosts" in out["error"], out
    assert _call("/api/v2/breath", [("x-loci-hook-token", "life")]) == (
        200, {"host": "legacy", "open": True})


def test_a_host_with_a_ceiling_waits_for_the_lock_and_mcp_auth(deploy, caplog):
    deploy(TABLE)
    status, out = _call("/api/v2/breath", [("x-loci-hook-token", "bot-key")])
    assert status == 401 and "面板" in out["error"], out
    assert any("max_grant" in r.getMessage() for r in caplog.records), "said loudly"
    deploy({**TABLE, "mcp_require_auth": False}, password=True)
    status, out = _call("/api/v2/breath", [("x-loci-hook-token", "bot-key")])
    assert status == 401 and "MCP" in out["error"], out
    deploy(TABLE, password=True)
    assert _call("/api/v2/breath", [("x-loci-hook-token", "bot-key")])[1]["host"] == "bot"


def test_a_session_with_a_host_credential_is_the_host(deploy):
    deploy(TABLE, password=True, session=True)
    assert _call("/api/v2/breath", [("authorization", "Bearer bot-key")])[1]["host"] == "bot"
    assert _call("/api/v2/breath")[1] == {"host": None, "open": True}, "the panel itself"


def test_without_a_table_the_hook_routes_are_as_they_were(deploy):
    deploy({})
    assert _call("/api/v2/breath") == (200, {"host": "legacy", "open": True})
    assert _call("/api/v2/breath", [("x-loci-hook-token", "stale")])[0] == 200


def test_the_lock_problem_is_a_red_row_on_the_setup_screen(deploy, monkeypatch):
    from web import loci as Wb
    deploy(TABLE)
    hs = PA.hosts()
    assert hs.unsafe and "max_grant" in hs.unsafe
    rows = {r["key"]: r for r in run(Wb.build_setup())["rows"]}
    assert rows["hosts_lock"]["ok"] is False and not rows["hosts_lock"]["note"]
    deploy(TABLE, password=True)
    assert PA.hosts().unsafe == ""
    assert run(Wb.build_setup())["rows"] and all(
        r["ok"] for r in run(Wb.build_setup())["rows"] if r["key"] == "hosts_lock")
    deploy({})
    assert "hosts_lock" not in {r["key"] for r in run(Wb.build_setup())["rows"]}


# ───────────────────────── the MCP endpoint ─────────────────────────

def _asgi(hosts, *, auth_required=True, headers=()):
    from server_app import MCPAuthMiddleware
    seen = {}

    async def app(scope, receive, send):
        seen["host"] = scope.get("loci.host", "<not set>")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    mw = MCPAuthMiddleware(app, auth_required=auth_required, auth_mode="token",
                           token_validator=lambda t, resource: t == "mcp-key",
                           path_matcher=lambda p: p == "/mcp",
                           host_resolver=lambda: hosts)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/mcp", "scheme": "http",
             "headers": [(k.encode(), v.encode()) for k, v in headers],
             "client": ("127.0.0.1", 1)}
    run(mw(scope, receive, send))
    body = b"".join(m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    return sent[0]["status"], seen.get("host", "<not set>"), body.decode("utf-8")


def _table(**extra):
    return SC.load_hosts(TABLE, {"T_L": "life", "T_B": "bot-key"})


def test_mcp_with_auth_off_and_a_table_takes_nobody_for_legacy():
    hosts = _table()
    status, host, body = _asgi(hosts, auth_required=False)
    assert status == 401 and host == "<not set>" and "hosts" in body
    assert _asgi(hosts, auth_required=False,
                 headers=[("x-loci-hook-token", "life")])[:2] == (200, "legacy")
    status, _host, body = _asgi(hosts, auth_required=False,
                                headers=[("x-loci-hook-token", "bot-key")])
    assert status == 401 and "MCP" in body, "a ceiling needs MCP auth on"
    # Auth on: the old MCP credential is still the owner's, and the bot is the bot.
    assert _asgi(hosts, headers=[("authorization", "Bearer mcp-key")])[:2] == (200, "legacy")
    assert _asgi(hosts, headers=[("x-loci-hook-token", "bot-key")])[:2] == (200, "bot")
    # The panel unlocked (the web layer says so on the table): the bot waits.
    hosts.unsafe = "面板没上锁"
    status, _host, body = _asgi(hosts, headers=[("x-loci-hook-token", "bot-key")])
    assert status == 401 and "面板没上锁" in body
    assert _asgi(hosts, headers=[("x-loci-hook-token", "life")])[:2] == (200, "legacy")


def test_mcp_without_a_table_is_as_it_was():
    implicit = SC.load_hosts({}, {}, legacy_token="life")
    assert _asgi(implicit, auth_required=False)[:2] == (200, "legacy")


# ───────────────────────── the export answers its own page only ─────────────────────────

def _guard(path, headers, method="GET"):
    from server_app import OriginCSRFGuardMiddleware
    reached = []

    async def app(scope, receive, send):
        reached.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    mw = OriginCSRFGuardMiddleware(app, mcp_path_matcher=lambda p: p == "/mcp")
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    scope = {"type": "http", "method": method, "path": path, "scheme": "http",
             "headers": [(k.encode(), v.encode()) for k, v in
                         [("host", "127.0.0.1:18001"), *headers]],
             "client": ("127.0.0.1", 1)}
    run(mw(scope, receive, send))
    return sent[0]["status"]


def test_the_export_is_refused_to_another_site():
    assert _guard("/api/loci/export", [("origin", "https://evil.example"),
                                       ("sec-fetch-site", "cross-site")]) == 403
    assert _guard("/api/loci/export", [("origin", "https://evil.example")]) == 403
    assert _guard("/api/loci/export", [("sec-fetch-site", "cross-site")]) == 403
    assert _guard("/api/loci/export", [("sec-fetch-site", "same-origin")]) == 200
    assert _guard("/api/loci/export", [("origin", "http://127.0.0.1:18001")]) == 200
    assert _guard("/api/loci/export", []) == 200, "a script on the machine, no browser"
    assert _guard("/api/loci/health", [("origin", "https://evil.example")]) == 200, \
        "other reads keep today's rule"
