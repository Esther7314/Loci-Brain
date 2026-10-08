# -*- coding: utf-8 -*-
"""
tests/test_panel_detail_window.py — the detail window: the entry, 关联, and 编辑
(panel contract 「面板接口」 §三: GET /api/loci/bucket/{id}, GET /api/loci/lineage/{id},
POST /api/loci/entry/fix).

WHAT IS AGREED
    Every row carries its id and short; times leave as local ISO with the offset; the tag
    row, the 关联 counts and every word on the window come from core (core/detail.py);
    an entry out of the request's scope reads as one that does not exist. 编辑 offers
    what the current rules allow (nothing on an archived entry or an old version) and the
    write asks the same rules again before writing. The three edits land in the ledger
    the way §七 says: 字写错了 a TraceUpdated naming the body, 内容错了 on an event a
    TraceCreated for the new version marked 人改的, 删除 a TraceDeletedToArchive (soft:
    the entry is still read by id). 内容错了 on a MIND entry (Q5) rewrites nothing: her
    `disputed` mark hangs on it (a TraceUpdated naming `invalidation`), the window shows
    it, and it is in breath's 依据变了的 with her note until the model deals with it. A
    write from another origin is refused and writes nothing.

Checked through the registered routes against a real BucketManager.
"""

import asyncio
import json
from datetime import datetime

import pytest
from starlette.requests import Request

import tools.grow as grow_mod
from core import _when as W
from core import detail as D
from core import names as S
from core import profile as P
from core import runtime as rt
from core.bucket_manager import BucketManager
from core.scope import OPEN_LINE
from tools.grow import rooms_path
from web import _shared as sh
from web import loci as L
from web import loci_detail as LD

NOW = datetime(2026, 10, 7, 21, 0, 0, tzinfo=W.LOCAL_TZ)


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    for mod in (rt, sh):
        monkeypatch.setattr(mod, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path / "buckets")})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    monkeypatch.setattr(W, "now", lambda: NOW)
    monkeypatch.setattr(P, "get_ai_name", lambda: "沈慢")

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    table = tmp_path / "aliases.yaml"
    table.write_text("小林:\n  instance_of: 人\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(table))
    monkeypatch.setattr(S, "_cache", None)
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


def get(routes, path, params=None, query=""):
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": query.encode(), "path_params": params or {}})
    resp = run(routes[(path, "GET")](req))
    return resp.status_code, json.loads(resp.body)


def post(routes, path, body, *, origin="http://127.0.0.1:8000", ctype="application/json"):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", ctype.encode())]
    if origin:
        headers.append((b"origin", origin.encode()))
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)
    resp = run(routes[(path, "POST")](req))
    return resp.status_code, json.loads(resp.body)


def bucket(routes, bid):
    return get(routes, "/api/loci/bucket/{bucket_id}", {"bucket_id": bid})


def lineage(routes, bid):
    return get(routes, "/api/loci/lineage/{bucket_id}", {"bucket_id": bid})


def fix(routes, body, **kw):
    return post(routes, "/api/loci/entry/fix", body, **kw)


def event(store, text, **kw):
    return run(store.create(text, room=kw.pop("room", "EVENT/SELF"), **kw))


def meta_of(store, bid):
    return (run(store.get_including_archive(bid)) or {}).get("metadata") or {}


def ledger(store):
    return list(store.ledger_mirror.iter_events())


# ───────────────────────── the window ─────────────────────────

def test_the_window_carries_ids_local_times_the_tag_row_and_the_edits(store, routes):
    bid = event(store, "小周说牙又疼了，我说周末陪小周去。", name="周末陪小周去看牙",
                summary="答应这周末陪小周去看牙", when="2026-10-11",
                direction_of_fit="telic", bound=["沈慢"], tags=["看牙"], subjects=["小林"])
    status, out = bucket(routes, bid)
    assert status == 200, out
    assert (out["id"], out["short"]) == (bid, bid[:6])
    assert out["date"] == "2026-10-11"
    assert out["tags_human"] == [{"key": "self", "text": "亲历"},
                                 {"key": "telic", "text": "想要"},
                                 {"key": "promised", "text": "我答应的"}]
    assert out["content"] == "小周说牙又疼了，我说周末陪小周去。"
    assert out["edit"] == ["typo", "content", "delete"] and out["can_edit"] is True
    assert out["related"] == {"later": 0, "signposts": 0}
    assert out["source_layer"] == "none"
    assert out["scope"] == OPEN_LINE
    # Stored in UTC, shown local with its offset.
    assert out["created"].endswith("+08:00"), out["created"]


def test_the_tag_row_says_each_kind_and_leaves_a_plain_record_bare():
    assert D.human_tags({"room": "MIND/VIEWS"}) == []
    assert [t["key"] for t in D.human_tags({"room": "EVENT/WORLD"})] == ["world"]
    owed = D.human_tags({"direction_of_fit": "telic", "bound": ["小林"]})
    assert owed[-1] == {"key": "promised", "text": "小林欠着"}
    assert D.human_tags({"room": "MIND/VIEWS", "evidential": "assumption"}) == [
        {"key": "assumption", "text": "猜的"}]
    assert D.human_tags({"internally_generated": True})[0]["key"] == "dream"
    assert D.human_tags({"recurrence": "FREQ=YEARLY", "when": "2019-10-09"}) == [
        {"key": "yearly", "text": "每年 10-09"}]
    assert D.human_tags({"direction_of_fit": "telic", "status": "abandoned"})[-1] == {
        "key": "abandoned", "text": "不做了"}
    assert D.human_tags({"exception_of": "aaaaaaaaaaaa", "hold": "avoid"}) == [
        {"key": "hold", "text": "条子·别碰"}]
    assert D.human_tags({"tags": ["人改的"]}) == [{"key": "edited", "text": "人改的"}]


def test_a_mind_entry_offers_a_content_mark_and_an_archived_one_offers_nothing(store, routes):
    view = run(store.create("小周紧张的时候想有人陪着。", room="MIND/TRAITS",
                            evidential="inference"))
    _, out = bucket(routes, view)
    # 内容错了 on a judgement is her mark, not a corrected copy: `can_edit` (the old
    # drawer's new-version form) stays off.
    assert out["edit"] == ["typo", "content", "delete"] and out["can_edit"] is False
    assert out["content_fix"] == "mark" and out["disputed"] is None
    assert out["tags_human"] == [{"key": "inference", "text": "推的"}]
    gone = event(store, "一条要收起来的。")
    run(store.delete(gone))
    status, out = bucket(routes, gone)
    assert status == 200 and out["archived"] is True and out["edit"] == []


def test_an_unknown_id_is_404_on_every_read(store, routes):
    for path in ("/api/loci/bucket/{bucket_id}", "/api/loci/lineage/{bucket_id}",
                 "/api/loci/source/{bucket_id}"):
        status, out = get(routes, path, {"bucket_id": "0123456789ab"})
        assert status == 404 and "查无此桶" in out["error"], (path, out)


class _Hides:
    """A read scope that refuses the ids it is given."""

    def __init__(self, *hidden):
        self.hidden = set(hidden)

    def permits(self, meta):
        return str(meta.get("id") or "") not in self.hidden


def test_out_of_scope_reads_as_absent_and_linked_lines_drop_out(store, routes, monkeypatch):
    root = event(store, "考完那天去吃甜品。")
    seen = run(store.create("小周考完会想庆祝。", room="MIND/VIEWS",
                            prov=[{"rel": "wasDerivedFrom", "target": root}]))
    hidden = run(store.create("小周考完想一个人待着。", room="MIND/VIEWS",
                              prov=[{"rel": "wasDerivedFrom", "target": root}]))
    line = "〔范围：受限 · 测试〕"

    async def scoped(request):
        return None, _Hides(hidden), line
    monkeypatch.setattr(LD, "read_scope_of", scoped)
    status, _ = bucket(routes, hidden)
    assert status == 404
    status, out = lineage(routes, root)
    assert status == 200 and out["scope"] == line
    assert [d["id"] for d in out["later"]["derived"]] == [seen]


# ───────────────────────── 关联 ─────────────────────────

def test_lineage_lists_what_came_after_and_the_signposts(store, routes):
    root = event(store, "小周说考完了就去海边。", when="2026-09-17", direction_of_fit="telic",
                 cue={"condition": "小周说考完了", "phrasings": ["考完了", "终于考完"]})
    view = run(store.create("小周紧张的时候想有人陪着。", room="MIND/TRAITS",
                            prov=[{"rel": "wasDerivedFrom", "target": root}]))
    gist = run(store.create("九月下旬的考试周", room="EVENT/SELF", tags=["__gist__"]))
    run(store.update(gist, cover=[root]))
    run(store.update(root, covered_by=[gist]))
    period = run(store.create("考试周\n小周每天复习到很晚", room="EVENT/SELF",
                              tags=["__大event__"], when="2026-09-15..2026-09-21"))
    hold = run(store.create("考试这事先别催小周", room="EVENT/SELF", exception_of=root,
                            hold="defer", when="2026-10-12"))

    status, out = lineage(routes, root)
    assert status == 200, out
    later, signs = out["later"], out["signposts"]
    assert later["derived"] == [{"id": view, "short": view[:6], "text": later["derived"][0]["text"],
                                 "kind": "mind", "state_words": ""}]
    assert [c["id"] for c in later["covered_by"]] == [gist]
    assert later["periods"] == [{"id": period, "short": period[:6], "text": "考试周",
                                 "span": "2026-09-15..2026-09-21"}]
    assert later["new_version"] is None
    assert signs["cue"] == {"condition": "小周说考完了", "phrasings": ["考完了", "终于考完"]}
    assert signs["holds"] == [{"id": hold, "short": hold[:6], "text": signs["holds"][0]["text"],
                               "hold": "defer", "words": "先别催", "until": "2026-10-12"}]
    assert out["count"] == 5 and out["scope"] == OPEN_LINE
    _, win = bucket(routes, root)
    assert win["related"] == {"later": 3, "signposts": 2}


def test_a_new_version_is_named_and_a_line_not_live_says_its_state(store, routes):
    old = event(store, "周六去海边。")
    new = event(store, "周日去海边。")
    run(store.update(old, superseded_by=new))
    _, out = lineage(routes, old)
    assert out["later"]["new_version"]["id"] == new
    assert out["later"]["new_version"]["state_words"] == ""
    _, win = bucket(routes, old)
    assert win["edit"] == [], "an old version is edited through the version in use"
    run(store.delete(new))
    _, out = lineage(routes, old)
    assert out["later"]["new_version"]["id"] == new
    assert out["later"]["new_version"]["state_words"] in ("在归档区", "已删除")


# ───────────────────────── 编辑 ─────────────────────────

def test_a_typo_fix_patches_the_body_and_the_ledger_names_the_body(store, routes):
    bid = event(store, "小周说牙又疼了。")
    before = ledger(store)[-1]
    status, out = fix(routes, {"id": bid, "kind": "typo", "old": "又疼", "new": "还疼"})
    assert status == 200 and out["ok"] and out["kind"] == "typo" and out["id"] == bid, out
    assert run(store.get(bid))["content"].strip() == "小周说牙还疼了。"
    last = ledger(store)[-1]
    assert last["event_type"] == "TraceUpdated" and last["trace_id"] == bid
    assert "content" in last["payload"]["changed_fields"]
    assert last["body_hash"] != before["body_hash"]


def test_a_typo_the_body_does_not_hold_is_refused_and_changes_nothing(store, routes):
    bid = event(store, "小周说牙又疼了。")
    n = len(ledger(store))
    status, out = fix(routes, {"id": bid, "kind": "typo", "old": "不在里面", "new": "x"})
    assert status == 409 and "old_str" in out["error"]
    assert len(ledger(store)) == n
    status, _ = fix(routes, {"id": bid, "kind": "typo", "new": "x"})
    assert status == 400


def test_a_content_fix_writes_a_new_version_marked_edited_and_keeps_the_old(store, routes):
    bid = event(store, "周六去海边。", valence=0.7, arousal=0.5)
    status, out = fix(routes, {"id": bid, "kind": "content", "text": "周日去海边。"})
    assert status == 200 and out["kind"] == "content" and out["id"] == bid, out
    new_id = out["new_id"]
    new = run(store.get(new_id))
    assert "人改的" in new["metadata"]["tags"]
    assert bid in new["metadata"]["prov"][0]["target"]
    assert run(store.get(bid))["content"].strip() == "周六去海边。"
    assert any(e["event_type"] == "TraceCreated" and e["trace_id"] == new_id
               for e in ledger(store))
    # It is in 依据变了的 for the model to take or leave.
    assert new_id in [e["id"] for e in P.edited_by_user(run(store.list_all()))]
    _, win = bucket(routes, new_id)
    assert {"key": "edited", "text": "人改的"} in win["tags_human"]


def test_content_wrong_on_a_mind_entry_hangs_her_mark_and_rewrites_nothing(store, routes):
    from core import _invalidation as I
    from tools.breath import awaken as A
    view = run(store.create("小周紧张的时候想有人陪看。", room="MIND/TRAITS"))
    n_entries = len(run(store.list_all(include_archive=True)))
    note = "不是紧张，是累了，想一个人待着"
    status, out = fix(routes, {"id": view, "kind": "content", "text": note})
    assert status == 200 and out["ok"] and out["kind"] == "content", out
    assert out["id"] == view and out["new_id"] is None and out["mark"] == "disputed"
    # Criterion: nothing is rewritten — no new version, the body as it was.
    assert len(run(store.list_all(include_archive=True))) == n_entries
    assert run(store.get(view))["content"].strip() == "小周紧张的时候想有人陪看。"
    assert not meta_of(store, view).get("superseded_by")
    last = ledger(store)[-1]
    assert last["event_type"] == "TraceUpdated" and last["trace_id"] == view
    assert last["payload"]["changed_fields"] == ["invalidation"]
    [rec] = meta_of(store, view)["invalidation"]
    assert rec["kind"] == I.DISPUTED and rec["note"] == note and not rec.get("confirmed_at")

    # The window shows the mark hanging, with her note, and does not offer it twice.
    _, win = bucket(routes, view)
    assert {"key": "disputed", "text": "人说不对·待复核"} in win["tags_human"]
    assert win["disputed"]["text"] == note and win["disputed"]["at"].endswith("+08:00")
    assert win["edit"] == ["typo", "delete"] and win["content_fix"] is None
    status, out = fix(routes, {"id": view, "kind": "content", "text": "再说一遍"})
    assert status == 409 and len(meta_of(store, view)["invalidation"]) == 1

    # It is in 依据变了的 for the model, with her note.
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["id"] == view and it["disputed"] == [{"at": rec["at"], "text": note}]
    lines = A._invalidation_lines({"items": [it], "more": 0})
    assert f"人在面板上说这条不对（10-07）：「{note}」" in lines[0], lines
    assert "人说不对的是你自己的判断" in lines[-1]

    # The typo kind keeps its own.
    status, out = fix(routes, {"id": view, "kind": "typo", "old": "陪看", "new": "陪着"})
    assert status == 200, out


def test_her_mark_with_no_note_says_so_plainly(store, routes):
    from core import _invalidation as I
    from tools.breath import awaken as A
    view = run(store.create("小周周末更想待在家里。", room="MIND/VIEWS"))
    status, _ = fix(routes, {"id": view, "kind": "content"})
    assert status == 200
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["disputed"][0]["text"] == ""
    line = A._invalidation_lines({"items": [it], "more": 0})[0]
    assert line.endswith("人在面板上说这条不对（10-07）"), line


def test_delete_is_soft_and_recorded_and_an_archived_entry_takes_no_fix(store, routes):
    bid = event(store, "一条要收起来的。")
    status, out = fix(routes, {"id": bid, "kind": "delete"})
    assert status == 200 and out["kind"] == "delete", out
    assert ledger(store)[-1]["event_type"] == "TraceDeletedToArchive"
    assert meta_of(store, bid), "still read by id"
    status, out = fix(routes, {"id": bid, "kind": "typo", "old": "一条", "new": "两条"})
    assert status == 409


def test_a_fix_from_another_origin_or_not_json_is_refused_and_writes_nothing(store, routes):
    bid = event(store, "小周说牙又疼了。")
    n = len(ledger(store))
    body = {"id": bid, "kind": "delete"}
    assert fix(routes, body, origin="")[0] == 403
    assert fix(routes, body, origin="http://127.0.0.1:9999")[0] == 403
    assert fix(routes, body, ctype="text/plain")[0] == 400
    assert len(ledger(store)) == n and meta_of(store, bid).get("deleted_at") is None
    assert fix(routes, {"id": bid, "kind": "rewrite"})[0] == 400
    assert fix(routes, {"id": "0123456789ab", "kind": "delete"})[0] == 404


def test_the_window_carries_the_title_every_list_shows(store, routes):
    # Criterion: `title` is core.profile.entry_label — the summary, else the name without
    # the time the store puts in front of it, else the start of the body — so the window
    # opens under the line of the row that was clicked.
    with_summary = event(store, "小周说牙又疼了。", name="周末陪小周去看牙",
                         summary="答应这周末陪小周去看牙")
    named = event(store, "小周说牙又疼了。", name="2026-10-07 21:00 周末陪小周去看牙")
    bare = event(store, "小周说牙又疼了，\n我说周末陪小周去。")
    for bid in (with_summary, named, bare):
        _, out = bucket(routes, bid)
        assert out["title"] == P.entry_label(meta_of(store, bid), out["content"]), out
    assert bucket(routes, with_summary)[1]["title"] == "答应这周末陪小周去看牙"
    assert bucket(routes, named)[1]["title"] == "周末陪小周去看牙"
