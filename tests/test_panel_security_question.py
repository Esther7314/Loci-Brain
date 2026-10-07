# -*- coding: utf-8 -*-
"""
tests/test_panel_security_question.py — 账号 · 设置安全问题, and the forgot-password page it feeds.

POST /api/loci/auth/security-question {question, answer} sets or changes the question
(web/loci_password, through `_save_security_qa`). The answer resets the password, and the
password is also the door to the remote MCP authorization page, so:

  · it wants a logged-in session — even on a panel left unlocked (`panel_auth: false`),
    where the gate itself lets everything through;
  · a request carrying a host's credential is refused like on every panel write, session
    or not;
  · with no password yet, or a password held by LOCI_DASHBOARD_PASSWORD, it is refused
    (the question would reset nothing).

Set, the forgot-password page's two routes use it: GET /auth/recovery-question shows it,
POST /auth/recover takes the answer (trimmed, any case) and sets the new password.
The routes are the real ones behind the real gate (web._Gated), on a real auth file.
"""

import asyncio
import json

import pytest
from starlette.requests import Request

PASSWORD = "first-password-1"
PATH = "/api/loci/auth/security-question"


@pytest.fixture
def world(tmp_path, monkeypatch):
    import web
    from web import _shared as sh
    from web import loci as Wl
    from web import panel_auth as PA

    monkeypatch.delenv("LOCI_DASHBOARD_PASSWORD", raising=False)
    monkeypatch.delenv("LOCI_HOOK_TOKEN", raising=False)
    config = {"buckets_dir": str(tmp_path), "panel_auth": True}
    monkeypatch.setattr(sh, "config", config)
    # A rate limiter of its own, so the wrong answer here counts against nobody else.
    from collections import OrderedDict, deque
    monkeypatch.setattr(sh, "_login_failures", {})
    monkeypatch.setattr(sh, "_login_locked_until", {})
    monkeypatch.setattr(sh, "_login_source_lru", OrderedDict())
    monkeypatch.setattr(sh, "_login_global_attempts", deque())

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

    def call(method, path, body=None, cookie=None, headers=()):
        hdrs = [(b"host", b"testserver"), (b"origin", b"http://testserver"),
                (b"content-type", b"application/json")]
        if cookie:
            hdrs.append((b"cookie", f"{PA._COOKIE}={cookie}".encode()))
        hdrs += [(k.encode(), v.encode()) for k, v in headers]
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "headers": hdrs,
                       "query_string": b"", "client": ("127.0.0.1", 50000)}, receive)
        resp = asyncio.run(table[(method, path)](req))
        set_cookie = resp.headers.get("set-cookie", "")
        return resp.status_code, json.loads(resp.body), set_cookie

    def login(password=PASSWORD):
        status, out, set_cookie = call("POST", "/auth/login", {"password": password})
        assert status == 200, out
        return set_cookie.split(f"{PA._COOKIE}=", 1)[1].split(";", 1)[0]

    return {"call": call, "login": login, "config": config, "sh": sh}


def _set_password(world, pw=PASSWORD):
    assert world["sh"]._save_password_hash(pw)


def test_with_no_password_there_is_nothing_to_recover(world):
    status, out, _ = world["call"]("POST", PATH, {"question": "q", "answer": "a"})
    assert status == 409 and "先设一把密码" in out["error"]


def test_a_locked_panel_wants_a_session(world):
    _set_password(world)
    status, _out, _ = world["call"]("POST", PATH, {"question": "q", "answer": "a"})
    assert status == 401


def test_an_unlocked_panel_still_wants_a_session(world):
    _set_password(world)
    world["config"]["panel_auth"] = False
    status, out, _ = world["call"]("POST", PATH, {"question": "q", "answer": "a"})
    assert status == 401 and out["error"] == "请先登录"
    assert world["call"]("GET", "/auth/recovery-question")[1]["question"] == ""
    cookie = world["login"]()
    status, out, _ = world["call"]("POST", PATH, {"question": "第一只猫叫什么", "answer": "Mimi"},
                                   cookie=cookie)
    assert status == 200, out


def test_a_host_credential_is_refused_even_with_a_session(world, monkeypatch):
    _set_password(world)
    cookie = world["login"]()
    monkeypatch.setenv("T_LENTO", "host-key-0123456789")
    world["config"]["hosts"] = {"lento": {"token_env": "T_LENTO", "scope_mode": "open"}}
    status, out, _ = world["call"]("POST", PATH, {"question": "q", "answer": "a"}, cookie=cookie,
                                   headers=[("x-loci-hook-token", "host-key-0123456789")])
    assert status == 403
    assert world["call"]("GET", "/auth/recovery-question")[1]["question"] == ""


def test_an_environment_password_is_refused(world, monkeypatch):
    monkeypatch.setenv("LOCI_DASHBOARD_PASSWORD", "env-password-1")
    status, out, _ = world["call"]("POST", PATH, {"question": "q", "answer": "a"})
    assert status == 409 and "LOCI_DASHBOARD_PASSWORD" in out["error"]


def test_both_are_needed_and_kept_short(world):
    _set_password(world)
    cookie = world["login"]()
    for body in ({"question": "q", "answer": "  "}, {"question": "", "answer": "a"},
                 {"question": "q", "answer": 3}, {"question": "q" * 201, "answer": "a"}):
        status, _out, _ = world["call"]("POST", PATH, body, cookie=cookie)
        assert status == 400, body


def test_set_it_and_the_forgot_page_uses_it(world):
    _set_password(world)
    cookie = world["login"]()
    status, out, _ = world["call"]("POST", PATH,
                                   {"question": " 第一只猫叫什么 ", "answer": " Mimi "}, cookie=cookie)
    assert status == 200 and out["ok"] and out["question"] == "第一只猫叫什么"
    assert "Mimi" not in json.dumps(out, ensure_ascii=False), "the answer never comes back"
    assert world["call"]("GET", "/api/loci/auth/state")[1]["question"] == "第一只猫叫什么"
    assert world["call"]("GET", "/auth/recovery-question")[1]["question"] == "第一只猫叫什么"

    status, out, _ = world["call"]("POST", "/auth/recover",
                                   {"answer": "rex", "password": "second-password-2"})
    assert status == 401 and out["error"] == "答案不对"
    status, out, set_cookie = world["call"]("POST", "/auth/recover",
                                            {"answer": "  mimi", "password": "second-password-2"})
    assert status == 200 and set_cookie, out
    assert world["sh"]._verify_any_password("second-password-2")
    assert not world["sh"]._verify_any_password(PASSWORD)

    # Changing it later replaces the old one.
    cookie = world["login"]("second-password-2")
    world["call"]("POST", PATH, {"question": "最喜欢的城市", "answer": "Kyoto"}, cookie=cookie)
    assert world["call"]("GET", "/auth/recovery-question")[1]["question"] == "最喜欢的城市"
    assert world["call"]("POST", "/auth/recover",
                         {"answer": "mimi", "password": "third-password-3"})[0] == 401
