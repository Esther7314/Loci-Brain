# -*- coding: utf-8 -*-
"""
tests/test_panel_revoke_grants.py — taking back every MCP OAuth grant from the panel, and
the Secure flag on the panel's login cookie.

POST /api/loci/auth/revoke-grants (web/loci_password) calls bridge/oauth's
revoke_all_mcp_grants, whose own contract is frozen in test_oauth_grant_contract.py. What
is asserted here is the door in front of it:

  · a logged-in session, always — locked panel or not, as for the security question;
  · same-origin, and a host's credential is refused like on every panel write;
  · it works: tokens on disk and in memory are gone, codes in flight too, and the
    owner's own panel session survives;
  · a revocation that cannot reach the disk says so and leaves the grants as they were.

The login cookie is Secure exactly when the browser came over https — directly, or via
a proxy in LOCI_TRUSTED_PROXY_CIDRS saying so — and never on a plain-http LAN login, where
a Secure cookie would never come back and the owner could not log in at all.

The routes are the real ones behind the real gate (web._Gated), on a real auth file and
synthetic grant state in tmp_path.
"""

import asyncio
import base64
import hashlib
import json
import time

import pytest
from starlette.requests import Request

PASSWORD = "first-password-1"
PATH = "/api/loci/auth/revoke-grants"
RESOURCE = "https://example.com/mcp"
VERIFIER = "a" * 43


@pytest.fixture
def world(tmp_path, monkeypatch):
    import web
    import bridge.oauth as oauth
    from web import _shared as sh
    from web import loci as Wl
    from web import panel_auth as PA

    monkeypatch.delenv("LOCI_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("LOCI_HOOK_TOKEN", raising=False)
    monkeypatch.delenv("LOCI_TRUSTED_PROXY_CIDRS", raising=False)
    config = {"buckets_dir": str(tmp_path), "panel_auth": True}
    monkeypatch.setattr(sh, "config", config)
    from collections import OrderedDict, deque
    monkeypatch.setattr(sh, "_login_failures", {})
    monkeypatch.setattr(sh, "_login_locked_until", {})
    monkeypatch.setattr(sh, "_login_source_lru", OrderedDict())
    monkeypatch.setattr(sh, "_login_global_attempts", deque())

    state = (oauth._oauth_clients, oauth._oauth_codes, oauth._mcp_tokens,
             oauth._mcp_token_resources, oauth._mcp_refresh_tokens)
    saved = [dict(d) for d in state]
    for d in state:
        d.clear()

    table = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                table[(methods[0], path)] = fn
                return fn
            return keep
    gated = web._Gated(_Mcp())
    PA.register(gated)
    Wl.register(gated)

    def call(method, path, body=None, cookie=None, headers=(), origin="http://testserver",
             scheme="http", peer="127.0.0.1"):
        hdrs = [(b"host", b"testserver"), (b"content-type", b"application/json")]
        if origin:
            hdrs.append((b"origin", origin.encode()))
        if cookie:
            hdrs.append((b"cookie", f"{PA._COOKIE}={cookie}".encode()))
        hdrs += [(k.encode(), v.encode()) for k, v in headers]
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "headers": hdrs,
                       "scheme": scheme, "server": ("testserver", 443 if scheme == "https" else 80),
                       "query_string": b"", "client": (peer, 50000)}, receive)
        resp = asyncio.run(table[(method, path)](req))
        return resp.status_code, json.loads(resp.body), resp.headers.get("set-cookie", "")

    def login(password=PASSWORD, **kw):
        status, out, set_cookie = call("POST", "/auth/login", {"password": password}, **kw)
        assert status == 200, out
        return set_cookie

    def cookie_value(set_cookie):
        return set_cookie.split(f"{PA._COOKIE}=", 1)[1].split(";", 1)[0]

    def issue_grant():
        generation = sh._credential_generation_snapshot()
        digest = hashlib.sha256(VERIFIER.encode()).digest()
        code_data = {"client_id": "client-1", "redirect_uri": "https://client.example/cb",
                     "code_challenge": base64.urlsafe_b64encode(digest).rstrip(b"=").decode(),
                     "resource": RESOURCE, "scope": "mcp", "expires": time.time() + 300}
        assert oauth._store_authorization_code("code-1", code_data, generation)
        pair = oauth._commit_authorization_code_exchange(
            "code-1", dict(oauth._oauth_codes["code-1"]), RESOURCE)
        assert pair is not None
        assert oauth._store_authorization_code("code-in-flight", dict(code_data),
                                               sh._credential_generation_snapshot())
        return pair

    def tokens_file():
        return json.loads((tmp_path / ".dashboard_mcp_tokens.json").read_text(encoding="utf-8"))

    assert sh._save_password_hash(PASSWORD)
    yield {"call": call, "login": login, "cookie_value": cookie_value, "config": config,
           "sh": sh, "oauth": oauth, "PA": PA, "issue_grant": issue_grant,
           "tokens_file": tokens_file}
    for d, snapshot in zip(state, saved):
        d.clear()
        d.update(snapshot)


def _still_granted(world, access, refresh):
    oauth = world["oauth"]
    return oauth._is_valid_mcp_token(access, RESOURCE) and refresh in oauth._mcp_refresh_tokens


# ───────────────────────── revoke-grants: the door ─────────────────────────

def test_a_locked_panel_wants_a_session_and_nothing_is_revoked(world):
    access, refresh = world["issue_grant"]()
    status, _out, _ = world["call"]("POST", PATH, {})
    assert status == 401
    assert _still_granted(world, access, refresh)


def test_an_unlocked_panel_still_wants_a_session(world):
    access, refresh = world["issue_grant"]()
    world["config"]["panel_auth"] = False
    status, out, _ = world["call"]("POST", PATH, {})
    assert status == 401 and out["error"] == "请先登录"
    assert _still_granted(world, access, refresh)


def test_another_origin_is_refused_even_with_a_session(world):
    access, refresh = world["issue_grant"]()
    cookie = world["cookie_value"](world["login"]())
    status, _out, _ = world["call"]("POST", PATH, {}, cookie=cookie,
                                    origin="http://evil.example")
    assert status == 403
    assert _still_granted(world, access, refresh)


def test_a_host_credential_is_refused_even_with_a_session(world, monkeypatch):
    access, refresh = world["issue_grant"]()
    cookie = world["cookie_value"](world["login"]())
    monkeypatch.setenv("T_LENTO", "host-key-0123456789")
    world["config"]["hosts"] = {"lento": {"token_env": "T_LENTO", "scope_mode": "open"}}
    status, _out, _ = world["call"]("POST", PATH, {}, cookie=cookie,
                                    headers=[("x-loci-hook-token", "host-key-0123456789")])
    assert status == 403
    assert _still_granted(world, access, refresh)


# ───────────────────────── revoke-grants: what it does ─────────────────────────

def test_with_a_session_every_grant_is_taken_back_and_the_panel_stays_logged_in(world):
    oauth = world["oauth"]
    access, refresh = world["issue_grant"]()
    cookie = world["cookie_value"](world["login"]())
    status, out, _ = world["call"]("POST", PATH, {}, cookie=cookie)
    assert status == 200 and out["ok"], out
    assert not oauth._is_valid_mcp_token(access, RESOURCE)
    assert refresh not in oauth._mcp_refresh_tokens
    assert not oauth._oauth_codes, "a code in flight must not turn into a token later"
    assert world["tokens_file"]() == {"access_tokens": {}, "refresh_tokens": {}}
    # Grants are not the panel's session: the owner who pressed it is still logged in.
    assert world["call"]("POST", PATH, {}, cookie=cookie)[0] == 200


def test_a_revocation_that_cannot_reach_the_disk_is_not_claimed(world, monkeypatch):
    access, refresh = world["issue_grant"]()
    cookie = world["cookie_value"](world["login"]())
    on_disk = world["tokens_file"]()

    def boom(path, data):
        raise OSError("disk full")
    monkeypatch.setattr(world["sh"], "_atomic_write_private_json", boom)
    status, out, _ = world["call"]("POST", PATH, {}, cookie=cookie)
    assert status == 500 and "授权都还在" in out["error"]
    assert _still_granted(world, access, refresh)
    assert world["tokens_file"]() == on_disk


# ───────────────────────── the login cookie's Secure flag ─────────────────────────

def _is_secure(set_cookie):
    return "secure" in [part.strip().lower() for part in set_cookie.split(";")]


def test_a_plain_http_lan_login_gets_a_cookie_it_will_send_back(world):
    set_cookie = world["login"](peer="192.168.1.20")
    assert set_cookie and not _is_secure(set_cookie)
    assert "httponly" in set_cookie.lower() and "samesite=lax" in set_cookie.lower()


def test_an_https_public_url_does_not_make_the_lan_login_secure(world):
    world["config"]["deployment"] = {"public_url": "https://loci.example.com"}
    assert not _is_secure(world["login"](peer="192.168.1.20"))


def test_a_direct_https_login_is_secure(world):
    assert _is_secure(world["login"](scheme="https", peer="192.168.1.20"))


def test_a_trusted_proxy_saying_https_makes_it_secure(world):
    # cloudflared or a reverse proxy on the same machine: loopback is trusted by default.
    assert _is_secure(world["login"](peer="127.0.0.1",
                                     headers=[("x-forwarded-proto", "https")]))


def test_a_proxy_trusted_by_cidr_saying_https_makes_it_secure(world, monkeypatch):
    monkeypatch.setenv("LOCI_TRUSTED_PROXY_CIDRS", "172.18.0.0/16")
    assert _is_secure(world["login"](peer="172.18.0.5",
                                     headers=[("x-forwarded-proto", "https")]))


def test_an_untrusted_peer_cannot_claim_https(world):
    assert not _is_secure(world["login"](peer="192.168.1.20",
                                         headers=[("x-forwarded-proto", "https")]))


def test_logout_over_https_clears_with_the_same_flag(world):
    status, _out, set_cookie = world["call"]("POST", "/auth/logout", {}, scheme="https",
                                             peer="192.168.1.20")
    assert status == 200 and _is_secure(set_cookie)
    status, _out, set_cookie = world["call"]("POST", "/auth/logout", {}, peer="192.168.1.20")
    assert status == 200 and not _is_secure(set_cookie)
