"""
The server's keepalive: it pings a route that exists and answers without a login
(web/panel_auth.KEEPALIVE_PATH), and only a 2xx answer is logged as a ping that worked.
"""

import asyncio

import httpx
import pytest

from server_app import RuntimeLifecycle


class _Log:
    def __init__(self):
        self.lines = []

    def debug(self, msg, *args):
        self.lines.append(("debug", msg % args))

    def warning(self, msg, *args):
        self.lines.append(("warning", msg % args))

    info = error = debug


class _Answer:
    def __init__(self, status_code):
        self.status_code = status_code


def _client(answers):
    """A fake client: each get takes the next answer (a status, or an exception to raise);
    when they run out the loop is cancelled."""
    class _Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def get(self, url, timeout=None):
            if not answers:
                raise asyncio.CancelledError
            got = answers.pop(0)
            if isinstance(got, BaseException):
                raise got
            return _Answer(got)
    return _Client


def _run_loop(answers):
    log = _Log()
    life = RuntimeLifecycle(logger=log, keepalive_url="http://127.0.0.1:1/ping",
                            keepalive_initial_delay=0, keepalive_interval=0,
                            keepalive_client=_client(list(answers)))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(life._keepalive_loop())
    return log.lines


def test_a_2xx_answer_is_a_ping_that_worked():
    assert _run_loop([200, 204]) == [("debug", "Keepalive ping OK")] * 2


def test_a_non_2xx_answer_is_a_failed_ping_not_ok():
    lines = _run_loop([404, 401, 500])
    assert [lvl for lvl, _ in lines] == ["warning"] * 3
    assert "HTTP 404 from http://127.0.0.1:1/ping" in lines[0][1]
    assert all("OK" not in text for _lvl, text in lines)


def test_no_answer_is_a_failed_ping_and_the_loop_goes_on():
    lines = _run_loop([httpx.ConnectError("refused"), 200])
    assert lines[0][0] == "warning" and "refused" in lines[0][1]
    assert lines[1] == ("debug", "Keepalive ping OK")


def test_the_keepalive_path_is_public_and_answers_a_locked_gate_without_a_login(monkeypatch):
    from starlette.requests import Request
    import web
    from web import _shared as sh
    from web import panel_auth as PA

    table = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                table[(methods[0], path)] = fn
                return fn
            return keep
    PA.register(web._Gated(_Mcp()))
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    monkeypatch.setattr(PA, "has_session", lambda r: False)
    monkeypatch.setattr(sh, "_load_auth_data", lambda: {})

    assert PA.is_public(PA.KEEPALIVE_PATH)
    route = table[("GET", PA.KEEPALIVE_PATH)]
    request = Request({"type": "http", "method": "GET", "path": PA.KEEPALIVE_PATH,
                       "query_string": b"", "headers": [(b"host", b"127.0.0.1")]})
    assert asyncio.run(route(request)).status_code == 200
