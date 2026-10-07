# -*- coding: utf-8 -*-
"""
tests/test_panel_hanging_trace.py — the panel's trace page and its buttons

`GET /api/loci/hanging` lists only what still hangs open — promises not closed, live holds,
open cue conditions — in two halves, awake (`surface`) and asleep (`deep`), each paged on
its own; every row says why it hangs and which buttons it takes (core/profile.hanging).

`POST /api/loci/trace` is those buttons: 做完了 / 不做了 on a promise, 撤掉 on a hold or a
cue. The hanging list is computed again before anything is written, and the write goes
through tools.trace.core.trace_core with closed_by="user", the same road the model's trace
takes; the ledger records only which fields changed.
"""

from datetime import datetime, timedelta

import frontmatter
import pytest

from _panel_kit import make_store, routes, run
from core import _when as W
from core import scope as SC
from core.profile import hanging

NOW = datetime(2026, 10, 14, 10, 0, 0, tzinfo=W.LOCAL_TZ)


def day(offset: int) -> str:
    return (NOW + timedelta(days=offset)).strftime("%Y-%m-%d")


def bucket(bid, *, created_days_ago=60, room="EVENT/SELF", **meta) -> dict:
    bid = (bid * 12)[:12]
    m = {"id": bid, "room": room, "name": f"entry {bid[:1]}",
         "created": (NOW - timedelta(days=created_days_ago)).isoformat(timespec="seconds")}
    m.update(meta)
    return {"id": bid, "content": f"body {bid}", "metadata": m}


def owed(bid, **meta):
    return bucket(bid, direction_of_fit="telic", bound=["AI"], **meta)


def test_three_kinds_two_halves_each_with_why_and_its_buttons():
    p1 = owed("p", created_days_ago=5)
    rows = [
        p1,
        owed("q", when=day(90), created_days_ago=6),
        owed("r", cue={"condition": "考试结束"}, created_days_ago=7),
        owed("h", exception_of=p1["id"], hold="defer", when=day(3), created_days_ago=8),
        owed("i", exception_of=p1["id"], hold="avoid", created_days_ago=9),
        owed("j", exception_of=p1["id"], hold="defer", review_after=day(4), created_days_ago=10),
        bucket("c", cue={"condition": "下次见到小林"}),
        bucket("d", cue={"condition": "closed already"}, status="resolved"),
        bucket("w", direction_of_fit="telic", weight=0.9),       # a wish nobody owes
        owed("x", status="abandoned"),                           # closed
        owed("y", exception_of=p1["id"], hold="defer", when=day(-1)),   # an expired hold
    ]
    halves = hanging(rows, NOW)
    surface = {r["id"][:1]: r for r in halves["surface"]}
    deep = {r["id"][:1]: r for r in halves["deep"]}
    assert [r["id"][:1] for r in halves["surface"]] == ["p", "q", "r", "h", "i", "j"]
    assert list(deep) == ["c"]
    assert surface["p"]["kind"] == "promise" and surface["p"]["actions"] == ["done", "drop"]
    assert surface["p"]["why_words"] == "答应了，没定时间（挂了 5 天）"
    assert surface["q"]["why_words"] == "还有 90 天"
    assert surface["r"]["actions"] == ["done", "drop", "withdraw"]
    assert surface["r"]["why_words"].endswith("；在等「考试结束」")
    h = surface["h"]
    assert (h["kind"], h["on"], h["hold"], h["hold_words"], h["actions"]) == (
        "hold", p1["id"], "defer", "先别催", ["withdraw"])
    assert h["why_words"] == f"条子到 {day(3)[5:]}"
    assert surface["i"]["why_words"] == "条子，没写到哪天" and surface["i"]["hold_words"] == "别碰"
    assert surface["j"]["why_words"] == f"条子，{day(4)[5:]} 问一次还放不放"
    c = deep["c"]
    assert (c["kind"], c["why_words"], c["actions"], c["condition"]) == (
        "cue", "在等「下次见到小林」", ["withdraw"], "下次见到小林")
    assert c["short"] == c["id"][:6] and c["text"] == "entry c"


# ── the route and its buttons ───────────────────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = make_store(tmp_path, monkeypatch)
    from web import _shared as sh
    # Nothing is awake for being new, so an entry waiting on a cue sleeps.
    sh.config["surfacing"] = {"awake_recent_days": 0}
    return mgr


@pytest.fixture
def seeded(store):
    async def go():
        promise = await store.create("Promised to fix the bike.", room="EVENT/SELF", name="bike",
                                     direction_of_fit="telic", bound=["AI"])
        other = await store.create("Promised to call the bank.", room="EVENT/SELF", name="bank",
                                   direction_of_fit="telic", bound=["AI"])
        hold = await store.create("Not now about the bike.", room="EVENT/SELF", name="not now",
                                  direction_of_fit="telic", bound=["AI"], exception_of=promise,
                                  hold="defer", when=(W.now() + timedelta(days=3)).strftime("%Y-%m-%d"))
        cue = await store.create("Ask how the exams went.", room="EVENT/SELF", name="exams",
                                 cue={"condition": "考试结束"})
        return {"promise": promise, "other": other, "hold": hold, "cue": cue}
    return run(go())


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _last_update(store, bid) -> dict:
    return [e for e in store.ledger_mirror.iter_events()
            if e["trace_id"] == bid and e["event_type"] == "TraceUpdated"][-1]


def test_the_halves_page_on_their_own(store, seeded, monkeypatch):
    call = routes(monkeypatch)
    surface = call("GET", "/api/loci/hanging", "part=surface&limit=2").json
    assert surface["part"] == "surface" and surface["total"] == 3
    assert len(surface["items"]) == 2 and surface["next_offset"] == 2
    assert surface["scope"] == SC.OPEN_LINE
    rest = call("GET", "/api/loci/hanging", "part=surface&offset=2&limit=2").json
    ids = {r["id"] for r in surface["items"] + rest["items"]}
    assert ids == {seeded["promise"], seeded["other"], seeded["hold"]}
    deep = call("GET", "/api/loci/hanging", "part=deep").json
    assert [r["id"] for r in deep["items"]] == [seeded["cue"]] and deep["next_offset"] is None
    assert call("GET", "/api/loci/hanging", "part=middle").status == 400


def test_an_empty_library_hangs_nothing(store, monkeypatch):
    r = routes(monkeypatch)("GET", "/api/loci/hanging", "part=deep").json
    assert r["items"] == [] and r["total"] == 0 and r["next_offset"] is None


@pytest.fixture
def spy(monkeypatch):
    from tools.trace import core as TC
    calls = []
    real = TC.trace_core

    async def watched(**kw):
        calls.append(kw)
        return await real(**kw)
    monkeypatch.setattr(TC, "trace_core", watched)
    return calls


def test_done_and_drop_close_a_promise_through_trace_core_as_the_user(store, seeded, tmp_path,
                                                                      monkeypatch, spy):
    call = routes(monkeypatch)
    r = call("POST", "/api/loci/trace", body={"id": seeded["promise"], "action": "done"})
    assert r.status == 200 and r.json["ok"] and r.json["action"] == "done" and r.json["msg"]
    assert spy == [{"bucket_id": seeded["promise"], "status": "resolved", "closed_by": "user"}]
    disk = _disk(tmp_path, seeded["promise"])
    assert disk["status"] == "resolved" and disk["closed_by"] == "user"
    line = _last_update(store, seeded["promise"])
    assert {"status", "closed_by"} <= set(line["payload"]["changed_fields"])

    r = call("POST", "/api/loci/trace", body={"id": seeded["other"], "action": "drop"})
    assert r.status == 200
    assert _disk(tmp_path, seeded["other"])["status"] == "abandoned"
    assert spy[-1] == {"bucket_id": seeded["other"], "status": "abandoned", "closed_by": "user"}
    # Closed, it hangs no more: the same button again finds nothing to close.
    again = call("POST", "/api/loci/trace", body={"id": seeded["promise"], "action": "done"})
    assert again.status == 404 and len(spy) == 2


def test_withdraw_lifts_a_hold_and_takes_a_cue_off(store, seeded, tmp_path, monkeypatch, spy):
    call = routes(monkeypatch)
    r = call("POST", "/api/loci/trace", body={"id": seeded["hold"], "action": "withdraw"})
    assert r.status == 200
    assert spy[-1] == {"bucket_id": seeded["hold"], "status": "resolved", "closed_by": "user"}
    hold = _disk(tmp_path, seeded["hold"])
    assert hold["status"] == "resolved" and hold["closed_by"] == "user"
    assert not _disk(tmp_path, seeded["promise"]).get("status"), "the agreement itself stays open"

    r = call("POST", "/api/loci/trace", body={"id": seeded["cue"], "action": "withdraw"})
    assert r.status == 200
    assert spy[-1] == {"bucket_id": seeded["cue"], "cue": ""}
    assert not _disk(tmp_path, seeded["cue"]).get("cue")
    assert "cue" in _last_update(store, seeded["cue"])["payload"]["changed_fields"]


def test_only_a_hanging_id_with_a_button_its_row_offers(store, seeded, monkeypatch, spy):
    call = routes(monkeypatch)
    assert call("POST", "/api/loci/trace",
                body={"id": seeded["promise"], "action": "withdraw"}).status == 409
    assert call("POST", "/api/loci/trace",
                body={"id": seeded["cue"], "action": "done"}).status == 409
    assert call("POST", "/api/loci/trace",
                body={"id": "0123456789ab", "action": "done"}).status == 404
    assert call("POST", "/api/loci/trace", body={"id": seeded["promise"],
                                                 "action": "delete"}).status == 400
    assert call("POST", "/api/loci/trace", body={"action": "done"}).status == 400
    assert spy == []


def test_a_write_without_same_origin_or_json_is_refused(store, seeded, tmp_path, monkeypatch, spy):
    call = routes(monkeypatch)
    body = {"id": seeded["promise"], "action": "done"}
    assert call("POST", "/api/loci/trace", body=body, origin=False).status == 403
    assert call("POST", "/api/loci/trace", body=body, ctype="text/plain").status == 400
    assert routes(monkeypatch, locked=True)("POST", "/api/loci/trace", body=body).status == 401
    assert spy == [] and not _disk(tmp_path, seeded["promise"]).get("status")


def test_the_old_close_route_still_works(store, seeded, tmp_path, monkeypatch):
    r = routes(monkeypatch)("POST", "/api/loci/want/resolve",
                            body={"id": seeded["promise"], "status": "resolved"})
    assert r.status == 200 and _disk(tmp_path, seeded["promise"])["closed_by"] == "user"
