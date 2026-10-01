# -*- coding: utf-8 -*-
"""
tests/test_holds_and_cues.py — cues and holds (stage 4.2).

A cue says what an entry waits on; writing one is the declaration, so it needs its
condition. A hold is a short telic entry hung on a standing one by exception_of: it keeps
the original off breath's three roads while it holds, keeps an `avoid`ed one out of
dreams too, ends on its own date (on read, and on disk through the decay sweep), and is
never an item of its own on those roads.
"""

import asyncio
from datetime import datetime, timedelta
from pathlib import Path

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _dream as D
from core import _holds as H
from core import _when
from core.bucket_manager import BucketManager
from core.decay_engine import DecayEngine
from core.profile import door_note, event_pool
from tools import _runtime as rt
from tools.grow import rooms_path
from tools.trace.core import trace_core


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    monkeypatch.setenv("AI_NAME", "DT")
    monkeypatch.setenv("LOCI_OWNER_NAME", "Es")
    return tmp_path


def run(coro):
    return asyncio.run(coro)


def _meta(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return frontmatter.load(path).metadata


def _day(offset: int) -> str:
    return (_when.now() + timedelta(days=offset)).strftime("%Y-%m-%d")


def _new_ids(before: set, tmp_path) -> list[str]:
    ids = []
    for p in tmp_path.rglob("*.md"):
        bid = frontmatter.load(p).metadata.get("id")
        if bid not in before:
            ids.append(bid)
    return ids


def _all_ids(tmp_path) -> set:
    return {frontmatter.load(p).metadata.get("id") for p in tmp_path.rglob("*.md")}


ITEM = {"room": "EVENT/SELF", "text": "Go to the gym together on weekdays.", "v": 0.6, "a": 0.4}
HOLD = {"room": "EVENT/SELF", "text": "Asked not to be pushed about the gym this week.",
        "v": 0.5, "a": 0.3}


def _agreement(**fields) -> str:
    return run(rt.bucket_mgr.create(ITEM["text"], tags=["t"], direction_of_fit="telic",
                                    bound=["DT", "Es"], room="EVENT/SELF", **fields))


def _grow_hold(target, **kwargs):
    """grow a hold on `target`; returns (the tool's text, the ids it created)."""
    lib = Path(rt.bucket_mgr.base_dir)
    before = _all_ids(lib)
    out = run(grow_mod.dispatch(kind="event", items=[dict(HOLD)], exception_of=target,
                                **kwargs))
    return out, _new_ids(before, lib)


# ── cue ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("cue", [{}, {"condition": ""}, {"condition": "  "},
                                 {"phrasings": ["when the exam ends"]}])
def test_a_cue_without_a_condition_is_refused(store, cue):
    out = run(grow_mod.dispatch(kind="event", items=[ITEM], direction_of_fit="telic",
                                cue=cue))
    assert "cue 要有 condition" in out
    assert not list(store.rglob("*.md")), "nothing is written"


def test_a_want_with_neither_when_nor_cue_is_a_plain_want(store):
    out = run(grow_mod.dispatch(kind="event", items=[ITEM], direction_of_fit="telic"))
    assert "已落盘" in out
    [bid] = _all_ids(store)
    meta = _meta(store, bid)
    assert meta["direction_of_fit"] == "telic"
    assert "cue" not in meta and "when" not in meta


def test_a_cue_is_stored_whole_and_may_ride_on_a_plain_event(store):
    out = run(grow_mod.dispatch(
        kind="event", items=[dict(ITEM, room="EVENT/WORLD", text="Allergic to nuts.")],
        cue={"condition": "the next time we bake"}))
    assert "[cue]" in out
    [bid] = _all_ids(store)
    assert _meta(store, bid)["cue"] == {"condition": "the next time we bake", "phrasings": []}


def test_unknown_cue_keys_are_refused(store):
    out = run(grow_mod.dispatch(kind="event", items=[ITEM],
                                cue={"condition": "x", "when": "later"}))
    assert "cue 不认识" in out


def test_trace_sets_and_clears_a_cue(store):
    bid = run(rt.bucket_mgr.create("Return the borrowed book.", tags=["t"]))
    assert "已修改" in run(trace_core(bucket_id=bid,
                                      cue={"condition": "he is back from the trip"}))
    assert _meta(store, bid)["cue"]["condition"] == "he is back from the trip"
    assert "cue 要有 condition" in run(trace_core(bucket_id=bid, cue={"condition": ""}))
    assert "已修改" in run(trace_core(bucket_id=bid, cue=""))
    assert "cue" not in _meta(store, bid)


# ── creating a hold ──────────────────────────────────────────────────────────

def test_a_defer_without_a_date_is_telic_bound_to_the_ai_and_gets_a_review_day(store):
    target = _agreement()
    out, [hid] = _grow_hold(target, hold="defer")
    assert "📎" in out and target in out
    meta = _meta(store, hid)
    assert meta["exception_of"] == target and meta["hold"] == "defer"
    assert meta["direction_of_fit"] == "telic", "a hold is always telic"
    assert meta["bound"] == ["DT"], "a hold is about the AI's behaviour"
    assert meta["review_after"] == _day(H.DEFAULT_REVIEW_DAYS)
    assert "when" not in meta
    assert "exception_of" not in _meta(store, target), "the original is not edited"


def test_the_review_day_is_the_hosts_setting(store, monkeypatch):
    target = _agreement()
    monkeypatch.setattr(rt, "config", {"surfacing": {"hold_review_days": 3}})
    _out, [hid] = _grow_hold(target, hold="defer")
    assert _meta(store, hid)["review_after"] == _day(3)
    monkeypatch.setattr(rt, "config", {"surfacing": {"hold_review_days": 0}})
    _out, [hid2] = _grow_hold(target, hold="defer")
    assert "review_after" not in _meta(store, hid2), "0 = never ask"


def test_a_dated_defer_ends_instead_of_asking(store):
    target = _agreement()
    _out, [hid] = _grow_hold(target, hold="defer", when=f"{_day(0)}..{_day(5)}")
    meta = _meta(store, hid)
    assert meta["when"] == f"{_day(0)}..{_day(5)}"
    assert "review_after" not in meta


def test_an_avoid_has_neither_end_nor_review(store):
    target = _agreement()
    out, [hid] = _grow_hold(target, hold="avoid")
    meta = _meta(store, hid)
    assert meta["hold"] == "avoid"
    assert "when" not in meta and "review_after" not in meta
    assert "不会自己放下" in out


def test_exception_of_takes_a_short_id(store):
    target = _agreement()
    _out, [hid] = _grow_hold(target[:6], hold="defer")
    assert _meta(store, hid)["exception_of"] == target


def test_a_hold_on_a_missing_target_is_refused(store):
    out, new = _grow_hold("abcdefabcdef", hold="defer")
    assert "不存在" in out and not new


def test_a_hold_on_an_archived_target_is_refused(store):
    target = _agreement()
    assert run(rt.bucket_mgr.delete(target))
    out, new = _grow_hold(target, hold="defer")
    assert "归档区" in out and not new


def test_a_hold_on_a_superseded_target_is_refused(store):
    target = _agreement()
    newer = _agreement()
    assert run(rt.bucket_mgr.update(target, superseded_by=newer))
    out, new = _grow_hold(target, hold="defer")
    assert newer in out and not new


def test_a_hold_on_a_hold_is_refused(store):
    target = _agreement()
    _out, [hid] = _grow_hold(target, hold="defer")
    out, new = _grow_hold(hid, hold="avoid")
    assert "本身是一张条子" in out and not new


@pytest.mark.parametrize("kwargs,needle", [
    ({"hold": "later"}, "hold 只有两个值"),
    ({"hold": ""}, "挂条子要说是哪种"),
    ({"hold": "defer", "direction_of_fit": "thetic"}, "条子总是 telic"),
    ({"hold": "defer", "when": "3w"}, "条子的 when"),
    ({"hold": "defer", "when": f"{_day(0)}.."}, "条子的 when 要有止"),
    ({"hold": "defer", "when": f"{_day(5)}..{_day(0)}"}, "止早于起"),
])
def test_malformed_holds_are_refused(store, kwargs, needle):
    target = _agreement()
    out, new = _grow_hold(target, **kwargs)
    assert needle in out
    assert not new


def test_hold_needs_exception_of_and_one_item(store):
    out = run(grow_mod.dispatch(kind="event", items=[HOLD], hold="defer"))
    assert "有 hold 就要有 exception_of" in out
    target = _agreement()
    out = run(grow_mod.dispatch(kind="event", items=[HOLD, ITEM], exception_of=target,
                                hold="defer"))
    assert "一张条子一条" in out


# ── is_held ──────────────────────────────────────────────────────────────────

def _hold_meta(hid, target, level="defer", **extra):
    return {"id": hid, "exception_of": target, "hold": level,
            "direction_of_fit": "telic", "bound": ["DT"], **extra}


def test_a_span_holds_inside_its_days_and_not_before_or_after():
    rows = [{"id": "orig00000000"},
            _hold_meta("hold00000000", "orig00000000", when="2026-10-06..2026-10-11")]
    idx = H.hold_index(rows)
    at = lambda d: datetime(2026, 10, d, 10, 0)  # noqa: E731
    assert H.is_held("orig00000000", at(5), idx) is None, "not yet"
    assert H.is_held("orig00000000", at(6), idx) == "defer"
    assert H.is_held("orig00000000", at(11), idx) == "defer", "the last day still holds"
    assert H.is_held("orig00000000", at(12), idx) is None, "past the end"


def test_one_day_means_until_then():
    idx = H.hold_index([_hold_meta("hold00000000", "orig00000000", when="2026-10-11")])
    assert H.is_held("orig00000000", datetime(2026, 10, 1), idx) == "defer"
    assert H.is_held("orig00000000", datetime(2026, 10, 12), idx) is None


def test_the_strongest_live_level_wins_and_a_closed_hold_holds_nothing():
    idx = H.hold_index([
        _hold_meta("hold00000001", "orig00000000"),
        _hold_meta("hold00000002", "orig00000000", "avoid"),
        _hold_meta("hold00000003", "other0000000", "avoid", status="resolved"),
    ])
    now = datetime(2026, 10, 1)
    assert H.is_held({"id": "orig00000000"}, now, idx) == "avoid"
    assert H.is_held("other0000000", now, idx) is None


def test_a_hold_keeps_holding_after_the_agreement_is_reworded():
    rows = [{"id": "orig00000000", "superseded_by": "orig11111111"},
            {"id": "orig11111111", "supersedes": "orig00000000"},
            _hold_meta("hold00000000", "orig00000000")]
    assert H.is_held("orig11111111", datetime(2026, 10, 1), H.hold_index(rows)) == "defer"


# ── the roads ────────────────────────────────────────────────────────────────

NOW = datetime(2026, 10, 7, 10, 0)


def _bucket(bid, content="…", **meta):
    m = {"id": bid, "room": "EVENT/SELF", "tags": [],
         "created": (NOW - timedelta(days=20)).strftime("%Y-%m-%dT%H:%M:%S")}
    m.update(meta)
    return {"content": content, "metadata": m}


def _roads(rows, now=NOW):
    door = door_note(rows, now)
    return ({r["id"] for r in door["reminders"]}, {h["id"] for h in door["heavy"]},
            {e["id"] for e in event_pool(rows, now)})


@pytest.mark.parametrize("level", ["defer", "avoid"])
def test_breath_roads_skip_a_held_original_and_never_list_the_hold(level):
    rows = [
        _bucket("heavy0000000", direction_of_fit="telic", bound=["DT"]),
        _bucket("soon00000000", direction_of_fit="telic", when="2026-10-09"),
        _bucket("hold00000001", **_hold_meta("hold00000001", "heavy0000000", level,
                                             when="2026-10-06..2026-10-11")),
        _bucket("hold00000002", **_hold_meta("hold00000002", "soon00000000", level)),
    ]
    reminders, heavy, pool = _roads(rows)
    for road in (reminders, heavy, pool):
        assert not road & {"heavy0000000", "soon00000000"}, "held originals stay off"
        assert not road & {"hold00000001", "hold00000002"}, "a hold is never its own item"


def test_the_original_comes_back_when_the_hold_ends():
    rows = [_bucket("heavy0000000", direction_of_fit="telic", bound=["DT"]),
            _bucket("hold00000001", **_hold_meta("hold00000001", "heavy0000000",
                                                 when="2026-10-06..2026-10-11"))]
    _r, heavy, pool = _roads(rows, datetime(2026, 10, 13, 10, 0))
    assert "heavy0000000" in heavy and "heavy0000000" in pool
    assert "hold00000001" not in heavy and "hold00000001" not in pool


# ── dreams ───────────────────────────────────────────────────────────────────

def _rec(bid, **meta):
    m = {"id": bid, "room": "EVENT/SELF", "valence": 0.1, "arousal": 0.9,
         "created": "2026-09-01T02:00:00"}
    m.update(meta)
    return (m, f"body of {bid}")


def test_defer_leaves_dreams_alone_and_avoid_takes_the_original_out(monkeypatch):
    monkeypatch.setattr(rt, "config", {})
    now = _when.now()
    recs = [
        _rec("want00000001", direction_of_fit="telic"),
        _rec("want00000002", direction_of_fit="telic"),
        _rec("past00000001"),
        _rec("past00000002"),
        _rec("hold00000001", **_hold_meta("hold00000001", "want00000001", "defer")),
        _rec("hold00000002", **_hold_meta("hold00000002", "want00000002", "avoid")),
        _rec("hold00000003", **_hold_meta("hold00000003", "past00000001", "defer")),
        _rec("hold00000004", **_hold_meta("hold00000004", "past00000002", "avoid")),
    ]
    wanted = {x.id for x in D.want_pool(recs, now)}
    unclear = {x.id for x in D.unclear_pool(recs, set(), {}, now)}
    assert "want00000001" in wanted, "defer: still dreamt about"
    assert "want00000002" not in wanted, "avoid: out of dreams"
    assert "past00000001" in unclear and "past00000002" not in unclear
    holds = {"hold00000001", "hold00000002", "hold00000003", "hold00000004"}
    assert not (wanted | unclear) & holds, "a hold is not dream material of its own"


# ── the sweep ────────────────────────────────────────────────────────────────

def test_the_decay_sweep_closes_an_expired_dated_hold_and_nothing_else(store):
    mgr = rt.bucket_mgr
    target = _agreement()

    def hold(**fields):
        return run(mgr.create(HOLD["text"], tags=["t"], room="EVENT/SELF",
                              direction_of_fit="telic", bound=["DT"],
                              exception_of=target, **fields))
    expired = hold(hold="defer", when=f"{_day(-9)}..{_day(-2)}")
    live = hold(hold="defer", when=_day(1))
    undated = hold(hold="avoid")
    result = run(DecayEngine({}, mgr).run_decay_cycle())
    assert result["holds_expired"] == 1
    gone = _meta(store, expired)
    assert (gone["status"], gone["closed_by"]) == ("resolved", H.CLOSED_BY_EXPIRY)
    for bid in (live, undated, target):
        assert "status" not in _meta(store, bid), bid
    # Closed, it no longer stays immortal: it ages like anything else.
    assert not DecayEngine._never_decays(gone)


def test_closing_a_hold_with_trace_brings_the_original_back(store):
    target = _agreement()
    _out, [hid] = _grow_hold(target, hold="avoid")
    rows = run(rt.bucket_mgr.list_all(include_archive=False))
    assert H.is_held(target, _when.now(), H.hold_index(rows)) == "avoid"
    assert "已修改" in run(trace_core(bucket_id=hid, status="resolved"))
    rows = run(rt.bucket_mgr.list_all(include_archive=False))
    assert H.is_held(target, _when.now(), H.hold_index(rows)) is None


def test_trace_moves_a_holds_end_with_a_span(store):
    target = _agreement()
    _out, [hid] = _grow_hold(target, hold="defer", when=_day(2))
    span = f"{_day(0)}..{_day(9)}"
    assert "已修改" in run(trace_core(bucket_id=hid, when=span))
    assert _meta(store, hid)["when"] == span
    assert "条子的 when" in run(trace_core(bucket_id=hid, when="3w"))
