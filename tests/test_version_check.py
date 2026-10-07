# -*- coding: utf-8 -*-
"""
tests/test_version_check.py — the setting page's version block: what runs, and 「检查更新」.

The panel reads the running version; on request it asks GitHub for the latest release of
the project and gets its tag, day, notes and page, with whether it is newer. The answer is
kept a short while, so pressing again does not ask again. When GitHub cannot be reached
the reply says it could not check, with status 200 — never an error page. Nothing is
installed: there is no update route. The network call is stubbed throughout.
"""

import asyncio
import json

import pytest
from starlette.requests import Request

from core import releases as R

RELEASE = {"tag_name": "v1.5.0", "name": "1.5.0", "published_at": "2026-10-06T18:00:00Z",
           "body": "## 改了什么\n- 阈值可以在设置页改了",
           "html_url": "https://github.com/Esther7314/Loci-Brain/releases/tag/v1.5.0"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def github(monkeypatch):
    calls = []
    answer = {"status": 200, "body": RELEASE, "raise": None}

    async def fetch(url, headers):
        calls.append((url, headers))
        if answer["raise"] is not None:
            raise answer["raise"]
        return answer["status"], answer["body"]
    monkeypatch.setattr(R, "FETCH", fetch)
    R.forget()
    yield {"calls": calls, "answer": answer}
    R.forget()


def test_a_newer_release_is_said_with_its_notes_and_page(github):
    out = run(R.check("1.4.0", now=0.0))
    assert out["ok"] is True and out["newer"] is True
    latest = out["latest"]
    assert latest["tag"] == "v1.5.0" and latest["date"] == "2026-10-07"
    assert "阈值" in latest["notes"] and latest["url"].endswith("/v1.5.0")
    assert "有新版本 v1.5.0" in out["words"]
    url, headers = github["calls"][0]
    assert url == "https://api.github.com/repos/Esther7314/Loci-Brain/releases/latest"
    assert headers["User-Agent"] == "Loci-Brain/1.4.0"


@pytest.mark.parametrize("running,newer,word", [("1.5.0", False, "已经是最新的"),
                                                ("1.6.0", False, "还新"),
                                                ("dev", None, "对不上号")])
def test_the_same_or_an_older_release(github, running, newer, word):
    out = run(R.check(running, now=0.0))
    assert out["newer"] is newer and word in out["words"]


def test_the_answer_is_kept_a_short_while(github):
    run(R.check("1.4.0", now=0.0))
    run(R.check("1.4.0", now=R.CACHE_SECONDS - 1))
    assert len(github["calls"]) == 1
    run(R.check("1.4.0", now=R.CACHE_SECONDS + 1))
    assert len(github["calls"]) == 2


@pytest.mark.parametrize("fail,word", [
    ({"raise": TimeoutError("slow")}, "超时"),
    ({"raise": OSError("no route")}, "连不上 GitHub"),
    ({"status": 403, "body": {"message": "API rate limit exceeded"}}, "限流"),
    ({"status": 500, "body": None}, "读不懂"),
    ({"status": 200, "body": ["not", "a", "release"]}, "读不懂"),
])
def test_a_failed_check_is_a_plain_reply(github, fail, word):
    github["answer"].update(fail)
    out = run(R.check("1.4.0", now=0.0))
    assert out["ok"] is False and out["latest"] is None and out["newer"] is None
    assert word in out["words"] and "没查到更新" in out["words"]
    # A failure is kept for less time than an answer.
    run(R.check("1.4.0", now=R.FAILURE_CACHE_SECONDS + 1))
    assert len(github["calls"]) == 2


def test_no_release_yet(github):
    github["answer"].update({"status": 404, "body": {"message": "Not Found"}})
    out = run(R.check("1.4.0", now=0.0))
    assert out["ok"] is True and out["latest"] is None and out["newer"] is None
    assert "还没有发布过" in out["words"]


def _routes(monkeypatch):
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA
    table = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                table[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(_Mcp())
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(sh, "version", "1.4.0")
    return table


def _get(table, query=b""):
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    req = Request({"type": "http", "method": "GET", "path": "/api/loci/version",
                   "query_string": query, "headers": [(b"host", b"127.0.0.1:8000")]},
                  receive)
    resp = run(table[("GET", "/api/loci/version")](req))
    return resp.status_code, json.loads(resp.body)


def test_the_route_reads_the_version_and_checks_only_on_request(github, monkeypatch):
    table = _routes(monkeypatch)
    status, out = _get(table)
    assert status == 200 and out["version"] == "1.4.0" and "check" not in out
    assert out["releases_url"] == "https://github.com/Esther7314/Loci-Brain/releases"
    assert github["calls"] == []
    status, out = _get(table, b"check=1")
    assert status == 200 and out["check"]["newer"] is True
    github["answer"]["raise"] = OSError("offline")
    R.forget()
    status, out = _get(table, b"check=1")
    assert status == 200 and out["check"]["ok"] is False


def test_nothing_installs_an_update(monkeypatch):
    table = _routes(monkeypatch)
    assert not [k for k in table if "update" in k[1]]
