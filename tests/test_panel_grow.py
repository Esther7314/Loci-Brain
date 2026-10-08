# -*- coding: utf-8 -*-
"""
tests/test_panel_grow.py — grow's page: what was written today, and the slices.

GET /api/loci/grow/today: memories on the timeline written since today began, newest
first. Today starts at the `since` a host hands over (its daily report), else at the
latest `report_at` a host's slices batch carried (one host's with `?host=`; never one in
the future), else at local midnight. GET /api/loci/grow/slices: every batch the
pending store holds, open, handled and replaced slices alike, each with its state in
words and only the guesses at or above the guess line; an imported conversation's batch
is labelled by its title and its open slices wait to be checked.
GET /api/loci/grow/slices/{id}/source: one slice's 原话 laid out as the 来源 layer lists a
source; `?fetch=1` asks for the lines — a host's of the host serving them (Loci keeps no
text of them), an import's from Loci's own copy. A slice whose source the registry reads
as withdrawn, deleted or held shows nothing it was cut from, here and on the list.
"""

import asyncio
import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from core import _when as W
from core import detail as D
from core import grow_view as GV
from core.bucket_manager import BucketManager

SRC = {"system": "lento", "instance": "home", "container": "p"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))

    def get(route, **query):
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": route, "path_params": {},
                       "headers": [], "query_string": urlencode(query).encode()})
        resp = run(routes[("GET", route)](req))
        return resp.status_code, json.loads(resp.body)
    return {"store": store, "get": get, "sh": sh}


def _at(**delta) -> str:
    return (W.now() + timedelta(**delta)).isoformat(timespec="seconds")


# ── grow/today ───────────────────────────────────────────────────────────────

@pytest.fixture
def written(panel):
    store = panel["store"]

    async def seed():
        a = await store.create("考试考完了。", room="EVENT/SELF")
        b = await store.create("周末去海边。", room="EVENT/SELF")
        await store.create("我叫沈慢。", room="EVENT/SELF", tags=["__档案事实__"])
        return a, b
    a, b = run(seed())
    return {**panel, "a": a, "b": b}


def test_today_lists_what_was_written_since_midnight(written):
    status, out = written["get"]("/api/loci/grow/today")
    assert status == 200, out
    assert out["since_from"] == "midnight"
    assert out["since"] == W.today().isoformat(timespec="seconds")
    assert out["scope"] == "〔范围：全库（open）〕"
    assert out["total"] == 2, "the name page is not on the timeline"
    ids = {i["id"] for i in out["items"]}
    assert ids == {written["a"], written["b"]}
    row = next(i for i in out["items"] if i["id"] == written["a"])
    assert row["short"] == written["a"][:6] and row["text"] == "考试考完了。"
    meta = run(written["store"].get(written["a"]))["metadata"]
    assert row["at"] and row["tags_human"] == D.human_tags(meta)
    assert {"key": "self", "text": "亲历"} in row["tags_human"]
    stamps = [i["at"] for i in out["items"]]
    assert stamps == sorted(stamps, reverse=True)


def test_a_host_hands_over_its_own_report_time(written):
    status, out = written["get"]("/api/loci/grow/today", since=_at(minutes=5))
    assert status == 200 and out["since_from"] == "report" and out["total"] == 0
    assert out["items"] == [] and out["next_offset"] is None


def test_today_pages_with_as_of(written):
    get = written["get"]
    _s, first = get("/api/loci/grow/today", limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1
    _s, before = get("/api/loci/grow/today", as_of=_at(minutes=-30),
                     since=_at(days=-1))
    assert before["total"] == 0, "written after as_of"


def test_the_day_cut_is_one_function():
    now = W.now()
    cut, how = GV.day_cut(now)
    assert how == GV.SINCE_MIDNIGHT and cut == W.to_local(now).replace(
        hour=0, minute=0, second=0, microsecond=0)
    cut, how = GV.day_cut(now, "2026-10-07T05:12:00+08:00")
    assert how == GV.SINCE_REPORT and cut.isoformat().startswith("2026-10-07T05:12:00")


def _reported(store, host, report_at, n=1):
    """A slices batch from `host` carrying `report_at` (POST /api/v2/slices keeps both)."""
    lines = [(f"r{n}", f"sha256:{n:064x}")]
    run(store.slices.record_batch(
        batch_id=f"b_{host}_{n}", source=SRC, day="2026-10-06", revision=None, lines=lines,
        slices=[{"first": f"r{n}", "last": f"r{n}", "gist": "日报那天", "guesses": []}],
        host=host, report_at=report_at))


def test_today_starts_at_the_last_report_a_batch_carried(written):
    store, get = written["store"], written["get"]
    life, bot = _at(hours=-30), _at(hours=-50)
    _reported(store, "life", life, 1)
    _reported(store, "life", _at(minutes=5), 2)            # a clock ahead is passed over
    _reported(store, "bot", bot, 3)
    status, out = get("/api/loci/grow/today")
    assert status == 200 and out["since_from"] == "report", out
    assert out["since"] == life, "the latest any host wrote, not later than now"
    assert out["total"] == 2
    _s, out = get("/api/loci/grow/today", host="bot")
    assert out["since_from"] == "report" and out["since"] == bot
    _s, out = get("/api/loci/grow/today", host="nobody")
    assert out["since_from"] == "midnight"
    given = _at(days=-4)
    _s, out = get("/api/loci/grow/today", since=given)
    assert out["since"] == given and out["since_from"] == "report", "the caller's own wins"


def test_the_report_survives_a_restart(written):
    from core import _slicer as SL
    _reported(written["store"], "life", "2026-10-07T05:12:00+08:00")
    fresh = SL.PendingSlices(written["store"].base_dir)
    cut, how = GV.day_cut(W.now(), pending=fresh)
    assert how == GV.SINCE_REPORT and cut.isoformat() == "2026-10-07T05:12:00+08:00"


def test_human_tags_come_from_core_detail(written, monkeypatch):
    monkeypatch.setattr(D, "human_tags", lambda meta: [{"key": "self", "text": "亲历"}])
    _s, out = written["get"]("/api/loci/grow/today")
    assert all(i["tags_human"] == [{"key": "self", "text": "亲历"}] for i in out["items"])


# ── grow/slices ──────────────────────────────────────────────────────────────

@pytest.fixture
def slices(panel):
    store = panel["store"]

    async def seed():
        a = await store.create("周末陪去看牙。", room="EVENT/SELF")
        lines = [(f"m{i}", f"sha256:{i:064x}") for i in range(1, 7)]
        host_batch, _r = await store.slices.record_batch(
            batch_id="b_host", source=SRC, day="2026-10-06", revision=None, lines=lines,
            slices=[{"first": "m1", "last": "m3", "gist": "牙又疼了，约了周末",
                     "guesses": [{"id": a, "score": 0.81}, {"id": "ffffffffffff",
                                                             "score": 0.5}]},
                    {"first": "m4", "last": "m6", "gist": "说到晚饭", "guesses": []}])
        # As a host's batch handed over (core/_slicer.take_batch): its order registered.
        store.sources.record_order(SRC, [i for i, _fp in lines], batch_id="b_host")
        imp, _r = await store.slices.record_batch(
            batch_id="b_imp", source={"system": "import", "instance": "loci",
                                      "container": "imp_1"},
            day="2026-10-05", revision=None, lines=lines[:2],
            slices=[{"first": "m1", "last": "m2", "gist": "旧聊天里的一段",
                     "draft": "喜欢海", "guesses": []}],
            origin={"batch": "imp_1", "same_self": True, "title": "旧聊天"})
        first, second = (s["slice_id"] for s in host_batch["slices"])
        await store.slices.close(first, "trace", [a])
        return a, first, second
    a, first, second = run(seed())
    return {**panel, "a": a, "first": first, "second": second}


def test_every_batch_with_every_slice_and_its_state(slices):
    status, out = slices["get"]("/api/loci/grow/slices")
    assert status == 200, out
    assert [b["batch_id"] for b in out["items"]] == ["b_imp", "b_host"], "newest first"
    imp, host = out["items"]
    assert imp["label"] == "来自导入 · 旧聊天" and imp["import"]["batch"] == "imp_1"
    [draft] = imp["slices"]
    assert draft["state"] == "open" and draft["state_words"] == "等他核"
    assert draft["draft"] == "喜欢海"

    assert host["label"] == "lento · 2 段" and host["day"] == "2026-10-06"
    assert host["recorded_at"]
    done, waiting = host["slices"]
    assert done["slice_id"] == slices["first"] and done["state"] == "closed"
    assert done["how"] == "trace" and done["by"] == [slices["a"]]
    assert done["state_words"] == "补进已有的了"
    assert done["span"] == {"first": "m1", "last": "m3", "count": 3,
                            "from_line": 1, "to_line": 3}, "line numbers counted from 1"
    assert waiting["span"]["from_line"] == 4 and waiting["span"]["to_line"] == 6
    assert [g["id"] for g in done["guesses"]] == [slices["a"]], "only at or above 0.65"
    assert done["guesses"][0]["text"] == "周末陪去看牙。" and done["guesses"][0]["score"] == 0.81
    assert waiting["state"] == "open" and waiting["state_words"] == "等他看"
    assert out["total"] == 2


def test_a_resent_batch_shows_what_it_replaced(slices):
    store = slices["store"]
    lines = [(f"m{i}", f"sha256:{i:064x}") for i in range(1, 7)]
    run(store.slices.record_batch(
        batch_id="b_host", source=SRC, day="2026-10-06", revision=None, lines=lines,
        slices=[{"first": "m4", "last": "m6", "gist": "说到晚饭和电影", "guesses": []}]))
    _s, out = slices["get"]("/api/loci/grow/slices")
    host = next(b for b in out["items"] if b["batch_id"] == "b_host")
    states = {s["slice_id"]: s for s in host["slices"]}
    assert states[slices["second"]]["state"] == "replaced"
    assert states[slices["second"]]["state_words"] == "重切过"
    assert states[slices["first"]]["state"] == "closed"
    assert host["label"] == "lento · 2 段"
    assert host["slices"][-1]["slice_id"] == slices["second"], "replaced ones last"


def test_slices_page_by_batch_with_as_of(slices):
    get = slices["get"]
    _s, first = get("/api/loci/grow/slices", limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1
    assert first["items"][0]["batch_id"] == "b_imp"
    _s, old = get("/api/loci/grow/slices", as_of=_at(minutes=-10))
    assert old["total"] == 0, "recorded after as_of"


def test_no_slices_is_an_empty_page(panel):
    status, out = panel["get"]("/api/loci/grow/slices")
    assert status == 200 and out["items"] == [] and out["total"] == 0


# ── one slice's 原话 ─────────────────────────────────────────────────────────

def _slice_source(panel, sid, **query):
    from starlette.requests import Request
    from web import loci_activity as A
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "path_params": {"slice_id": sid},
                   "query_string": urlencode(query).encode()})
    resp = run(A.api_loci_grow_slice_source(req))
    return resp.status_code, json.loads(resp.body)


def test_a_hosts_slice_says_where_its_lines_are_and_asks_the_host_for_them(slices, monkeypatch):
    import dataclasses
    from core import _originals as O
    from core import _sources as SR
    status, out = _slice_source(slices, slices["second"])
    assert status == 200, out
    assert (out["slice_id"], out["gist"], out["label"], out["day"]) == (
        slices["second"], "说到晚饭", "lento", "2026-10-06")
    assert out["span"]["from_line"] == 4 and out["state_words"] == "等他看"
    o = out["original"]
    assert o["record"].startswith("lento:home/p#m4..m6")
    assert o["span"]["count"] == 3 and o["state"] == "active" and o["state_words"] == "在"
    # No host is declared to serve lento:home: Loci keeps no text of a host's lines.
    assert o["host"] is None and o["can_fetch"] is False
    status, got = _slice_source(slices, slices["second"], fetch=1)
    assert status == 200 and got["outcome"] == "no_host" and got["lines"] == []
    assert got["outcome_words"] == "原话在宿主那边，这儿没配谁给原话"

    asked = []

    async def host(record, **kw):
        asked.append(record)
        answer = O.parse_answer(json.dumps({"v": 1, "status": "given", "lines": [
            {"id": "m4", "revision": None, "text": "晚饭吃什么", "speaker": "小周"},
            {"id": "m5", "revision": None, "missing": "unavailable"},
            {"id": "m6", "revision": None, "text": "面吧"}]}).encode(), record, O.Settings())
        return dataclasses.replace(answer, source=SR.record_string(record), host="lento")
    monkeypatch.setattr(O, "fetch", host)
    status, got = _slice_source(slices, slices["second"], fetch=1)
    assert status == 200 and got["outcome"] == "given" and got["partial"] is True
    assert got["lines"][0] == {"id": "m4", "who": "小周", "at": None, "text": "晚饭吃什么"}
    assert got["lines"][1]["missing_words"] == "这一行暂时取不到"
    assert asked[0]["id"] == "m4" and asked[0]["through"] == "m6"
    assert _slice_source(slices, "sl_nope")[0] == 404
    assert _slice_source(slices, "sl_nope", fetch=1)[0] == 404


def test_an_imported_slice_reads_the_lines_loci_holds(panel):
    from core.import_memory import ImportStore
    store = panel["store"]
    batch = "imp_0123456789ab"
    where = {"system": "import", "instance": batch, "container": "c0001"}
    rows = [{"id": f"l000{i}", "role": "user" if i % 2 else "assistant",
             "at": f"2026-10-05T0{i}:00:00+08:00", "text": f"第{i}句"} for i in range(1, 4)]
    ImportStore(store.base_dir).create(
        {"batch": batch, "same_self": True, "human": "小周", "title": "旧聊天",
         "conversations": [{"container": "c0001"}]}, {"c0001": rows})
    store.sources.record_order(where, [r["id"] for r in rows], batch_id=batch)
    out, _r = run(store.slices.record_batch(
        batch_id="b_imp2", source=where, day="2026-10-05", revision=None,
        lines=[(r["id"], f"sha256:{i:064x}") for i, r in enumerate(rows)],
        slices=[{"first": "l0001", "last": "l0003", "gist": "约了周六", "draft": "约好周六见",
                 "guesses": []}],
        origin={"batch": batch, "same_self": True, "title": "旧聊天"}))
    sid = out["slices"][0]["slice_id"]
    status, got = _slice_source(panel, sid)
    assert status == 200, got
    assert got["label"] == "来自导入 · 旧聊天" and got["draft"] == "约好周六见"
    o = got["original"]
    assert (o["host"], o["can_fetch"], o["span"]["count"]) == ("loci", True, 3)
    assert o["span"]["first_at"] == "2026-10-05T01:00:00+08:00"
    status, lines = _slice_source(panel, sid, fetch=1)
    assert status == 200 and lines["outcome"] == "given", lines
    assert [(ln["who"], ln["at"], ln["text"]) for ln in lines["lines"]] == [
        ("小周", "2026-10-05T01:00:00+08:00", "第1句"),
        ("我", "2026-10-05T02:00:00+08:00", "第2句"),
        ("小周", "2026-10-05T03:00:00+08:00", "第3句")]


def test_a_slice_on_a_held_source_shows_nothing_it_was_cut_from(slices):
    from core import _source_change as SC
    store = slices["store"]
    run(SC.hold(store, "lento:home/p#m5", "withdrawn", "lento"))
    status, out = _slice_source(slices, slices["second"])
    assert status == 200
    assert out["gist"] == "" and out["source_words"] == "宿主说撤回或删了，等确认"
    assert out["original"]["state"] == "held" and out["original"]["can_fetch"] is False
    status, got = _slice_source(slices, slices["second"], fetch=1)
    assert got["outcome"] == "not_allowed" and got["lines"] == []
    # The list says the same: no gist, the words in its place.
    _s, page = slices["get"]("/api/loci/grow/slices")
    host = next(b for b in page["items"] if b["batch_id"] == "b_host")
    held = next(s for s in host["slices"] if s["slice_id"] == slices["second"])
    assert held["gist"] == "" and held["source_words"] == "宿主说撤回或删了，等确认"
    other = next(s for s in host["slices"] if s["slice_id"] == slices["first"])
    assert other["gist"] == "牙又疼了，约了周末" and "source_words" not in other


def test_a_host_registering_the_lines_names_the_batch():
    class _Host:
        name = "lento-home"

    class _Hosts:
        def registrar_for(self, source):
            return _Host()
    assert GV._batch_label({"source": SRC, "import": None}, _Hosts(), 6) == "lento-home · 6 段"
