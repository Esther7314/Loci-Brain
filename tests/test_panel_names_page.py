# -*- coding: utf-8 -*-
"""
tests/test_panel_names_page.py — the names page and the name card
(panel contract 「面板接口」 §四, §五 name: GET /api/loci/names, /names/pending,
/names/{name}, POST /api/loci/names/action).

WHAT IS AGREED
    The page lists only the names the table knows and gives a kind, by kind, most
    mentioned first, and counts how many wait; the pending page lists the others (a name
    the table knows without a kind among them), each with the entry it first appeared in
    and what it looks like when something says (the side model's kind the table could not
    take; a person for a name hanging in a work or a group), and a name leaves it once a
    button is pressed. The card says what the
    table says, the MIND entry filed as its card, and the entries naming it (after the
    table's normalising), paged. Every list pages by offset / limit / as_of: an entry
    written after `as_of` does not shift the pages. The buttons write aliases.yaml only —
    never an entry and never the ledger — and a write from another origin is refused.
    The old paths (/api/loci/subjects, /subjects/action) keep working.
"""

import asyncio
import json
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote

import pytest
from starlette.requests import Request

from core import _when as W
from core import names as S
from core import runtime as rt
from core.bucket_manager import BucketManager
from core.scope import OPEN_LINE
from web import _shared as sh
from web import loci as L

TABLE = """\
小林:
  aliases: [林林]
  instance_of: 人
  member_of: [读书会]
读书会:
  instance_of: 群
底特律:
  instance_of: 游戏
"""


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    for mod in (rt, sh):
        monkeypatch.setattr(mod, "bucket_mgr", mgr)
    table = tmp_path / "aliases.yaml"
    table.write_text(TABLE, encoding="utf-8")
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


def get(routes, path, query="", params=None):
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": query.encode(), "path_params": params or {}})
    resp = run(routes[(path, "GET")](req))
    return resp.status_code, json.loads(resp.body)


def post(routes, path, body, *, origin="http://127.0.0.1:8000"):
    raw = json.dumps(body).encode()

    async def receive():
        return {"type": "http.request", "body": raw, "more_body": False}
    headers = [(b"host", b"127.0.0.1:8000"), (b"content-type", b"application/json")]
    if origin:
        headers.append((b"origin", origin.encode()))
    req = Request({"type": "http", "method": "POST", "path": "/", "headers": headers}, receive)
    resp = run(routes[(path, "POST")](req))
    return resp.status_code, json.loads(resp.body)


def card(routes, name, query=""):
    return get(routes, "/api/loci/names/{name}", query, {"name": name})


def entry(store, text, subjects, **kw):
    return run(store.create(text, room=kw.pop("room", "EVENT/SELF"), subjects=subjects, **kw))


def test_the_names_page_lists_known_names_by_kind_and_counts_the_pending(store, routes):
    entry(store, "小林面完第二轮。", ["小林"], when="2026-10-03")
    entry(store, "林林说想换工作。", ["林林"], when="2026-09-20")
    entry(store, "读书会这周读完了。", ["读书会"])
    entry(store, "阿哲今天又加班。", ["阿哲"])
    entry(store, "老周来电话。", ["老周"])
    status, out = get(routes, "/api/loci/names")
    assert status == 200, out
    assert out["kinds"] == [{"kind": "人", "n": 1}, {"kind": "群", "n": 1}]
    assert out["items"][0] == {"name": "小林", "kind": "人", "n": 2, "aliases": ["林林"],
                               "last": "2026-10-03"}
    assert [r["name"] for r in out["items"]] == ["小林", "读书会"]
    assert out["pending_count"] == 2
    assert (out["total"], out["offset"], out["limit"], out["next_offset"]) == (2, 0, 5, None)
    assert out["scope"] == OPEN_LINE and out["as_of"].endswith("+08:00")
    _, only = get(routes, "/api/loci/names", f"kind={quote('群')}")
    assert [r["name"] for r in only["items"]] == ["读书会"]


def test_every_list_pages_and_as_of_holds_the_pages_still(store, routes):
    for i in range(7):
        entry(store, f"新名字{i}出现了。", [f"新名字{i}"])
    status, first = get(routes, "/api/loci/names/pending", "limit=3")
    assert status == 200 and len(first["items"]) == 3
    assert (first["total"], first["next_offset"]) == (7, 3)
    _, second = get(routes, "/api/loci/names/pending",
                    f"offset=3&limit=3&as_of={quote(first['as_of'])}")
    assert second["offset"] == 3 and second["next_offset"] == 6
    assert second["as_of"] == first["as_of"]
    names_seen = [r["name"] for r in first["items"] + second["items"]]
    assert len(set(names_seen)) == 6
    assert get(routes, "/api/loci/names", "limit=x")[0] == 400
    assert get(routes, "/api/loci/names", "offset=-1")[0] == 400
    assert get(routes, "/api/loci/names", "as_of=yesterday")[0] == 400
    _, big = get(routes, "/api/loci/names", "limit=999")
    assert big["limit"] == 50


def test_as_of_leaves_out_entries_written_after_it(store, routes):
    entry(store, "阿哲今天又加班。", ["阿哲"])
    past = (datetime.now(W.LOCAL_TZ) - timedelta(days=1)).isoformat(timespec="seconds")
    _, out = get(routes, "/api/loci/names/pending", f"as_of={quote(past)}")
    assert out["total"] == 0 and out["as_of"] == past


def test_a_pending_name_comes_with_where_it_first_appeared_and_leaves_once_pressed(store, routes):
    first = entry(store, "小周说阿哲今天又加班。", ["阿哲"], when="2026-10-05")
    entry(store, "阿哲请大家吃饭。", ["阿哲"], when="2026-10-06")
    _, out = get(routes, "/api/loci/names/pending")
    [row] = out["items"]
    assert row["name"] == "阿哲" and row["n"] == 2
    assert row["first"]["id"] == first and row["first"]["short"] == first[:6]
    assert row["first"]["date"] == "2026-10-05"
    status, done = post(routes, "/api/loci/names/action",
                        {"action": "set_kind", "name": "阿哲", "kind": "人"})
    assert status == 200 and done["ok"] and done["changed"] and "人" in done["note"], done
    _, out = get(routes, "/api/loci/names/pending")
    assert out["items"] == []
    _, page = get(routes, "/api/loci/names")
    assert "阿哲" in [r["name"] for r in page["items"]]


def test_the_name_card_says_what_the_table_says_its_card_and_its_entries(store, routes):
    src = entry(store, "小林搬家了。", ["小林"], when="2026-09-01")
    note = run(store.create("小周的大学室友，在找工作，别主动问面试结果", room="MIND/TRAITS",
                            card_of="小林", prov=[{"rel": "wasDerivedFrom", "target": src}]))
    newest = entry(store, "林林面完第二轮。", ["林林"], when="2026-10-03")
    for i in range(5):
        entry(store, f"小林的第{i}件事。", ["小林"], when=f"2026-08-0{i + 1}")
    status, out = card(routes, "林林")
    assert status == 200, out
    assert (out["name"], out["kind"], out["aliases"]) == ("小林", "人", ["林林"])
    assert (out["present_in"], out["member_of"]) == ([], ["读书会"])
    assert out["card"] == {"id": note, "short": note[:6],
                           "text": "小周的大学室友，在找工作，别主动问面试结果"}
    mem = out["memories"]
    assert mem["total"] == 7 and mem["limit"] == 5 and mem["next_offset"] == 5
    assert mem["items"][0]["id"] == newest and mem["items"][0]["date"] == "2026-10-03"
    assert out["scope"] == OPEN_LINE
    _, rest = card(routes, "小林", f"offset=5&as_of={quote(mem['as_of'])}")
    assert len(rest["memories"]["items"]) == 2 and rest["memories"]["next_offset"] is None


def test_an_unknown_name_is_404_and_a_name_only_on_disk_still_has_a_card(store, routes):
    assert card(routes, "谁也不是")[0] == 404
    entry(store, "阿哲今天又加班。", ["阿哲"])
    status, out = card(routes, "阿哲")
    assert status == 200 and out["kind"] == "" and out["card"] is None
    assert out["memories"]["total"] == 1
    status, out = card(routes, "底特律")
    assert status == 200 and out["kind"] == "游戏" and out["memories"]["total"] == 0


def test_the_actions_write_the_table_only_and_refuse_another_origin(store, routes, tmp_path):
    entry(store, "阿哲今天又加班。", ["阿哲"])
    n = len(list(store.ledger_mirror.iter_events()))
    table = tmp_path / "aliases.yaml"
    before = table.read_text(encoding="utf-8")
    body = {"action": "merge", "name": "林小林", "target": "小林"}
    assert post(routes, "/api/loci/names/action", body, origin="")[0] == 403
    assert post(routes, "/api/loci/names/action", body, origin="http://evil.test")[0] == 403
    assert table.read_text(encoding="utf-8") == before
    status, out = post(routes, "/api/loci/names/action", body)
    assert status == 200 and out["changed"] is True and "别名表" in out["note"]
    assert S.canonical("林小林") == "小林"
    status, out = post(routes, "/api/loci/names/action", {"action": "rename", "name": "阿哲",
                                                          "target": "张哲"})
    assert status == 200 and S.canonical("阿哲") == "张哲"
    status, out = post(routes, "/api/loci/names/action", {"action": "not_person",
                                                          "name": "那家店"})
    assert status == 200 and S.canonical("那家店") == ""
    assert post(routes, "/api/loci/names/action", {"action": "forget", "name": "x"})[0] == 400
    assert post(routes, "/api/loci/names/action", {"action": "set_kind", "name": "他",
                                                   "kind": "人"})[0] == 400
    # The table is not a memory: the ledger did not move.
    assert len(list(store.ledger_mirror.iter_events())) == n


def test_the_old_subjects_paths_keep_working(store, routes):
    entry(store, "阿哲今天又加班。", ["阿哲"])
    status, out = get(routes, "/api/loci/subjects")
    assert status == 200 and out["names"][0]["first_bucket"]
    status, out = post(routes, "/api/loci/subjects/action",
                       {"action": "rename", "name": "阿哲", "target": "张哲"})
    assert status == 200 and out["changed"] is True


def test_the_fixed_names_paths_are_matched_before_a_name(routes):
    # Starlette matches in registration order: /names/{name} first would read "pending"
    # as a name.
    order = [path for path, _method in routes]
    assert order.index("/api/loci/names/pending") < order.index("/api/loci/names/{name}")
    assert order.index("/api/loci/names/action") < order.index("/api/loci/names/{name}")


# ───────────────────────── a name the table knows without a kind; what it looks like ──

def test_a_name_the_table_knows_without_a_kind_waits_with_the_pending(store, routes, tmp_path):
    # 老周 is in the table with an alias only, 小周 hangs in a group: neither says what it is.
    table = tmp_path / "aliases.yaml"
    table.write_text(TABLE + "老周:\n  aliases: [周叔]\n小周:\n  member_of: [读书会]\n",
                     encoding="utf-8")
    S._cache = None
    entry(store, "周叔来电话。", ["周叔"])
    entry(store, "小周说读书会改到周六。", ["小周"])
    entry(store, "小林面完第二轮。", ["小林"])
    _, page = get(routes, "/api/loci/names")
    assert [r["name"] for r in page["items"]] == ["小林"]
    assert page["kinds"] == [{"kind": "人", "n": 1}]
    assert page["pending_count"] == 2
    _, pending = get(routes, "/api/loci/names/pending")
    rows = {r["name"]: r for r in pending["items"]}
    assert set(rows) == {"老周", "小周"}
    assert rows["老周"]["in_table"] is True and rows["老周"]["guess"] is None
    # A member of a group is a person in this table.
    assert rows["小周"]["guess"] == "人"
    # Told what it is, it leaves the pending page for the names page.
    assert post(routes, "/api/loci/names/action",
                {"action": "set_kind", "name": "老周", "kind": "人"})[0] == 200
    _, pending = get(routes, "/api/loci/names/pending")
    assert [r["name"] for r in pending["items"]] == ["小周"]
    _, page = get(routes, "/api/loci/names")
    assert {r["name"] for r in page["items"]} == {"小林", "老周"}


def test_a_pending_name_carries_what_the_side_model_said_it_is(store, routes):
    from core import name_guesses as G
    entry(store, "阿哲今天又加班。", ["阿哲"])
    entry(store, "老周来电话。", ["老周"])
    G.record(store.base_dir, "阿哲", "人", W.now())
    _, pending = get(routes, "/api/loci/names/pending")
    rows = {r["name"]: r for r in pending["items"]}
    assert rows["阿哲"]["guess"] == "人" and rows["阿哲"]["in_table"] is False
    assert rows["老周"]["guess"] is None
    # Only a hash of the spelling is kept, never the name.
    raw = (Path(store.base_dir) / "_state" / G.FILE).read_text(encoding="utf-8")
    assert "阿哲" not in raw
