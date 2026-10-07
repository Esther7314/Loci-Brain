# -*- coding: utf-8 -*-
"""
tests/test_panel_grow.py — grow's page: what was written today, and the slices.

GET /api/loci/grow/today: memories on the timeline written since today began, newest
first. Today starts at the `since` a host hands over (its daily report), else at local
midnight — Loci knows of no report itself. GET /api/loci/grow/slices: every batch the
pending store holds, open, handled and replaced slices alike, each with its state in
words and only the guesses at or above the guess line; an imported conversation's batch
is labelled by its title and its open slices wait to be checked.
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
    assert done["span"] == {"first": "m1", "last": "m3", "count": 3}
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


def test_a_host_registering_the_lines_names_the_batch():
    class _Host:
        name = "lento-home"

    class _Hosts:
        def registrar_for(self, source):
            return _Host()
    assert GV._batch_label({"source": SRC, "import": None}, _Hosts(), 6) == "lento-home · 6 段"
