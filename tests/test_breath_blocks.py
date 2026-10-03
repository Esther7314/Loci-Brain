# -*- coding: utf-8 -*-
"""
tests/test_breath_blocks.py — breath's blocks: 惦记的事, 忽然想起, 依据变了的.

惦记的事 (core/profile.prospective) is one list chosen from what is awake: every line says
why it is here now, with two kinds of reason (dated, undated); overdue comes first by how
far overdue, then the nearest dates, then undated promises longest-hanging first; five
lines, the rest counted. Off the list: a promise waiting on a cue, a wish nobody owes,
anything a live hold is on — except the one question a hold's review day asks, once.

忽然想起 (core/profile.involuntary): one older event linked to the last few days by a name
or a scene word, one at random, each saying how it came up; nothing held, nothing wanted.

依据变了的 (core/_invalidation.py): panel corrections, overturned bases one layer at a
time, sources revised / withdrawn / deleted — never the body of an entry standing on a
withdrawn source. Three ways out: regrow, archive, or the confirm gesture on trace, which
reaches disk as `confirmed_at` on each record.

And the screen is one object with two skins: the text is rendered from the object the
JSON route hands out, so the two cannot drift.
"""

import asyncio
import json
import random
from datetime import datetime, timedelta

import frontmatter
import pytest

from core import _invalidation as I
from core import _sources as S
from core import _when as W
from core import visibility as V
from core.bucket_manager import BucketManager
from core.profile import BreathSettings, due_now, involuntary, is_accessible, prospective
from tools import _runtime as rt
from tools.breath import awaken as A
from tools.grow import rooms_path
from tools.recall import core as R
from tools.regrow import dispatch as regrow
from tools.trace import dispatch as trace

NOW = datetime(2026, 10, 14, 10, 0, 0, tzinfo=W.LOCAL_TZ)


def day(offset: int) -> str:
    return (NOW + timedelta(days=offset)).strftime("%Y-%m-%d")


def bucket(bid, content="", *, created_days_ago=60, room="EVENT/SELF", **meta) -> dict:
    bid = (bid * 12)[:12]
    m = {"id": bid, "room": room,
         "created": (NOW - timedelta(days=created_days_ago)).isoformat(timespec="seconds")}
    m.update(meta)
    return {"id": bid, "content": content or f"entry {bid}", "metadata": m}


def owed(bid, **meta):
    return bucket(bid, direction_of_fit="telic", bound=["AI"], **meta)


def ids(items):
    return [i["id"][:1] for i in items]


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


def run(coro):
    return asyncio.run(coro)


# ── 惦记的事: order, reasons, the cut ───────────────────────────────────────

def test_overdue_first_by_days_then_nearest_then_undated_longest_hanging_and_the_sixth_is_counted():
    rows = [
        owed("f", created_days_ago=5),                           # undated, hanging 5 days
        owed("b", when=day(-2), weight=1.0),                     # overdue 2, the heavier one
        bucket("c", direction_of_fit="telic", when=day(1)),      # a wish, dated tomorrow
        owed("e", created_days_ago=50),                          # undated, hanging 50 days
        owed("a", when=day(-10), weight=0.1),                    # overdue 10, light
        bucket("d", room="EVENT/WORLD", when=day(5), created_days_ago=1),   # heard about the future
    ]
    p = prospective(rows, NOW)
    # Criterion: overdue by how far overdue, never by weight; dated before undated; the
    # undated by how long they have hung; five lines and the sixth counted, not dropped.
    assert ids(p["items"]) == ["a", "b", "c", "d", "e"]
    assert p["more"] == 1
    text = A._prospective_lines({**p, "slices_pending": 0})
    assert any("还有 1 条" in line for line in text)


def test_the_two_kinds_of_reason_and_who_owes_it():
    rows = [owed("a", when=day(-3)), owed("b", when=day(0)), owed("c", when=day(2)),
            owed("d", when=day(20)), owed("e", created_days_ago=4), owed("f", created_days_ago=40)]
    p = prospective(rows, NOW, settings=BreathSettings(prospective_lines=10))
    reasons = {i["id"][:1]: i["reason"] for i in p["items"]}
    assert reasons["a"] == {"kind": "dated", "days": -3, "date": day(-3), "loud": "overdue"}
    assert (reasons["b"]["loud"], reasons["c"]["loud"], reasons["d"]["loud"]) == ("now", "soon", "far")
    assert reasons["e"] == {"kind": "undated", "held": 4, "ask": "set_time"}
    assert reasons["f"] == {"kind": "undated", "held": 40, "ask": "still_counts"}
    text = "\n".join(A._prospective_lines({**p, "slices_pending": 0}))
    for words in ("过了 3 天", "就是今天", "还有 2 天", "还有 20 天", "没定时间", "定个时间或条件",
                  "挂了 40 天——还算数吗", "（我欠着）"):
        assert words in text, words
    assert "AI欠着" not in text, "the AI reads its own debt as 我"
    assert all(i["bound"] == ["AI"] for i in p["items"]), "the object keeps the stored names"


def test_off_the_list_a_cue_waiting_promise_a_light_wish_and_anything_held():
    held = owed("h")
    rows = [
        owed("q", cue={"condition": "after the exams"}),         # waits for the card
        bucket("w", direction_of_fit="telic", weight=0.9),       # nobody owes it, no date
        held,
        bucket("x", direction_of_fit="telic", bound=["AI"], exception_of=held["id"], hold="defer",
               when=f"{day(-1)}..{day(3)}", created_days_ago=1),
        bucket("t", when=day(0), created_days_ago=0),            # written today about today: a record
        owed("z", when=day(-4), status="resolved"),              # closed
    ]
    p = prospective(rows, NOW)
    assert p["items"] == [] and p["more"] == 0


def test_a_promise_with_a_cue_and_a_date_is_dated():
    p = prospective([owed("q", cue={"condition": "after the exams"}, when=day(3))], NOW)
    assert ids(p["items"]) == ["q"]


def test_a_yearly_day_and_a_length_say_so():
    birthday = (NOW + timedelta(days=4)).replace(year=2019).strftime("%Y-%m-%d")
    rows = [bucket("y", room="EVENT/WORLD", when=birthday, recurrence="FREQ=YEARLY"),
            bucket("l", direction_of_fit="telic", when="3w", created_days_ago=10)]
    p = prospective(rows, NOW)
    r = {i["id"][:1]: i["reason"] for i in p["items"]}
    assert r["y"]["days"] == 4 and r["y"]["yearly"]
    assert r["l"]["days"] == 11 and r["l"]["length"] == "3w"


def test_a_backfilled_date_says_so_in_its_first_days_on_the_list():
    fresh = owed("n", when=day(5), backfilled=["when", "name"], created_days_ago=1)
    seasoned = owed("o", when=day(5), backfilled=["when"], created_days_ago=10)
    p = prospective([fresh, seasoned], NOW)
    marked = {i["id"][:1]: bool(i["reason"].get("backfilled")) for i in p["items"]}
    assert marked == {"n": True, "o": False}
    assert "日子是补的" in "\n".join(A._prospective_lines({**p, "slices_pending": 0}))


def test_a_looks_like_promise_entry_is_a_question_below_the_items_at_most_two():
    rows = [bucket(c, looks_like_promise=True, created_days_ago=n)
            for c, n in (("p", 1), ("r", 2), ("s", 3))]
    rows.append(bucket("u", looks_like_promise=True, last_asked=day(-1)))   # asked already
    rows.append(owed("a", when=day(1)))
    p = prospective(rows, NOW)
    assert ids(p["items"]) == ["a"]
    assert ids(p["questions"]) == ["p", "r"]
    lines = A._prospective_lines({**p, "slices_pending": 0})
    q = [n for n, line in enumerate(lines) if "这条像是答应过的" in line]
    assert len(q) == 2 and q[0] > 0


# ── a clock time later today waits for its time ─────────────────────────────

def at(hour: int, minute: int = 0) -> datetime:
    return NOW.replace(hour=hour, minute=minute)


TONIGHT = f"{day(0)}T19:00+08:00"


def test_a_promise_for_tonight_waits_all_day_and_is_due_once_its_time_has_passed():
    rows = [owed("t", when=TONIGHT, created_days_ago=0)]
    assert prospective(rows, at(14))["items"] == []
    [it] = prospective(rows, at(19, 10))["items"]
    assert it["reason"]["days"] == 0 and it["reason"]["loud"] == "now"
    # Still awake all day: only the roads that list it on breath wait.
    assert is_accessible(rows[0]["metadata"], at(14))


def test_a_day_alone_counts_from_the_morning_and_a_later_day_is_unchanged():
    morning = at(8)
    p = prospective([owed("d", when=day(0)), owed("f", when=f"{day(2)}T19:00+08:00")], morning)
    assert {i["id"][:1]: i["reason"]["days"] for i in p["items"]} == {"d": 0, "f": 2}


def test_every_breath_road_waits_and_the_lookup_roads_do_not():
    meta = owed("t", when=TONIGHT)["metadata"]
    idx = V._H.hold_index([])
    for road in (V.PROSPECTIVE, V.REVIEW, V.RECENT, V.SUDDEN, V.EDITED, V.INVALIDATION):
        verdict = V.visible_for(meta, road=road, now=at(14), holds=idx)
        assert V.LATER_TODAY in verdict.reasons, road
        assert V.LATER_TODAY not in V.visible_for(meta, road=road, now=at(19), holds=idx).reasons
    for road in (V.LIST, V.READ):
        assert V.visible_for(meta, road=road, now=at(14))
    # Closed, there is nothing to wait for.
    assert not V.waits_for_clock({**meta, "status": "resolved"}, at(14))


def test_due_now_is_the_moment_the_wait_ends():
    meta = owed("t", when=TONIGHT)["metadata"]
    assert not due_now(meta, at(14))
    assert due_now(meta, at(19)) and due_now(meta, at(22))
    assert not due_now({**meta, "status": "resolved"}, at(20))
    assert not due_now(owed("d", when=day(0))["metadata"], at(20))   # no clock to come due


# ── the review question, asked once (with a store: the stamp reaches disk) ──

@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _later(monkeypatch, days):
    real = W.now()
    monkeypatch.setattr(W, "now", lambda: real + timedelta(days=days))


def test_a_review_day_asks_once_with_the_hold_and_the_original_together(store, tmp_path, monkeypatch):
    review = (W.now() + timedelta(days=7)).strftime("%Y-%m-%d")

    async def seed():
        original = await store.create("Find a moment to talk about the hospital stay.",
                                      room="EVENT/SELF", direction_of_fit="telic", bound=["AI"])
        hold = await store.create("Not now, she said; leave it for a while.", room="EVENT/SELF",
                                  direction_of_fit="telic", bound=["AI"], exception_of=original,
                                  hold="defer", review_after=review)
        return original, hold
    original, hold = run(seed())

    _later(monkeypatch, 3)
    inside = run(A.build_breath())
    assert inside["prospective"]["items"] == []          # held, and not yet its day

    _later(monkeypatch, 8)
    first = run(A.build_breath())
    [line] = first["prospective"]["items"]
    assert line["kind"] == "review" and line["id"] == original and line["hold"]["id"] == hold
    text = A.render_breath(first)
    asked = [ln for ln in text.splitlines() if original[:6] in ln]
    # Criterion: the original shows only inside the question, with its hold beside it.
    assert asked and all("现在还放着吗" in ln and hold[:6] in ln for ln in asked)
    run(A.stamp_asked(first))
    assert _disk(tmp_path, hold).get("last_asked")
    assert not _disk(tmp_path, hold).get("status")          # asking lifts nothing

    second = run(A.build_breath())
    assert second["prospective"]["items"] == []
    assert original[:6] not in A.render_breath(second)


def test_a_question_asked_through_the_tool_is_stamped(store, tmp_path, monkeypatch):
    bid = run(store.create("I will bring the umbrella back next time.", room="EVENT/SELF",
                           looks_like_promise=True))
    first = run(A.surface_awaken())
    assert "这条像是答应过的" in first and bid[:6] in first
    assert _disk(tmp_path, bid).get("last_asked")
    assert "这条像是答应过的" not in run(A.surface_awaken())


def test_waiting_slices_get_one_line(store, monkeypatch):
    monkeypatch.setattr(store.slices, "pending_count", lambda: 3)
    text = A.render_breath(run(A.build_breath()))
    assert "有 3 段原话切片等着认领" in text and 'recall(view="slices")' in text


# ── 忽然想起 ────────────────────────────────────────────────────────────────

def test_one_linked_to_the_last_days_one_random_each_saying_how_and_nothing_held_or_wanted():
    recent = bucket("n", created_days_ago=1, subjects=["小丑牌"])
    linked = bucket("l", created_days_ago=30, subjects=["小丑牌"])
    plain = bucket("p", created_days_ago=40)
    wanted = bucket("w", created_days_ago=40, direction_of_fit="telic", subjects=["小丑牌"])
    held = bucket("h", created_days_ago=40, subjects=["小丑牌"])
    hold = bucket("x", created_days_ago=2, direction_of_fit="telic", bound=["AI"],
                  exception_of=held["id"], hold="avoid")
    rows = [recent, linked, plain, wanted, held, hold]
    for seed in range(12):
        got = involuntary(rows, NOW, rng=random.Random(seed))
        assert [(g["id"][:1], g["how"]) for g in got] == [("l", "linked"), ("p", "random")]
        assert got[0]["why"] == "因为最近提到小丑牌" and got[1]["why"] == "随手翻到的"


def test_with_nothing_to_link_both_are_random():
    rows = [bucket("n", created_days_ago=1, subjects=["卡牌"]),
            bucket("a", created_days_ago=30), bucket("b", created_days_ago=40)]
    got = involuntary(rows, NOW, rng=random.Random(1))
    assert sorted(g["id"][:1] for g in got) == ["a", "b"]
    assert {g["why"] for g in got} == {"随手翻到的"}


def test_coming_up_warms_nothing(store, monkeypatch):
    async def go():
        bid = await store.create("The lighthouse at the end of the pier.", room="EVENT/SELF")
        before = dict((await store.get(bid))["metadata"])
        _later(monkeypatch, 30)
        text = await A.surface_awaken()
        return bid, before, dict((await store.get(bid))["metadata"]), text
    bid, before, after, text = run(go())
    assert bid[:6] in text.split("═══ 忽然想起 ═══")[1]
    for key in ("last_active", "activation_count", "last_asked"):
        assert after.get(key) == before.get(key), key


# ── 依据变了的 ──────────────────────────────────────────────────────────────

ROOT = "Saturday we went up the hill."
CHILD = "Tired days are the days they want to go out."
GRANDCHILD = "Ask them out at the weekend without waiting to be asked."


def _derived(*ids_):
    return [{"rel": "wasDerivedFrom", "target": i} for i in ids_]


async def _family(store):
    root = await store.create(ROOT, room="EVENT/SELF")
    child = await store.create(CHILD, room="MIND/TRAITS", prov=_derived(root))
    grandchild = await store.create(GRANDCHILD, room="MIND/VIEWS", prov=_derived(child))
    out = await regrow(bucket_id=root, mode="overturn", text="We stayed in and watched films.",
                       v=0.5, a=0.3)
    assert "已标上" in out
    return root, child, grandchild


def _carded(store):
    return [i["id"] for i in I.block(run(store.list_all()), store.sources)]


def test_an_overturn_cards_one_layer_at_a_time_and_the_confirm_gesture_moves_it_on(store, tmp_path):
    _root, child, grandchild = run(_family(store))
    # Criterion (E5): the child is carded, the grandchild — marked already — waits.
    assert _carded(store) == [child]
    assert I.open_records(_disk(tmp_path, grandchild), I.OVERTURN)

    out = run(trace(bucket_id=child[:6], invalidation="confirmed"))
    assert "确认照留" in out
    recs = _disk(tmp_path, child)["invalidation"]
    assert recs and all(r.get("confirmed_at") == W.now().date().isoformat() for r in recs)
    assert {"kind", "of", "by", "at"} <= set(recs[0])
    # Dealt with: the child leaves, its child comes up.
    assert _carded(store) == [grandchild]
    read = run(R.recall_core(when="", room="", tag="", query=child))
    assert "确认照留" in read and "依据变过" in read and "⚠️依据变了" not in read


def test_a_regrown_or_archived_parent_counts_as_dealt_with(store, tmp_path):
    _root, child, grandchild = run(_family(store))
    run(regrow(bucket_id=child, mode="supplement", text=CHILD + " Mostly.", v=0.5, a=0.3))
    newer = _disk(tmp_path, child)["superseded_by"]
    assert "invalidation" not in _disk(tmp_path, newer)      # regrow carries no mark
    assert _carded(store) == [grandchild]

    mark = [{"kind": "overturn", "of": "x" * 12, "by": "y" * 12}]
    parent = run(store.create(CHILD + " Again.", room="MIND/TRAITS"))
    gc = run(store.create(GRANDCHILD + " Again.", room="MIND/VIEWS", prov=_derived(parent)))
    assert run(store.update(parent, invalidation=mark)) and run(store.update(gc, invalidation=mark))
    assert parent in _carded(store) and gc not in _carded(store)
    assert run(store.archive(parent))
    assert gc in _carded(store)


def test_confirming_with_nothing_open_is_refused_and_writes_nothing(store, tmp_path):
    bid = run(store.create("Nothing changed under this one.", room="EVENT/SELF"))
    out = run(trace(bucket_id=bid, invalidation="confirmed"))
    assert "没有待看的依据变化" in out and "invalidation" not in _disk(tmp_path, bid)
    assert "只认" in run(trace(bucket_id=bid, invalidation="yes"))


SRC = {"system": "lento", "instance": "home", "container": "private:U"}


def test_a_withdrawn_source_hands_back_ids_and_states_never_the_body(store, tmp_path):
    body = "The thing she said in the night, word for word."
    bid = run(store.create(body, room="EVENT/WORLD",
                           sources=[{**SRC, "id": "m_0001"}, {**SRC, "id": "m_0002"}]))
    run(store.sources.apply_change({"change_id": "w1", "kind": "withdrawn", "host_seq": 1,
                                    "source": "lento:home/private:U#m_0001"}))
    b = run(A.build_breath())
    [it] = b["invalidation"]["items"]
    assert it["id"] == bid and it["text"] is None
    assert it["failed"] == [{"source": "lento:home/private:U#m_0001", "state": "withdrawn"}]
    assert it["remaining"] == ["lento:home/private:U#m_0002"]
    block = A.render_breath(b).split("═══ 依据变了的 ═══")[1]
    assert bid[:6] in block and "已撤回" in block and "只凭它们重写" in block
    assert "word for word" not in block and "night" not in block
    # Keeping it as it is would keep using what may not be used.
    out = run(trace(bucket_id=bid, invalidation="confirmed"))
    assert "照留不行" in out and "invalidation" not in _disk(tmp_path, bid)


def test_a_revised_source_is_carded_and_confirming_records_the_revision(store, tmp_path):
    bid = run(store.create("A note about the plan.", room="EVENT/WORLD",
                           sources=[{**SRC, "id": "m_0003", "revision": "1"}]))
    run(store.sources.apply_change({"change_id": "r1", "kind": "revised", "host_seq": 1,
                                    "source": "lento:home/private:U#m_0003", "revision": "2"}))
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["text"] and it["revised"] == [{"source": "lento:home/private:U#m_0003",
                                             "revision": "2"}]
    run(trace(bucket_id=bid, invalidation="confirmed"))
    [rec] = _disk(tmp_path, bid)["invalidation"]
    assert rec["kind"] == I.SOURCE_REVISED and rec["by"] == "2" and rec["confirmed_at"]
    assert I.block(run(store.list_all()), store.sources) == []


def _revise(store, cid, seq, line, revision):
    run(store.sources.apply_change({"change_id": cid, "kind": "revised", "host_seq": seq,
                                    "source": f"lento:home/private:U#{line}",
                                    "revision": revision}))


def test_a_line_revised_inside_a_run_cards_the_memory_until_confirmed_per_revision(
        store, tmp_path):
    store.sources.record_order(SRC, [f"m_{i:04d}" for i in range(1, 11)])
    bid = run(store.create("A talk about the trip.", room="EVENT/WORLD",
                           sources=[{**SRC, "id": "m_0002", "through": "m_0006",
                                     "fingerprint": "sha256:run", "fingerprint_by": "loci"}]))
    _revise(store, "out", 1, "m_0008", "2")                 # outside the run
    _revise(store, "out2", 1, "m_0001", "2")                # just before it
    assert I.block(run(store.list_all()), store.sources) == []
    from core import _cue
    before = _cue.fingerprint(_disk(tmp_path, bid), "x", registry=store.sources)
    _revise(store, "mid", 1, "m_0004", "2")                 # a line in the middle
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["id"] == bid and it["text"]
    assert it["revised"] == [{"source": "lento:home/private:U#m_0004", "revision": "2"}]
    assert _cue.fingerprint(_disk(tmp_path, bid), "x", registry=store.sources) != before, \
        "the card's version moves with it (the same findings)"
    run(trace(bucket_id=bid, invalidation="confirmed"))
    [rec] = _disk(tmp_path, bid)["invalidation"]
    assert (rec["of"], rec["by"]) == ("lento:home/private:U#m_0004", "2") and rec["confirmed_at"]
    assert I.block(run(store.list_all()), store.sources) == []
    _revise(store, "mid2", 2, "m_0004", "3")                # the same line again
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["revised"] == [{"source": "lento:home/private:U#m_0004", "revision": "3"}]
    run(trace(bucket_id=bid, invalidation="confirmed"))
    _revise(store, "last", 1, "m_0006", "2")                # another line of the run
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["revised"] == [{"source": "lento:home/private:U#m_0006", "revision": "2"}]


def test_the_counter_example_read_e1_notice_e2_then_written_on_e1_is_carded(store, tmp_path):
    # The other team's order: the model reads m_0003 at e1, the e2 notice arrives, then the
    # model writes a memory resting on e1. Write time proves nothing; the adopted revision
    # registered under the delivery's watermark does.
    reg = store.sources
    ids = [f"m_{i:04d}" for i in range(1, 11)]
    assert reg.record_order(SRC, ids, revision="w-1",
                            revisions={i: "e1" for i in ids}) == S.RECORDED
    _revise(store, "e2", 1, "m_0003", "e2")                 # arrives before the write
    rec = {**SRC, "id": "m_0002", "through": "m_0004", "revision": "w-1"}
    refusal, notes = reg.check_writable(S.normalize_sources([rec]))
    assert not refusal and any("m_0003 宿主已经出了新版本 e2（你依据的是 e1）" in n
                               for n in notes), notes
    run(store.create("Written from what was read at e1.", room="EVENT/WORLD", sources=[rec]))
    [it] = I.block(run(store.list_all()), reg)
    assert it["revised"] == [{"source": "lento:home/private:U#m_0003", "revision": "e2"}]
    run(trace(bucket_id=it["id"], invalidation="confirmed"))  # per (line, revision)
    assert I.block(run(store.list_all()), reg) == []


def test_a_line_adopted_at_its_newest_revision_is_not_carded(store, tmp_path):
    reg = store.sources
    ids = [f"m_{i:04d}" for i in range(1, 11)]
    _revise(store, "e2", 1, "m_0003", "e2")
    reg.record_order(SRC, ids, revision="w-2", revisions={**{i: "e1" for i in ids},
                                                          "m_0003": "e2"})
    run(store.create("Written from what was read at e2.", room="EVENT/WORLD",
                     sources=[{**SRC, "id": "m_0002", "through": "m_0004", "revision": "w-2"}]))
    assert I.block(run(store.list_all()), reg) == []
    _revise(store, "e3", 2, "m_0003", "e3")                 # a later one does
    [it] = I.block(run(store.list_all()), reg)
    assert it["revised"] == [{"source": "lento:home/private:U#m_0003", "revision": "e3"}]


def test_an_unknown_adopted_revision_is_pending_review_whenever_it_was_written(store, tmp_path):
    # No watermark on the record (or none registered with line revisions): which version
    # was read is unknown, so any announced revision cards it, before the write or after.
    reg = store.sources
    reg.record_order(SRC, [f"m_{i:04d}" for i in range(1, 11)])
    _revise(store, "early", 1, "m_0003", "2")
    run(store.create("Written from the revised lines.", room="EVENT/WORLD",
                     sources=[{**SRC, "id": "m_0002", "through": "m_0004"}]))
    [it] = I.block(run(store.list_all()), reg)
    assert it["revised"] == [{"source": "lento:home/private:U#m_0003", "revision": "2"}]


def test_one_watermark_cannot_give_a_line_two_revisions(store):
    reg = store.sources
    ids = ["m_0001", "m_0002", "m_0003"]
    assert reg.record_order(SRC, ids, revision="w", revisions={"m_0002": "e1"}) == S.RECORDED
    assert reg.record_order(SRC, ids, revision="w", revisions={"m_0002": "e1"}) == S.KNOWN
    why = reg.record_order(SRC, ids, revision="w", revisions={"m_0002": "e2"})
    assert why not in (S.RECORDED, S.KNOWN) and "m_0002" in why
    assert reg.adopted_revisions({**SRC, "id": "m_0001", "through": "m_0003",
                                  "revision": "w"}) == {"m_0002": "e1"}


def test_a_panel_correction_is_carded_until_folded_or_confirmed(store, tmp_path):
    bid = run(store.create("Actually it was Tuesday.", room="EVENT/WORLD", tags=["人改的"]))
    [it] = I.block(run(store.list_all()), store.sources)
    assert it["id"] == bid and it["edited"]
    run(trace(bucket_id=bid, invalidation="confirmed"))
    assert I.block(run(store.list_all()), store.sources) == []
    assert "人改的" in _disk(tmp_path, bid)["tags"]          # the mark of the edit stays


def test_confirmed_at_reaches_disk_through_update(store, tmp_path):
    # Rule 8: a new record key goes through the normaliser and the pass-through list.
    bid = run(store.create("x", room="EVENT/SELF"))
    assert run(store.update(bid, invalidation=[{"kind": "overturn", "of": "a" * 12,
                                                "by": "b" * 12, "at": "2026-10-01",
                                                "confirmed_at": "2026-10-02"}]))
    assert _disk(tmp_path, bid)["invalidation"][0]["confirmed_at"] == "2026-10-02"


# ── one object, two skins ───────────────────────────────────────────────────

def test_the_text_is_rendered_from_the_object_the_json_hands_out(store):
    async def seed():
        await store.create("She is moving house on the 20th.", room="EVENT/WORLD",
                           when=(W.now() + timedelta(days=6)).strftime("%Y-%m-%d"))
        await store.create("Promised to fix the bike.", room="EVENT/SELF",
                           direction_of_fit="telic", bound=["AI"])
        await _family(store)
    run(seed())
    b = run(A.build_breath())
    assert list(b) == ["core", "prospective", "recent", "involuntary", "invalidation", "earliest"]
    wire = json.loads(json.dumps(b, ensure_ascii=False))
    # Criterion: what the JSON says is everything the text is made from.
    assert A.render_breath(wire) == A.render_breath(b)
    text = A.render_breath(b)
    for block in ("prospective", "involuntary", "invalidation"):
        for it in b[block]["items"]:
            assert it["short"] in text and it["id"].startswith(it["short"])
    for title in ("核心", "惦记的事", "近三天", "依据变了的"):
        assert f"═══ {title}" in text


def _route(monkeypatch, store, locked=False):
    from starlette.requests import Request
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(PA, "gate_needed", lambda: locked)
    monkeypatch.setattr(PA, "has_session", lambda r: False)
    monkeypatch.setattr(PA, "hook_token", lambda: "s3cret")

    def call(query=b"", key=None):
        headers = [(b"x-loci-hook-token", key.encode())] if key else []

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}
        req = Request({"type": "http", "method": "GET", "path": "/api/v2/breath",
                       "headers": headers, "query_string": query}, receive)
        return asyncio.run(routes[("GET", "/api/v2/breath")](req))
    return call


def test_the_route_serves_the_same_text_and_the_json_keys_behind_the_hook_key(store, monkeypatch):
    run(store.create("Promised to fix the bike.", room="EVENT/SELF",
                     direction_of_fit="telic", bound=["AI"]))
    call = _route(monkeypatch, store, locked=True)
    assert call().status_code == 401
    text = call(key="s3cret")
    # Like every read, the route opens with the request's scope (core/scope.py): the
    # legacy host with no Loci-Scope reads the whole library.
    assert text.status_code == 200 and text.body.decode("utf-8") == (
        "〔范围：全库（open）〕\n" + run(A.surface_awaken()))
    data = json.loads(call(b"format=json", key="s3cret").body)
    assert list(data) == ["core", "prospective", "recent", "involuntary", "invalidation", "earliest",
                          "scope"]
    assert data["scope"] == "〔范围：全库（open）〕"
    [it] = data["prospective"]["items"]
    assert {"id", "short", "text", "reason"} <= set(it) and it["reason"]["kind"] == "undated"
    assert call(b"format=xml", key="s3cret").status_code == 400


def test_a_peek_through_the_route_stamps_nothing_and_records_nothing(store, tmp_path,
                                                                    monkeypatch):
    # A host's own machine read (Lento's hourly bridge) is not the model being handed the
    # screen: it must not use up an ask-once question nor count as shown.
    bid = run(store.create("I will bring the umbrella back next time.", room="EVENT/SELF",
                           looks_like_promise=True))
    call = _route(monkeypatch, store)
    for query in (b"peek=1", b"peek=1&format=json"):
        out = call(query)
        assert out.status_code == 200 and bid[:6] in out.body.decode("utf-8")
    assert not _disk(tmp_path, bid).get("last_asked")
    assert [r for r in store.usage.read() if r["kind"] == "shown"] == []
    # The tool is the model reading: it still stamps.
    assert "这条像是答应过的" in run(A.surface_awaken())
    assert _disk(tmp_path, bid).get("last_asked")
    assert call(b"peek=maybe").status_code == 400


def test_breath_is_a_hook_route():
    from web import panel_auth as PA
    assert PA.is_hook("/api/v2/breath")
