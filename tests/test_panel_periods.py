# -*- coding: utf-8 -*-
"""
tests/test_panel_periods.py — the regrow/fold page's 日子 / 想法 column

GET /api/loci/periods lists the periods fold drew (with their span, how many memories
fall in it, and which other periods overlap it) and the merged thoughts (their day, how
many they cover); GET /api/loci/periods/{id} one of them with its members. The five POSTs
are the owner's edits: a period renamed, moved, deleted, merged; a memory's or a thought's
day; a thought's roster. Each is told to the model once, at the end of his next tool
reply (core/_panel_changes.py).
"""

from datetime import datetime

import frontmatter
import pytest

from _panel_kit import make_store, routes, run
from core import _panel_changes as PC
from core import _when as W

GIST, BIG = "__gist__", "__大event__"


@pytest.fixture
def store(tmp_path, monkeypatch):
    return make_store(tmp_path, monkeypatch)


@pytest.fixture
def lib(store):
    async def go():
        ids = {}
        ids["a"] = await store.create("Moved the boxes.", room="EVENT/SELF", name="boxes",
                                      when="2026-10-02")
        ids["b"] = await store.create("Keys handed over.", room="EVENT/SELF", name="keys",
                                      when=datetime(2026, 10, 5, 18, 30,
                                                    tzinfo=W.LOCAL_TZ).isoformat())
        ids["c"] = await store.create("Dinner out.", room="EVENT/SELF", name="dinner",
                                      when="2026-10-09")
        ids["week"] = await store.create("搬家那周", room="EVENT/SELF", name="搬家那周",
                                         tags=[GIST, BIG], when="2026-10-01..2026-10-07")
        ids["hol"] = await store.create("国庆", room="EVENT/SELF", name="国庆",
                                        tags=[GIST, BIG], when="2026-10-01..2026-10-08")
        ids["m1"] = await store.create("She hates being kept waiting.", room="MIND/VIEWS",
                                       name="waiting")
        ids["m2"] = await store.create("She asks twice when unsure.", room="MIND/VIEWS",
                                       name="asks")
        ids["m3"] = await store.create("She goes quiet when tired.", room="MIND/VIEWS",
                                       name="quiet")
        ids["t"] = await store.create("She needs to be answered.", room="MIND/VIEWS",
                                      name="answered", tags=[GIST])
        await store.update(ids["t"], cover=[ids["m1"], ids["m2"]])
        for m in ("m1", "m2"):
            await store.update(ids[m], covered_by=[ids["t"]])
        return ids
    return run(go())


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md") if "archive" not in p.parts]
    return dict(frontmatter.load(path).metadata)


def test_the_column_lists_periods_and_thoughts(store, lib, monkeypatch):
    out = routes(monkeypatch)("GET", "/api/loci/periods").json
    periods = {p["id"]: p for p in out["periods"]}
    week = periods[lib["week"]]
    assert (week["text"], week["start"], week["end"], week["count"]) == (
        "搬家那周", "2026-10-01", "2026-10-07", 2)
    assert [o["id"] for o in week["overlaps"]] == [lib["hol"]]
    assert periods[lib["hol"]]["count"] == 2
    [t] = out["thoughts"]
    assert (t["id"], t["day"], t["count"]) == (lib["t"], "", 2)


def test_one_period_and_one_thought_with_their_members(store, lib, monkeypatch):
    call = routes(monkeypatch)
    one = "/api/loci/periods/{bucket_id}"
    week = call("GET", one, params={"bucket_id": lib["week"]}).json
    assert [(m["id"], m["day"]) for m in week["members"]] == [
        (lib["a"], "2026-10-02"), (lib["b"], "2026-10-05")]
    t = call("GET", one, params={"bucket_id": lib["t"]}).json
    assert [m["id"] for m in t["members"]] == [lib["m1"], lib["m2"]]
    assert call("GET", one, params={"bucket_id": lib["a"]}).status == 404


def test_a_period_renamed_and_moved_and_the_model_hears_it_once(store, lib, tmp_path,
                                                                 monkeypatch):
    call = routes(monkeypatch)
    r = call("POST", "/api/loci/periods/edit",
             body={"id": lib["week"], "name": "搬家", "when": "2026-09-29..2026-10-06"})
    assert r.status == 200, r.json
    disk = _disk(tmp_path, lib["week"])
    assert disk["when"] == "2026-09-29..2026-10-06" and disk["name"] == "搬家"
    bad = call("POST", "/api/loci/periods/edit",
               body={"id": lib["week"], "when": "2026-10-06..2026-10-01"})
    assert bad.status == 409 and "前面" in bad.json["error"]
    text = PC.take_text(store.base_dir, W.now(), whole_library=True, actor="阿青")
    assert text.startswith("阿青在面板上改了 2 处：")
    assert "改了名字，现在叫「搬家」" in text and "10-01..10-07 → 09-29..10-06" in text
    assert PC.take_text(store.base_dir, W.now(), whole_library=True, actor="阿青") == ""


def test_a_scoped_call_is_not_told(store, lib, monkeypatch):
    routes(monkeypatch)("POST", "/api/loci/periods/delete", body={"id": lib["hol"]})
    assert PC.take_text(store.base_dir, W.now(), whole_library=False, actor="x") == ""
    assert "删掉了（里面的记忆都没动）" in PC.take_text(store.base_dir, W.now(),
                                                       whole_library=True, actor="x")


def test_merging_takes_the_span_over_both_and_puts_one_away(store, lib, tmp_path, monkeypatch):
    call = routes(monkeypatch)
    r = call("POST", "/api/loci/periods/merge",
             body={"keep": lib["week"], "drop": lib["hol"], "name": "国庆搬家"})
    assert r.status == 200, r.json
    assert _disk(tmp_path, lib["week"])["when"] == "2026-10-01..2026-10-08"
    left = [p["id"] for p in call("GET", "/api/loci/periods").json["periods"]]
    assert left == [lib["week"]]


def test_a_members_day_moves_it_out_and_keeps_its_clock(store, lib, tmp_path, monkeypatch):
    call = routes(monkeypatch)
    r = call("POST", "/api/loci/periods/day", body={"id": lib["b"], "day": "2026-10-12"})
    assert r.status == 200, r.json
    assert str(_disk(tmp_path, lib["b"])["when"]).startswith("2026-10-12T18:30")
    week = call("GET", "/api/loci/periods/{bucket_id}", params={"bucket_id": lib["week"]}).json
    assert [m["id"] for m in week["members"]] == [lib["a"]]


def test_a_thoughts_day_is_added_and_cleared(store, lib, tmp_path, monkeypatch):
    call = routes(monkeypatch)
    assert call("POST", "/api/loci/periods/day", body={"id": lib["t"], "day": "2026-10-03"}).status == 200
    assert str(_disk(tmp_path, lib["t"])["when"]) == "2026-10-03"
    assert call("POST", "/api/loci/periods/day", body={"id": lib["t"], "day": ""}).status == 200
    assert not _disk(tmp_path, lib["t"]).get("when")
    assert call("POST", "/api/loci/periods/day", body={"id": lib["a"], "day": ""}).status == 409


def test_a_thoughts_roster_takes_one_off_and_one_on(store, lib, tmp_path, monkeypatch):
    call = routes(monkeypatch)
    r = call("POST", "/api/loci/periods/cover",
             body={"id": lib["t"], "add": [lib["m3"]], "remove": [lib["m1"]]})
    assert r.status == 200, r.json
    assert _disk(tmp_path, lib["t"])["cover"] == [lib["m2"], lib["m3"]]
    assert not _disk(tmp_path, lib["m1"]).get("covered_by")
    assert _disk(tmp_path, lib["m3"])["covered_by"] == [lib["t"]]
    last = call("POST", "/api/loci/periods/cover",
                body={"id": lib["t"], "remove": [lib["m2"], lib["m3"]]})
    assert last.status == 409


def test_the_detail_window_does_not_offer_content_on_a_period(lib):
    from core import detail as D
    assert D.edit_actions({"tags": [GIST, BIG], "room": "EVENT/SELF"}) == [D.TYPO, D.DELETE]


def test_the_next_tool_reply_carries_the_changes_once(store, lib, monkeypatch):
    import server_call as SCALL
    monkeypatch.setattr(SCALL, "bucket_mgr", store)
    monkeypatch.setattr("utils.panel_actor", lambda: "阿青")
    for n in range(12):
        PC.record(store.base_dir, f"change {n}", [], W.now())

    async def tool():
        return "recall said this"
    first = run(SCALL._run_with_notice(tool(), "recall", {}))
    assert first.startswith("recall said this\n\n阿青在面板上改了 12 处：")
    assert "· change 9" in first and "change 10" not in first
    assert "……（还有 2 处没列，都在面板 REGROW/FOLD 里）" in first
    assert run(SCALL._run_with_notice(tool(), "recall", {})) == "recall said this"


def test_a_promises_deadline_is_not_moved_from_here(store, lib, monkeypatch):
    async def go():
        return await store.create("Promised to fix the bike.", room="EVENT/SELF", name="bike",
                                  direction_of_fit="telic", bound=["AI"], when="2026-10-20")
    pid = run(go())
    r = routes(monkeypatch)("POST", "/api/loci/periods/day", body={"id": pid, "day": "2026-10-03"})
    assert r.status == 409 and "期限" in r.json["error"]
