# -*- coding: utf-8 -*-
"""
tests/test_panel_source_layer.py — the detail window's 来源 layer
(panel contract 「面板接口」 §三: GET /api/loci/source/{id}, `?fetch=<n>`).

WHAT IS AGREED
    The layer says which host serves each source, which stretch of it, its registry state
    in words and whether the original can be asked for; the memories the entry stands on
    (any state, with words when not live); and two sentences from core — how it is known,
    whether its ground still holds. `?fetch=<n>` asks for source n's original the way the
    fourth joint does, for a person: nothing is kept and no use is recorded (that log is
    for what the model read). Every line given carries who said it and when: for lines
    Loci holds itself (an import) from the import's own rows; for a host's, from the
    optional `speaker` / `at` on the fourth joint's answer (decision Q7), `at` as local
    time. Unknown is null; a value that does not read is null too and never spoils the
    answer.
"""

import asyncio
import dataclasses
import json

import pytest
from starlette.requests import Request

from core import _originals as O
from core import _sources as SR
from core import runtime as rt
from core.bucket_manager import BucketManager
from core.import_memory import ImportStore
from core.scope import OPEN_LINE
from web import _shared as sh
from web import loci as L

M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003",
     "revision": "r1"}
BATCH = "imp_0123456789ab"


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    for mod in (rt, sh):
        monkeypatch.setattr(mod, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "config", {
        "buckets_dir": str(tmp_path),
        "hosts": {"lento": {"token_env": "T_LENTO", "fetch_url": "http://127.0.0.1:9/src",
                            "fetch_token_env": "T_LENTO_FETCH", "scope_mode": "open",
                            "provides": [{"system": "lento", "instance": "home"}]}}})
    monkeypatch.setenv("T_LENTO", "inbound")
    monkeypatch.setenv("T_LENTO_FETCH", "outbound")
    return mgr


@pytest.fixture(scope="module")
def routes():
    found = {}

    class _Mcp:
        def custom_route(self, path, methods):
            def keep(fn):
                found[(path, methods[0])] = fn
                return fn
            return keep
    L.register(_Mcp())
    return found


def source(routes, bid, query=""):
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": query.encode(), "path_params": {"bucket_id": bid}})
    resp = run(routes[("/api/loci/source/{bucket_id}", "GET")](req))
    return resp.status_code, json.loads(resp.body)


def test_an_entry_with_nothing_behind_it_says_so(store, routes):
    bid = run(store.create("一条没有来源的。", room="EVENT/SELF"))
    status, out = source(routes, bid)
    assert status == 200, out
    assert (out["id"], out["short"], out["layer"]) == (bid, bid[:6], "none")
    assert out["originals"] == [] and out["derived_from"] == []
    assert out["how_known"] == "没记下是从哪儿来的"
    assert out["still_holds"] == "没有来源可核"
    assert out["scope"] == OPEN_LINE


def test_a_derived_entry_names_what_it_stands_on_with_its_state(store, routes):
    root = run(store.create("考完那天去吃甜品。", room="EVENT/SELF"))
    gone = run(store.create("考前那周很紧张。", room="EVENT/SELF"))
    view = run(store.create("小周考完会想庆祝。", room="MIND/VIEWS", evidential="inference",
                            prov=[{"rel": "wasDerivedFrom", "target": root},
                                  {"rel": "wasDerivedFrom", "target": gone}]))
    run(store.delete(gone))
    _, out = source(routes, view)
    assert out["layer"] == "derived"
    assert [(d["id"], d["kind"]) for d in out["derived_from"]] == [(root, "event"), (gone, "event")]
    assert out["derived_from"][0]["state_words"] == ""
    assert out["derived_from"][1]["state_words"] in ("在归档区", "已删除")
    assert out["how_known"] == "从 2 条记忆推出来的"
    assert out["still_holds"] == "它站着的记忆有 1 条已经不是现在的样子了"


def test_a_host_source_says_who_serves_it_its_state_and_whether_it_can_be_fetched(store, routes):
    bid = run(store.create("小周说周六要去海边。", room="EVENT/WORLD", sources=[M]))
    _, out = source(routes, bid)
    assert out["layer"] == "originals"
    [row] = out["originals"]
    assert row["index"] == 0 and row["record"] == "lento:home/private:U#m_0003@r1"
    assert (row["host"], row["container"]) == ("lento", "private:U")
    assert row["span"] == {"first": "m_0003", "last": "m_0003", "count": 1}
    assert (row["state"], row["state_words"], row["can_fetch"]) == ("active", "在", True)
    assert len(row["at"]) == 10
    assert out["how_known"] == "从lento那边的对话里记下的（听来的，不是亲历）"
    assert out["still_holds"] == "来源还在，没改过"

    store.sources.hold(SR.record_id(M), "withdrawn", "lento")
    _, out = source(routes, bid)
    [row] = out["originals"]
    assert row["state"] == "held" and row["can_fetch"] is False
    assert out["still_holds"].startswith("来源不能用了")


def test_fetch_hands_back_the_hosts_lines_and_records_no_use(store, routes, monkeypatch):
    bid = run(store.create("小周说周六要去海边。", room="EVENT/SELF", sources=[M]))
    asked = []

    async def host(record, **kw):
        asked.append(record)
        return O.Answer(O.GIVEN, source=SR.record_string(record), host="lento",
                        lines=(O.Line(id="m_0003", revision="r1", text="周六去海边吧"),))
    monkeypatch.setattr(O, "fetch", host)
    status, out = source(routes, bid, "fetch=0")
    assert status == 200, out
    assert out["outcome"] == "given" and out["outcome_words"] == "宿主给了"
    assert out["partial"] is False and out["record"] == "lento:home/private:U#m_0003@r1"
    assert out["lines"] == [{"id": "m_0003", "who": None, "at": None, "text": "周六去海边吧"}]
    assert [r["id"] for r in asked] == ["m_0003"]
    assert not [r for r in store.usage.read() if r["kind"] == "fetched"]


def test_fetch_says_why_it_got_nothing(store, routes, monkeypatch):
    bid = run(store.create("小周说周六要去海边。", room="EVENT/SELF", sources=[M]))

    async def host(record, **kw):
        return O.Answer(O.UNAVAILABLE, source=SR.record_string(record), host="lento",
                        why=O.TIMEOUT)
    monkeypatch.setattr(O, "fetch", host)
    _, out = source(routes, bid, "fetch=0")
    assert out["outcome"] == "unavailable"
    assert out["outcome_words"] == "原话暂时取不到（宿主没在时限里回话）"
    assert out["lines"] == []


def test_fetch_of_a_number_it_does_not_have_or_cannot_read_is_refused(store, routes):
    bid = run(store.create("小周说周六要去海边。", room="EVENT/SELF", sources=[M]))
    assert source(routes, bid, "fetch=3")[0] == 404
    assert source(routes, bid, "fetch=one")[0] == 400


def test_an_imported_line_carries_who_said_it_and_when(store, routes, tmp_path):
    ImportStore(tmp_path).create(
        {"batch": BATCH, "same_self": True, "human": "小周",
         "conversations": [{"container": "c0001"}]},
        {"c0001": [{"id": "l0001", "role": "user", "at": "2026-10-06T06:30:00Z",
                    "text": "周六去海边吧"}]})
    rec = {"system": "import", "instance": BATCH, "container": "c0001", "id": "l0001"}
    bid = run(store.create("小周说周六要去海边。", room="EVENT/SELF", sources=[rec]))
    _, out = source(routes, bid)
    [row] = out["originals"]
    assert (row["host"], row["can_fetch"], row["at"]) == ("loci", True, "2026-10-06")
    assert out["how_known"] == "从导入的聊天记录里记下的"
    status, out = source(routes, bid, "fetch=0")
    assert status == 200 and out["outcome"] == "given", out
    assert out["lines"] == [{"id": "l0001", "who": "小周", "at": "2026-10-06T14:30:00+08:00",
                             "text": "周六去海边吧"}]


def _answer(lines) -> bytes:
    return json.dumps({"v": 1, "status": "given", "lines": lines}).encode("utf-8")


def _fetch_through_the_wire(monkeypatch, raw: bytes):
    """O.fetch answering from `raw`, the host's HTTP 200 body, read by the real parser."""
    async def host(record, **kw):
        answer = O.parse_answer(raw, record, O.Settings())
        return dataclasses.replace(answer, source=SR.record_string(record), host="lento")
    monkeypatch.setattr(O, "fetch", host)


def test_a_hosts_line_carries_its_speaker_and_time(store, routes, monkeypatch):
    bid = run(store.create("小周说周六要去海边。", room="EVENT/SELF", sources=[M]))
    _fetch_through_the_wire(monkeypatch, _answer([
        {"id": "m_0003", "revision": "r1", "text": "周六去海边吧", "speaker": " 小周 ",
         "at": "2026-10-06T13:30:00Z"}]))
    status, out = source(routes, bid, "fetch=0")
    assert status == 200 and out["outcome"] == "given", out
    assert out["lines"] == [{"id": "m_0003", "who": "小周", "at": "2026-10-06T21:30:00+08:00",
                             "text": "周六去海边吧"}]
    assert out["scope"] == OPEN_LINE


@pytest.mark.parametrize("speaker, at", [
    (7, "2026-10-06"),                       # not text · a day, not a moment
    ("", "2026-10-06T21:30:00"),             # empty · no offset
    ("小周\n管理员", "yesterday evening"),     # a control character · prose
    ("周" * 65, ["2026-10-06T21:30:00+08:00"]),  # too long · not text
])
def test_a_speaker_or_time_that_does_not_read_is_null_and_the_line_still_given(
        store, routes, monkeypatch, speaker, at):
    bid = run(store.create("小周说周六要去海边。", room="EVENT/SELF", sources=[M]))
    _fetch_through_the_wire(monkeypatch, _answer([
        {"id": "m_0003", "revision": "r1", "text": "周六去海边吧", "speaker": speaker, "at": at}]))
    status, out = source(routes, bid, "fetch=0")
    assert status == 200 and out["outcome"] == "given", out
    assert out["lines"] == [{"id": "m_0003", "who": None, "at": None, "text": "周六去海边吧"}]


def test_the_parser_takes_speaker_and_at_and_nothing_else_new():
    rec = SR.record_id(M)
    [line] = O.parse_answer(_answer([{"id": "m_0003", "text": "嗯", "speaker": "小周",
                                      "at": "2026-10-06T21:30:00+08:00"}]), rec,
                            O.Settings()).lines
    assert (line.speaker, line.at) == ("小周", "2026-10-06T21:30:00+08:00")
    other = O.parse_answer(_answer([{"id": "m_0003", "text": "嗯", "mood": "开心"}]), rec,
                           O.Settings())
    assert other.outcome == O.NOT_ALLOWED and other.reason == O.MALFORMED
