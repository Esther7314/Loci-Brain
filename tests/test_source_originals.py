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
from core import runtime as rt
from tools.recall import core as R

PHRASE = "暗号是青柠汽水"          # only the host has it
BODY = "小周说周六要去海边。"
M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003",
     "revision": "r1"}
M_STR = "lento:home/private:U#m_0003@r1"
RUN = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0001",
       "through": "m_0004"}
SRC_PLACE = {"system": "lento", "instance": "home", "container": "private:U"}
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
                                "fetch_token_env": "T_LENTO_FETCH",
                                "max_grant": [{"system": "lento", "instance": "home"}],
                                "provides": [{"system": "lento", "instance": "home"}]}},
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
    monkeypatch.setenv("T_LENTO", "lento-inbound-key")
    monkeypatch.setenv("T_LENTO_FETCH", TOKEN)
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
    # What was asked, field by field, with Loci's own credential toward the host.
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

def _serving(url, token="k", **kw):
    """A deployment of one host declared as serving lento's originals."""
    return SC.Hosts([SC.Host("lento", max_grant=(S.Place("lento", "home"),), token="in",
                             fetch_url=url, fetch_token=token,
                             provides=(S.Place("lento", "home"),), **kw)], implicit=False)


@pytest.mark.parametrize("case,why", [
    ("refused", O.UNREACHABLE), ("slow", O.TIMEOUT), ("no_token", O.NO_TOKEN),
])
def test_only_no_connection_a_timeout_or_no_credential_is_unavailable(host, case, why):
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
        elif case == "no_token":
            token = ""
        answer = run(O.fetch(M, hosts=_serving(url, token), settings=settings))
        assert (answer.outcome, answer.why) == (O.UNAVAILABLE, why)
        assert not answer.holds
    finally:
        if case != "refused":
            other.close()


@pytest.mark.parametrize("case,reason", [
    ("redirect", O.REDIRECT), ("big", O.TOO_BIG), ("500", "http_500"), ("404", "http_404"),
    ("401", "http_401"), ("403", "http_403"), ("garbled", O.MALFORMED),
    ("extra_key", O.MALFORMED), ("wrong_line", O.MALFORMED),
])
def test_a_refusal_or_an_answer_loci_cannot_read_lets_nothing_through(host, case, reason):
    other = FakeHost()
    try:
        if case == "redirect":
            host.answer = (302, {"Location": other.url})
        elif case == "big":
            host.answer = given({"id": "m_0003", "revision": None, "text": "长" * 2000})
        elif case in ("500", "404", "401", "403"):
            host.answer = (int(case), {})
        elif case == "garbled":
            host.answer = b"<html>oops</html>"
        elif case == "extra_key":
            host.answer = {**given({"id": "m_0003", "revision": None, "text": "x"}), "more": 1}
        elif case == "wrong_line":
            host.answer = given({"id": "m_9999", "revision": None, "text": "x"})
        answer = run(O.fetch(M, hosts=_serving(host.url),
                             settings=O.Settings(timeout_seconds=2.0, max_bytes=1024)))
        assert (answer.outcome, answer.reason) == (O.NOT_ALLOWED, reason)
        assert not answer.holds, "this call only"
        if case == "redirect":
            assert other.requests == [], "a redirect is never followed"
    finally:
        other.close()


def test_gone_410_is_not_allowed_for_this_call_only(host):
    host.answer = (410, {})
    answer = run(O.fetch(M, hosts=_serving(host.url)))
    assert (answer.outcome, answer.reason) == (O.NOT_ALLOWED, O.GONE_HTTP)
    assert answer.holds == (), "the endpoint's word, not the source's"


def test_a_word_this_version_does_not_know_is_taken_as_not_allowed(host):
    hosts = _serving(host.url)
    host.answer = {"v": 1, "status": "expired"}
    assert run(O.fetch(M, hosts=hosts)).outcome == O.NOT_ALLOWED
    host.answer = given({"id": "m_0003", "revision": None, "missing": "embargoed"})
    assert run(O.fetch(M, hosts=hosts)).reason == O.UNKNOWN_WORD


def test_the_fetch_credential_is_loci_own_never_the_hosts_inbound_one():
    env = {"T_IN": "same", "T_OUT": "same"}
    hs = SC.load_hosts({"hosts": {"h": {"token_env": "T_IN", "fetch_url": "https://h/x",
                                        "fetch_token_env": "T_OUT",
                                        "max_grant": [{"system": "lento"}],
                                        "provides": [{"system": "lento"}]}}}, env)
    assert hs.get("h") is not None and hs.get("h").fetch_token == "", "equal value: not used"
    hs = SC.load_hosts({"hosts": {"h": {"token_env": "T_IN", "fetch_token_env": "T_IN",
                                        "max_grant": [{"system": "lento"}]}}}, env)
    assert hs.get("h") is None and hs.errors, "the same variable is refused"
    env["T_OUT"] = "other"
    hs = SC.load_hosts({"hosts": {"h": {"token_env": "T_IN", "fetch_url": "https://h/x",
                                        "fetch_token_env": "T_OUT",
                                        "max_grant": [{"system": "lento"}],
                                        "provides": [{"system": "lento"}]}}}, env)
    assert hs.get("h").fetch_token == "other" and hs.get("h").token == "same"


# ───────────────────────── runs of lines ─────────────────────────

def test_a_withdrawn_line_blocks_the_run_and_holds_that_line(library, host):
    store, _e, _root = library
    store.sources.record_order(SRC_PLACE, ["m_0001", "m_0002", "m_0003", "m_0004"])
    bid = run(store.create("那天聊了一晚上。", name="长聊", sources=[RUN], room="EVENT/SELF"))
    host.answer = given({"id": "m_0001", "revision": None, "text": "早"},
                        {"id": "m_0002", "revision": None, "missing": "withdrawn"},
                        {"id": "m_0003", "revision": None, "text": "晚"},
                        {"id": "m_0004", "revision": None, "text": "安"})
    out = original(bid)
    assert "那天聊了一晚上" not in out and "依据的来源被撤回或删除了" in out, out
    assert host.requests[0]["body"]["source"] == RUN
    # Not just this once: the line is held until the host's ordered change settles it.
    assert store.sources.state_of("lento:home/private:U#m_0002") == S.HELD
    assert store.sources.describe("lento:home/private:U#m_0002") is None
    shown = run(R.recall_core(when="", room="", tag="", query=bid))
    assert "那天聊了一晚上" not in shown
    asked = len(host.requests)
    original(bid)
    assert len(host.requests) == asked, "a held source is not asked again"


def test_a_run_marks_lines_missing_shows_it_as_partial(library, host):
    store, _e, _root = library
    store.sources.record_order(SRC_PLACE, ["m_0001", "m_0002", "m_0003", "m_0004"])
    bid = run(store.create("那天聊了一晚上。", name="长聊", sources=[RUN], room="EVENT/SELF"))
    host.answer = given({"id": "m_0001", "revision": "e1", "text": "早"},
                        {"id": "m_0002", "revision": None, "missing": "unavailable"},
                        {"id": "m_0003", "revision": None, "text": "晚\n还没睡"},
                        after="m_0003")
    out = original(bid)
    assert out.splitlines()[0].startswith("原话：宿主只给了一部分"), out
    assert "│ [m_0001 @e1] 早" in out and "┆ [m_0002] ⚠️这一行宿主那边暂时取不到" in out
    assert "│ [m_0003] 晚" in out and "│          还没睡" in out
    assert "┆ …（宿主只给到 m_0003，后面的没给）" in out
    assert out.rstrip().endswith("那天聊了一晚上。"), "the memory's own body below the part"


def test_a_line_the_host_has_no_record_of_is_not_found_not_deleted(library, host):
    store, e, _root = library
    host.answer = given({"id": "m_0003", "revision": None, "missing": "not_found"})
    out = original(e)
    assert out.splitlines()[0].startswith("原话暂时取不到") and "宿主那边找不到这条" in out
    assert out.rstrip().endswith(BODY)
    assert store.sources.state_of(S.record_id(M)) == S.ACTIVE, "nothing held"
    store.sources.record_order(SRC_PLACE, ["m_0001", "m_0002", "m_0003", "m_0004"])
    bid = run(store.create("那天聊了一晚上。", name="长聊", sources=[RUN], room="EVENT/SELF"))
    host.answer = given({"id": "m_0001", "revision": None, "text": "早"},
                        {"id": "m_0002", "revision": None, "missing": "not_found"},
                        {"id": "m_0003", "revision": None, "text": "晚"},
                        {"id": "m_0004", "revision": None, "text": "安"})
    out = original(bid)
    assert out.splitlines()[0].startswith("原话：宿主只给了一部分")
    assert "┆ [m_0002] ⚠️这一行宿主那边找不到" in out


def test_a_run_whose_lines_are_unknown_is_not_asked(library, host):
    store, _e, _root = library
    bid = run(store.create("那天聊了一晚上。", name="长聊", sources=[RUN], room="EVENT/SELF"))
    out = original(bid)
    assert out.splitlines()[0].startswith("原话不许看了") and host.requests == []
    assert "宿主没交过里面有哪几行" in out and "那天聊了一晚上" not in out


def test_a_run_with_a_span_is_refused_before_anything_is_sent():
    with pytest.raises(ValueError):
        O.build_request({**RUN, "span": {"unit": "utf16", "start": 0, "end": 3}}, None,
                        O.Settings())


# ───────────────────────── the state, checked again after the answer ─────────────────────────

def test_a_withdrawal_landing_while_the_host_answers_wins(library, host, monkeypatch):
    store, e, _root = library
    host.answer = given({"id": "m_0003", "revision": "r1", "text": PHRASE})
    real = O._post

    async def withdrawn_meanwhile(*a, **kw):
        got = await real(*a, **kw)
        await SCH.handle(store, {"change_id": "c-1", "source": M_STR.split("@")[0],
                                 "host_seq": 1, "change": "withdrawn"},
                         SC.Host("life", scope_mode="open"))
        return got
    monkeypatch.setattr(O, "_post", withdrawn_meanwhile)
    out = original(e)
    assert PHRASE not in out and BODY not in out, out
    assert "依据的来源被撤回或删除了" in out


def test_unavailable_does_not_fall_back_once_the_source_is_gone_meanwhile(library, host,
                                                                          monkeypatch):
    store, e, _root = library
    host.answer = {"v": 1, "status": "unavailable"}
    real = O._post

    async def withdrawn_meanwhile(*a, **kw):
        got = await real(*a, **kw)
        await SCH.handle(store, {"change_id": "c-1", "source": M_STR.split("@")[0],
                                 "host_seq": 1, "change": "withdrawn"},
                         SC.Host("life", scope_mode="open"))
        return got
    monkeypatch.setattr(O, "_post", withdrawn_meanwhile)
    out = original(e)
    assert BODY not in out and "海边的约定" not in out, out


def test_a_host_saying_withdrawn_holds_it_everywhere_until_the_ordered_change(library, host):
    store, e, _root = library
    host.answer = {"v": 1, "status": "not_allowed", "reason": "withdrawn"}
    out = original(e)
    assert BODY not in out
    assert store.sources.state_of(S.record_id(M)) == S.HELD
    assert not (store.sources.changes_path).exists(), "no state was written"
    shown = run(R.recall_core(when="", room="", tag="", query=e))
    assert BODY not in shown, "recall does not keep feeding the old body"
    # The ordered change settles it either way: here, the host restores it.
    run(SCH.handle(store, {"change_id": "c-1", "source": M_STR.split("@")[0], "host_seq": 1,
                           "change": "restored"}, SC.Host("life", scope_mode="open")))
    assert store.sources.state_of(S.record_id(M)) == S.ACTIVE
    host.answer = given({"id": "m_0003", "revision": "r1", "text": PHRASE})
    assert original(e).splitlines()[0].startswith("原话：宿主给了")


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

def test_the_declared_provider_is_asked_never_a_host_that_merely_may_touch_it():
    wide = SC.Host("wide", max_grant=(S.Place("lento"),), fetch_token="a",
                   fetch_url="http://a/", provides=(S.Place("lento"),))
    deep = SC.Host("deep", max_grant=(S.Place("lento", "home", "private:U"),),
                   fetch_token="b", fetch_url="http://b/",
                   provides=(S.Place("lento", "home", "private:U"),))
    toucher = SC.Host("toucher", max_grant=(S.Place("telegram"),), fetch_token="c",
                      fetch_url="http://c/")
    hosts = SC.Hosts([toucher, wide, deep], implicit=False)
    assert O.host_for(hosts, S.record_id(M)).name == "deep"
    assert O.host_for(hosts, S.SourceId("lento", "work", "x", "1")).name == "wide"
    assert O.host_for(hosts, S.SourceId("telegram", "bot", "x", "1")) is None, \
        "max_grant says what a host may touch, not whose the source is"
    twin = SC.Host("twin", fetch_token="d", fetch_url="http://d/",
                   provides=(S.Place("lento", "home", "private:U"),))
    assert O.host_for(SC.Hosts([deep, twin], implicit=False), S.record_id(M)) is None, \
        "two providers declared at the same place: neither"
    no_url = SC.Host("no-url", fetch_token="e", provides=(S.Place("lento"),))
    assert O.host_for(SC.Hosts([no_url], implicit=False), S.record_id(M)) is None


def test_a_fetch_url_that_is_not_plain_http_leaves_the_host_out():
    for url in ("ftp://h/x", "http://user:pw@h/x", "http://h/x#frag", "localhost:3010"):
        hs = SC.load_hosts({"hosts": {"h": {"token_env": "T", "fetch_url": url,
                                            "max_grant": [{"system": "lento"}]}}}, {"T": "k"})
        assert hs.get("h") is None and hs.errors, url
    hs = SC.load_hosts({"hosts": {"h": {"token_env": "T", "fetch_url": "https://h/x",
                                        "max_grant": [{"system": "lento"}]}}}, {"T": "k"})
    assert hs.get("h").fetch_url == "https://h/x"
