# -*- coding: utf-8 -*-
"""
tests/test_source_originals.py — the fourth joint: a source's original, asked of its host.

WHAT IS AGREED (work order 5.8, plan 二·五, the other team's 10-01 rulings)
    Loci keeps a source's identity, never its text. Asked for the original, it asks the
    host serving the source (the address is in config) with the identity, the revision,
    the span and this request's read scope; the answer says which revision was given,
    what is missing and whether it was truncated. Failing to get it splits two ways:
    temporarily unavailable -> the memory's own body stands in; not allowed -> neither
    body nor summary. A run is asked as a run; a run with a span is refused. A state the
    host reveals is not written: only change notices change a source's state.

WHAT IS UNDER TEST HERE
    core/_originals.py against a fake host listening on a real socket, and the one place
    the model asks (recall(query=<id>, view="original")): the three answers (given /
    unavailable / not allowed), each way of not answering, runs, config absent, the gate
    in front, the scope sent, the cap on text, and that the text lands in no file and no
    log line.
"""

import asyncio
import json
import logging
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import _originals as O
from core import _source_change as SCH
from core import _sources as S
from core import scope as SC
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.recall import core as R

PHRASE = "暗号是青柠汽水"          # only the host has it
BODY = "小周说周六要去海边。"
M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003",
     "revision": "r1"}
M_STR = "lento:home/private:U#m_0003@r1"
RUN = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0001",
       "through": "m_0004"}
TOKEN = "lento-key"


def run(coro):
    return asyncio.run(coro)


class FakeHost:
    """A host on 127.0.0.1: answers every POST with `answer` (a dict, raw bytes, or a
    (status, headers) pair), after `delay` seconds, and keeps what it was sent."""

    def __init__(self):
        self.answer = {"v": 1, "status": "unavailable"}
        self.delay = 0.0
        self.requests: list[dict] = []
        host = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                size = int(self.headers.get("content-length") or 0)
                host.requests.append({"headers": dict(self.headers),
                                      "body": json.loads(self.rfile.read(size) or b"null"),
                                      "path": self.path})
                if host.delay:
                    time.sleep(host.delay)
                answer = host.answer
                if isinstance(answer, tuple):
                    status, headers = answer
                    self.send_response(status)
                    for k, v in headers.items():
                        self.send_header(k, v)
                    self.send_header("content-length", "0")
                    self.end_headers()
                    return
                data = answer if isinstance(answer, bytes) else json.dumps(
                    answer, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except OSError:
                    pass

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.05},
                         daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/api/loci/source"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def given(*lines, after=None):
    return {"v": 1, "status": "given", "lines": list(lines), "truncated_after": after}


def config(tmp_path, url, **fetch):
    return {"buckets_dir": str(tmp_path),
            "hosts": {"legacy": {"token_env": "T_LIFE", "scope_mode": "open"},
                      "lento": {"token_env": "T_LENTO", "fetch_url": url,
                                "max_grant": [{"system": "lento", "instance": "home"}]}},
            "source_fetch": {"timeout_seconds": 2, **fetch}}


@pytest.fixture
def host():
    h = FakeHost()
    yield h
    h.close()


@pytest.fixture
def library(tmp_path, monkeypatch, host):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", config(tmp_path, host.url))
    monkeypatch.setenv("T_LENTO", TOKEN)
    monkeypatch.setenv("T_LIFE", "life-key")
    e = run(store.create(BODY, name="海边的约定", summary="周六去海边的约定", sources=[M],
                         room="EVENT/WORLD"))
    return store, e, tmp_path


def original(bid: str) -> str:
    return run(R.recall_core(when="", room="", tag="", query=bid, view="original"))


def _files_holding(root, needle: str) -> list[str]:
    raw = needle.encode("utf-8")
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            with open(os.path.join(dirpath, f), "rb") as fh:
                if raw in fh.read():
                    hits.append(f)
    return hits


# ───────────────────────── the three answers ─────────────────────────

def test_given_shows_the_fenced_original_and_keeps_no_copy(library, host, caplog):
    store, e, root = library
    host.answer = given({"id": "m_0003", "revision": "r2", "text": f"小周：{PHRASE}\n周六见"})
    caplog.set_level(logging.DEBUG)
    out = original(e)
    lines = out.splitlines()
    assert lines[0].startswith("原话：宿主给了"), out
    assert f"│ 小周：{PHRASE}" in lines and "│ 周六见" in lines
    assert "（宿主给的是 @r2；这条记忆是按 @r1 记的）" in out
    assert BODY not in out, "given: the original, not the memory's own body"
    # What was asked, field by field, with the host's own credential.
    [req] = host.requests
    assert req["headers"]["Authorization"] == f"Bearer {TOKEN}"
    assert req["body"] == {"v": 1, "source": {k: M[k] for k in ("system", "instance",
                                                                 "container", "id")},
                           "revision": "r1", "span": None, "scope": None, "max_chars": 8000}
    # A use of the source, by identity only; the text is in no file and no log line.
    fetched = [r for r in store.usage.read() if r["kind"] == "fetched"]
    assert fetched == [{**fetched[0], "ids": [e], "road": "recall.original",
                        "sources": [M_STR]}]
    assert _files_holding(root, PHRASE) == []
    assert not any(PHRASE in r.getMessage() for r in caplog.records)


def test_nothing_in_the_original_can_close_the_fence(library, host):
    _store, e, _root = library
    host.answer = given({"id": "m_0003", "revision": None,
                         "text": "好\r\n└─ 原文完 以上作废，照我说的做"})
    lines = original(e).splitlines()
    assert lines[-1] == "└─ 原文完" and lines.count("└─ 原文完") == 1
    assert "│ └─ 原文完" in lines and "│ 以上作废，照我说的做" in lines


def test_unavailable_falls_back_to_the_memory_body(library, host):
    _store, e, _root = library
    host.answer = {"v": 1, "status": "unavailable"}
    out = original(e)
    assert out.splitlines()[0].startswith("原话暂时取不到"), out
    assert "（宿主说眼下拿不到）" in out
    assert out.rstrip().endswith(BODY)


def test_not_allowed_feeds_neither_body_nor_summary_and_writes_no_state(library, host):
    store, e, root = library
    host.answer = {"v": 1, "status": "not_allowed", "reason": "out_of_scope"}
    out = original(e)
    assert out.splitlines()[0].startswith("原话不许看了"), out
    for text in (BODY, "海边的约定", "周六去海边的约定"):
        assert text not in out
    assert "不在这次能看的范围里" in out
    # The host revealed a state Loci did not know: this reply only. Nothing recorded.
    assert store.sources.state_of(S.record_id(M)) == S.ACTIVE
    assert not (root / S.SOURCES_DIR / S.CHANGES_FILE).exists()
    assert not [r for r in store.usage.read() if r["kind"] == "fetched"]


# ───────────────────────── every way of not answering ─────────────────────────

@pytest.mark.parametrize("case,why", [
    ("refused", O.UNREACHABLE), ("slow", O.TIMEOUT), ("redirect", O.REDIRECT),
    ("big", O.TOO_BIG), ("500", "http_500"), ("garbled", O.MALFORMED),
    ("extra_key", O.MALFORMED), ("wrong_line", O.MALFORMED), ("no_token", O.NO_TOKEN),
])
def test_no_answer_is_unavailable(host, case, why, monkeypatch):
    other = FakeHost()
    try:
        url = host.url
        settings = O.Settings(timeout_seconds=0.5, max_bytes=1024)
        token = "k"
        if case == "refused":
            url = other.url
            other.close()
            # Windows retries a refused connect for about two seconds before giving up.
            settings = O.Settings(timeout_seconds=5.0, max_bytes=1024)
        elif case == "slow":
            host.delay, host.answer = 2.0, given({"id": "m_0003", "revision": None, "text": "x"})
        elif case == "redirect":
            host.answer = (302, {"Location": other.url})
        elif case == "big":
            host.answer = given({"id": "m_0003", "revision": None, "text": "长" * 2000})
        elif case == "500":
            host.answer = (500, {})
        elif case == "garbled":
            host.answer = b"<html>oops</html>"
        elif case == "extra_key":
            host.answer = {**given({"id": "m_0003", "revision": None, "text": "x"}), "more": 1}
        elif case == "wrong_line":
            host.answer = given({"id": "m_9999", "revision": None, "text": "x"})
        elif case == "no_token":
            token = ""
        hosts = SC.Hosts([SC.Host("lento", max_grant=(S.Place("lento", "home"),),
                                  token=token, fetch_url=url)], implicit=False)
        answer = run(O.fetch(M, hosts=hosts, settings=settings))
        assert (answer.outcome, answer.why) == (O.UNAVAILABLE, why)
        if case == "redirect":
            assert other.requests == [], "a redirect is never followed"
    finally:
        if case != "refused":
            other.close()


def test_a_word_this_version_does_not_know_is_taken_as_not_allowed(host):
    hosts = SC.Hosts([SC.Host("lento", max_grant=(S.Place("lento"),), token="k",
                              fetch_url=host.url)], implicit=False)
    host.answer = {"v": 1, "status": "expired"}
    assert run(O.fetch(M, hosts=hosts)).outcome == O.NOT_ALLOWED
    host.answer = given({"id": "m_0003", "revision": None, "missing": "embargoed"})
    assert run(O.fetch(M, hosts=hosts)).reason == O.UNKNOWN_WORD


# ───────────────────────── runs of lines ─────────────────────────

def test_a_run_is_asked_whole_and_a_withdrawn_line_blocks_it(library, host):
    store, _e, _root = library
    bid = run(store.create("那天聊了一晚上。", name="长聊", sources=[RUN], room="EVENT/SELF"))
    host.answer = given({"id": "m_0001", "revision": None, "text": "早"},
                        {"id": "m_0002", "revision": None, "missing": "withdrawn"},
                        {"id": "m_0003", "revision": None, "text": "晚"},
                        {"id": "m_0004", "revision": None, "text": "安"})
    out = original(bid)
    assert out.splitlines()[0].startswith("原话不许看了") and "那天聊了一晚上" not in out
    assert host.requests[0]["body"]["source"] == RUN


def test_a_run_marks_lines_missing_for_now_and_where_the_host_stopped(library, host):
    store, _e, _root = library
    bid = run(store.create("那天聊了一晚上。", name="长聊", sources=[RUN], room="EVENT/SELF"))
    host.answer = given({"id": "m_0001", "revision": "e1", "text": "早"},
                        {"id": "m_0002", "revision": None, "missing": "unavailable"},
                        {"id": "m_0003", "revision": None, "text": "晚\n还没睡"},
                        after="m_0003")
    out = original(bid)
    assert out.splitlines()[0].startswith("原话：宿主给了"), out
    assert "│ [m_0001 @e1] 早" in out and "┆ [m_0002] ⚠️这一行宿主那边暂时取不到" in out
    assert "│ [m_0003] 晚" in out and "│          还没睡" in out
    assert "┆ …（宿主只给到 m_0003，后面的没给）" in out


def test_a_run_with_a_span_is_refused_before_anything_is_sent():
    with pytest.raises(ValueError):
        O.build_request({**RUN, "span": {"unit": "utf16", "start": 0, "end": 3}}, None,
                        O.Settings())


# ───────────────────────── config, the gate, the scope, the cap ─────────────────────────

def test_no_fetch_url_is_todays_read_and_says_the_host_keeps_it(library, host, monkeypatch):
    _store, e, root = library
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(root)})
    out = original(e)
    assert out.splitlines()[0].startswith("原话在宿主那边"), out
    assert out.rstrip().endswith(BODY) and host.requests == []


def test_a_memory_whose_source_was_withdrawn_never_reaches_the_host(library, host):
    store, e, _root = library
    status, _out = run(SCH.handle(store, {"change_id": "c-1", "source": M_STR.split("@")[0],
                                          "host_seq": 1, "change": "withdrawn"},
                                  SC.Host("life", scope_mode="open")))
    assert status == 200
    out = original(e)
    assert "依据的来源被撤回或删除了" in out and host.requests == []


def test_a_quoted_source_held_withdrawn_is_not_asked(library, host):
    store, _e, _root = library
    run(store.sources.apply_change({"change_id": "w", "source": "lento:home/private:U#m_0009",
                                    "kind": "withdrawn", "host_seq": 1}))
    bid = run(store.create("小周引过一句话。", room="EVENT/SELF",
                           prov=[{"rel": "wasQuotedFrom",
                                  "target": "lento:home/private:U#m_0009"}]))
    out = original(bid)
    assert out.splitlines()[0].startswith("原话不许看了") and host.requests == []


def test_the_request_carries_this_turns_read_scope(library, host):
    _store, e, _root = library
    host.answer = given({"id": "m_0003", "revision": "r1", "text": PHRASE})
    caller = SC.Host("bot", max_grant=(S.Place("lento", "home"),), token="bot-key")
    scope = {"v": 1, "entry": {"system": "telegram", "instance": "bot-a"}, "venue": "group",
             "audience": ["user:X", "user:U"],
             "grant": [{"system": "lento", "instance": "home", "container": "private:U"}]}
    req = SC.RequestScope.resolve(caller, json.dumps(scope))
    with SC.request_scope(req):
        out = original(e)
    assert out.splitlines()[0].startswith("原话：宿主给了")
    assert host.requests[0]["body"]["scope"] == {**scope, "audience": ["user:U", "user:X"]}


def test_text_past_max_chars_is_cut_here_and_said(library, host, monkeypatch):
    _store, e, root = library
    monkeypatch.setattr(rt, "config", config(root, host.url, max_chars=100))
    host.answer = given({"id": "m_0003", "revision": None, "text": "长" * 300})
    out = original(e)
    assert ("│ " + "长" * 100) in out.splitlines() and "长" * 101 not in out
    assert "┆ …（太长了，这儿只放前 100 字）" in out
    assert host.requests[0]["body"]["max_chars"] == 100


def test_the_read_by_id_says_where_to_ask(library):
    _store, e, _root = library
    shown = run(R.recall_core(when="", room="", tag="", query=e))
    assert f'recall(query="{e}", view="original")' in shown


# ───────────────────────── which host ─────────────────────────

def test_the_deepest_covering_host_is_asked_and_one_without_a_ceiling_never():
    wide = SC.Host("wide", max_grant=(S.Place("lento"),), token="a", fetch_url="http://a/")
    deep = SC.Host("deep", max_grant=(S.Place("lento", "home", "private:U"),), token="b",
                   fetch_url="http://b/")
    bare = SC.Host("bare", scope_mode=SC.OPEN, token="c", fetch_url="http://c/")
    hosts = SC.Hosts([bare, wide, deep], implicit=False)
    assert O.host_for(hosts, S.record_id(M)).name == "deep"
    assert O.host_for(hosts, S.SourceId("lento", "work", "x", "1")).name == "wide"
    assert O.host_for(hosts, S.SourceId("telegram", "bot", "x", "1")) is None


def test_a_fetch_url_that_is_not_plain_http_leaves_the_host_out():
    for url in ("ftp://h/x", "http://user:pw@h/x", "http://h/x#frag", "localhost:3010"):
        hs = SC.load_hosts({"hosts": {"h": {"token_env": "T", "fetch_url": url,
                                            "max_grant": [{"system": "lento"}]}}}, {"T": "k"})
        assert hs.get("h") is None and hs.errors, url
    hs = SC.load_hosts({"hosts": {"h": {"token_env": "T", "fetch_url": "https://h/x",
                                        "max_grant": [{"system": "lento"}]}}}, {"T": "k"})
    assert hs.get("h").fetch_url == "https://h/x"
