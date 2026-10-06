# -*- coding: utf-8 -*-
"""
tests/test_dream_material.py — what a dream is woven from, and what it leaves behind.

THE MARK (hole 6b)
    A fading dream leaves a trace, an ordinary event at a=0.3 — far enough from the
    middle to clear the never-worked-out pool's emotion line, so dreams were fed their own
    remains. Everything a dream leaves carries `internally_generated` now (the trace, a
    note retelling it, a want grown from it), and no stream draws from anything that is,
    or stands on, a dream. A trace written before the mark existed is known by its words.

THE TWO SHARES
    cold    one old thing gone faint or sunk, not discounted by age; a sunk one is fed its
            original from archive/原文/, not the summary its body was reduced to
    quote   one memory of the host's material, its original asked of the host only once a
            dream will be woven: given -> the host's text, unavailable -> the memory's own
            body, refused -> not drawn. The record keeps the sources behind it (never the
            text), so a withdrawal reaches the dream.

AND
    an `avoid` hold keeps an entry out of every stream, a `defer` does not; a regrown
    want is back in the want pool; thread candidates ride with the dream and write
    nothing; weaving warms nothing; a nightmare is a mark and nothing more.

The weaver and the hosts are faked: no model is ever called here.
"""

import asyncio
import json
import os
import random
from datetime import timedelta

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _dream as D
from core import _fold as F
from core import _originals as O
from core import _source_change as SC
from core import _sources as S
from core import _when as W
from core import visibility as V
from core.bucket_manager import BucketManager
from core.scope import Host
from core import runtime as rt
from tools.grow import rooms_path
from tools.recall import core as R
from utils import WAS_DERIVED_FROM, WAS_QUOTED_FROM

M_REC = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003"}
M_STR = "lento:home/private:U#m_0003"
HOST_TEXT = "书里那一段：灯塔的光每隔九秒扫过一次海面"
DREAM_TEXT = ("我站在一条很长的走廊里，门的数量一直在变。墙上挂着小周的钥匙，"
              "钥匙在滴水。走到尽头是海，海在屋子里面，浪拍着书架。")


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def __init__(self):
        self.lines = []

    def _n(self, msg, *a, **k):
        self.lines.append(str(msg) % a if a else str(msg))
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    log = _Log()
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", log)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    mgr.test_log = log
    return mgr


def run(coro):
    return asyncio.run(coro)


def meta_of(store, bid) -> dict:
    return dict(frontmatter.load(store._find_bucket_file(bid)).metadata)


def new_id(out: str) -> str:
    return out.split("📝", 1)[1][:12]


async def records():
    from core import _muse
    return (await _muse.load_records())[0]


class Weaver:
    """Stands in for the weaving model: records what it was handed, answers `answer`."""

    def __init__(self, **answer):
        self.answer = {"完整": DREAM_TEXT, "碎片": "走廊。钥匙。屋里的海。", "v": 0.4,
                       "a": 0.6, **answer}
        self.calls = []

    async def __call__(self, ingredients, c):
        self.calls.append(D.build_user_message(ingredients, c))
        return D.parse_dream(json.dumps(self.answer, ensure_ascii=False))


def a_dream(**over) -> dict:
    stamp = W.now().isoformat(timespec="seconds")
    rec = {"id": "d0000000feed", "织于": stamp, "起算点": stamp, "回想次数": 0, "轮次": 0,
           "碎片": "走廊。钥匙。屋里的海。", "完整": DREAM_TEXT, "v": 0.4, "a": 0.6,
           "nightmare": False, "素材": {"压在心头": [], "想不明白": [], "几个词": []}}
    rec.update(over)
    return rec


async def heavy_want(store, text="答应陪小周去看海，一直没去成。"):
    bid = await store.create(text, tags=["t"], room="EVENT/SELF", direction_of_fit="telic",
                             weight=0.9, valence=0.4, arousal=0.6)
    return bid


# ── the mark ────────────────────────────────────────────────────────────────

def test_the_trace_of_a_fading_dream_is_marked_and_does_not_enter_the_dream_pool(store):
    async def go():
        path = D.save_record(a_dream(完整="", 起算点=(W.now() - timedelta(hours=3))
                                     .isoformat(timespec="seconds")))
        out = await D.sweep_expired(D.dream_config())
        assert not os.path.exists(path)
        return new_id(out["留痕"][0]), await records()
    trace, recs = run(go())
    assert meta_of(store, trace)["internally_generated"] is True
    meta = next(m for m, _t in recs if m["id"] == trace)
    # Criterion: without the mark the trace is in the pool — a=0.3 clears the 0.15 line.
    unmarked = dict(meta, internally_generated=None)
    assert D.unclear_pool([(unmarked, "plain words")], set(), {}, W.now()), \
        "the leak this closes: an unmarked neutral event is never-worked-out material"
    now = W.now()
    for pool in (D.unclear_pool(recs, set(), {}, now), D.want_pool(recs, now),
                 D.cold_pool(recs, now), D.quote_pool(recs, now)):
        assert trace not in {x.id for x in pool}


def test_a_trace_written_before_the_mark_existed_is_known_by_its_words(store):
    async def go():
        old = await store.create(f"09-12 {D.TRACE_WORDS}", tags=["t"], room="EVENT/SELF",
                                 valence=0.5, arousal=0.3)
        real = await store.create("09-12 做了一碗面，汤有点咸。", tags=["t"], room="EVENT/SELF",
                                  valence=0.5, arousal=0.3)
        return old, real, await records()
    old, real, recs = run(go())
    pool = {x.id for x in D.unclear_pool(recs, set(), {}, W.now())}
    assert old not in pool and real in pool


def test_a_note_retelling_a_dream_on_disk_is_marked_without_being_asked(store):
    async def go():
        D.save_record(a_dream())
        note = new_id(await grow_mod.dispatch(kind="event", items=[{
            "room": "EVENT/SELF", "v": 0.4, "a": 0.6,
            "text": "昨晚梦见一条很长的走廊，门的数量一直在变，墙上挂着小周的钥匙，钥匙在滴水。"}]))
        real = new_id(await grow_mod.dispatch(kind="event", items=[{
            "room": "EVENT/SELF", "v": 0.6, "a": 0.4,
            "text": "下午陪小周去书店，小周买了一本讲灯塔的书。"}]))
        # One shared phrase is not a retelling.
        brush = new_id(await grow_mod.dispatch(kind="event", items=[{
            "room": "EVENT/SELF", "v": 0.5, "a": 0.4,
            "text": "小周的钥匙在滴水，刚才下雨小周忘了带伞，衣服全湿了，我给小周拿了毛巾。"}]))
        return note, real, brush
    note, real, brush = run(go())
    assert meta_of(store, note).get("internally_generated") is True
    assert not meta_of(store, real).get("internally_generated")
    assert not meta_of(store, brush).get("internally_generated")


def test_a_note_written_after_waking_is_still_known_by_the_prints(store):
    # Criterion: waking strips the whole text off the disk; a note written from the
    # window after that is matched against the prints the record kept.
    async def go():
        D.save_record(a_dream(whole_prints=D.dream_prints(DREAM_TEXT)))
        assert D.degrade_on_wake() == ["d0000000feed"]
        assert DREAM_TEXT not in json.dumps(D.load_dreams()[0], ensure_ascii=False)
        return new_id(await grow_mod.dispatch(kind="event", items=[{
            "room": "EVENT/SELF", "v": 0.4, "a": 0.6,
            "text": "梦里走到尽头是海，海在屋子里面，浪拍着书架。"}]))
    assert meta_of(store, run(go())).get("internally_generated") is True


def test_a_want_grown_from_a_dream_note_is_marked_and_kept_out_of_the_want_pool(store):
    async def go():
        note = await store.create("梦见海在屋子里。", tags=["t"], room="EVENT/SELF",
                                  internally_generated=True, valence=0.4, arousal=0.6)
        want = new_id(await grow_mod.dispatch(
            kind="event", direction_of_fit="telic", weight=0.9, from_=[note], items=[{
                "room": "EVENT/SELF", "v": 0.4, "a": 0.6, "text": "想弄明白那片海是什么意思。"}]))
        plain = await heavy_want(store)
        return want, plain, await records()
    want, plain, recs = run(go())
    assert meta_of(store, want).get("internally_generated") is True
    pool = {x.id for x in D.want_pool(recs, W.now())}
    assert want not in pool and plain in pool


def test_the_mark_survives_a_period_a_gist_and_a_new_version(store):
    async def go():
        note = await store.create("梦见海在屋子里。", tags=["t"], room="EVENT/SELF",
                                  internally_generated=True, valence=0.4, arousal=0.6)
        other = await store.create("下午去了海边。", tags=["t"], room="EVENT/SELF",
                                   valence=0.6, arousal=0.5)
        day = W.now().strftime("%Y-%m-%d")
        await F.save_gist("那几天", "EVENT/SELF", 0.5, 0.4, [], when=f"{day}..{day}")
        thought = await store.create("海对我来说是边界。", tags=["t"], room="MIND/VIEWS",
                                     prov=[{"rel": WAS_DERIVED_FROM, "target": note}])
        thought2 = await store.create("边界不是墙。", tags=["t"], room="MIND/VIEWS",
                                      prov=[{"rel": WAS_DERIVED_FROM, "target": other}])
        gist, _report = await F.save_gist("我在想边界。", "MIND/VIEWS", 0.5, 0.4,
                                          [thought, thought2])
        new_note, _r = await F.save_gist("梦见海涨进了屋子。", "EVENT/SELF", 0.4, 0.6, [],
                                         supersedes=note)
        read = await R.recall_core(when="", room="", tag="", query=gist)
        read_note = await R.recall_core(when="", room="", tag="", query=new_note)
        return note, other, thought, gist, new_note, read, read_note, await records()
    note, other, thought, gist, new_note, read, read_note, recs = run(go())
    assert meta_of(store, note).get("internally_generated") is True, "a period keeps it"
    assert meta_of(store, new_note).get("internally_generated") is True, "a new version carries it"
    born = D.dream_born_ids(recs)
    assert {note, new_note, thought, gist} <= born and other not in born
    # Criterion: read by id, the gist says it stands on a dream, and so does its line.
    assert R.DREAMT_MARK in read.split("\n")[1]
    assert R.DREAMT_MARK in read_note.split("\n")[1]


# ── the cold share ──────────────────────────────────────────────────────────

def _put_old(store, text, stage, **kw):
    async def go():
        bid = await store.create(text, tags=["t"], room="EVENT/SELF", valence=0.3,
                                 arousal=0.7, summary="一句摘要", **kw)
        if stage == "sunk":
            assert await store.sink_bucket(bid)
        else:
            assert await store.update(bid, decay_stage=stage)
        return bid
    return run(go())


def test_each_dream_gets_one_cold_share_and_a_sunk_one_is_fed_its_original(store, monkeypatch):
    run(heavy_want(store))
    sunk = _put_old(store, "那年冬天在旧房子里，暖气坏了，我们裹着一床被子听收音机。", "sunk")
    weaver = Weaver()
    monkeypatch.setattr(D, "call_model", weaver)
    rec = run(D.weave(force=True))
    assert rec["素材"]["冷档案"] == [sunk]
    assert "暖气坏了" in weaver.calls[0] and "一句摘要" not in weaver.calls[0]
    assert "很久以前的" in weaver.calls[0]


def test_the_cold_share_is_one_entry_drawn_without_the_time_decay(store, monkeypatch):
    run(heavy_want(store))
    for i in range(4):
        _put_old(store, f"很久以前的第 {i} 件事，细节很多很多。", "faded")
    seen = []
    real = random.choices

    def spy(population, weights=None, **kw):
        seen.append(list(weights or []))
        return real(population, weights=weights, **kw)
    monkeypatch.setattr(D.random, "choices", spy)
    got = run(D.gather_ingredients())
    assert len(got["冷档案"]) == 1 and got["池子"]["冷档案"] == 4
    # The draw over four equally aroused entries weighs them equally, whatever their age.
    cold_draw = next(w for w in seen if len(w) == 4)
    assert len(set(cold_draw)) == 1


def test_the_cold_share_leaves_out_the_living_the_held_and_the_dreamt(store):
    alive = run(store.create("昨天的事。", tags=["t"], room="EVENT/SELF", valence=0.3,
                             arousal=0.7))
    avoided = _put_old(store, "一件不想再碰的旧事。", "faded")
    deferred = _put_old(store, "一件先别催的旧事。", "faded")
    dreamt = _put_old(store, "梦见旧房子。", "faded", internally_generated=True)
    mind = run(store.create("旧想法。", tags=["t"], room="MIND/VIEWS", valence=0.3,
                            arousal=0.7))
    run(store.update(mind, decay_stage="faded"))
    for target, level in ((avoided, "avoid"), (deferred, "defer")):
        run(store.create("条子", tags=["t"], room="EVENT/SELF", direction_of_fit="telic",
                         exception_of=target, hold=level))
    pool = {x.id for x in D.cold_pool(run(records()), W.now())}
    assert pool == {deferred}, (alive, avoided, dreamt, mind)


# ── the quote share ─────────────────────────────────────────────────────────

class Hosts:
    """Stands in for the fourth joint: answers per source string form."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.asked = []

    async def __call__(self, record, *, hosts, request=None, settings=None, registry=None,
                       transport=None):
        label = S.record_string(record)
        self.asked.append((label, request))
        outcome, extra = self.answers[label]
        return O.Answer(outcome, source=label, host="home", **extra)


def _quoted_memory(store, target=M_STR, text="小周读给我听的那一段，关于灯塔。"):
    # Calm on purpose: an event with emotion is also never-worked-out material, and the
    # unclear stream drawing it first would leave the quote share nothing to test.
    return run(store.create(text, tags=["t"], room="EVENT/SELF", valence=0.5, arousal=0.5,
                            prov=[{"rel": WAS_QUOTED_FROM, "target": target}]))


def _given(text=HOST_TEXT):
    return (O.GIVEN, {"lines": (O.Line(id="m_0003", text=text),)})


def test_a_given_original_is_fed_and_neither_stored_nor_logged(store, monkeypatch):
    run(heavy_want(store))
    quoted = _quoted_memory(store)
    hosts = Hosts({M_STR: _given()})
    monkeypatch.setattr(O, "fetch", hosts)
    weaver = Weaver()
    monkeypatch.setattr(D, "call_model", weaver)
    rec = run(D.weave(force=True))
    assert rec["素材"]["原话"] == [quoted] and rec["来源"] == [M_STR]
    assert HOST_TEXT in weaver.calls[0] and "读过听过的" in weaver.calls[0]
    # Criterion: weaving reads the whole library — the host is told no scope.
    assert hosts.asked == [(M_STR, None)]
    on_disk = open(D.load_dreams()[0]["_路径"], encoding="utf-8").read()
    assert HOST_TEXT not in on_disk
    assert not any(HOST_TEXT in ln for ln in store.test_log.lines)


def test_an_unavailable_original_feeds_the_memorys_own_body(store, monkeypatch):
    run(heavy_want(store))
    quoted = _quoted_memory(store)
    monkeypatch.setattr(O, "fetch", Hosts({M_STR: (O.UNAVAILABLE, {"why": O.TIMEOUT})}))
    weaver = Weaver()
    monkeypatch.setattr(D, "call_model", weaver)
    rec = run(D.weave(force=True))
    assert rec["素材"]["原话"] == [quoted]
    assert "关于灯塔" in weaver.calls[0]


def test_a_refused_original_keeps_the_memory_out_and_the_next_is_tried(store, monkeypatch):
    run(heavy_want(store))
    other = "lento:home/private:U#m_0009"
    refused = _quoted_memory(store, text="被撤回的那一段。")
    kept = _quoted_memory(store, target=other, text="另一本书里的一段。")
    hosts = Hosts({M_STR: (O.NOT_ALLOWED, {"reason": "withdrawn"}), other: _given("另一段原文")})
    monkeypatch.setattr(O, "fetch", hosts)
    weaver = Weaver()
    monkeypatch.setattr(D, "call_model", weaver)
    rec = run(D.weave(force=True))
    assert rec["素材"]["原话"] == [kept] and refused not in D.ingredient_ids(rec)
    assert "被撤回的那一段" not in weaver.calls[0]
    # Only refused: nothing is drawn.
    monkeypatch.setattr(O, "fetch", Hosts({M_STR: (O.NOT_ALLOWED, {}),
                                           other: (O.NOT_ALLOWED, {})}))
    got, fed = run(D._quoted(D.quote_pool(run(records()), W.now()), D.dream_config()))
    assert got == [] and fed == []


def test_no_host_is_asked_on_a_look_that_weaves_nothing(store, monkeypatch):
    _quoted_memory(store)
    hosts = Hosts({M_STR: _given()})
    monkeypatch.setattr(O, "fetch", hosts)
    monkeypatch.setattr(D, "call_model", Weaver())
    out = run(D.maintain())
    assert out["织"] is None, "nothing weighs enough: below the line"
    assert hosts.asked == []


def test_a_withdrawal_reaches_a_dream_fed_by_the_source(store):
    # The source is known to the registry through another memory; the dream was fed the
    # host's text for a memory that only quotes it, so no entry of the change names it.
    run(store.create("别的记忆。", tags=["t"], room="EVENT/WORLD", sources=[M_REC]))
    D.save_record(a_dream(来源=[M_STR], 素材={"压在心头": [], "想不明白": [], "原话": []}))
    D.save_record(a_dream(id="d0000000beef", 完整="梦见下雨", 碎片="雨"))
    status, out = run(SC.handle(store, {"change_id": "c-1", "source": M_STR, "host_seq": 1,
                                        "change": "withdrawn"},
                                Host("life", scope_mode="open", may_restore=True)))
    assert status == 200 and out["cleanup"]["dream_records"] == "done", out
    assert [r["id"] for r in D.load_dreams()] == ["d0000000beef"]


def test_a_dream_whose_source_is_gone_is_withheld(store):
    run(store.create("别的记忆。", tags=["t"], room="EVENT/WORLD", sources=[M_REC]))
    rec = a_dream(来源=[M_STR + "@r2"])
    assert run(D.withheld_ingredients(rec)) == []
    run(store.sources.apply_change({"change_id": "c-2", "source": M_STR, "host_seq": 2,
                                    "kind": "deleted"}, may_restore=True, host="life"))
    assert run(D.withheld_ingredients(rec)) == [M_STR + "@r2"]


def test_an_avoid_hold_keeps_a_quoted_memory_out_and_a_defer_does_not(store):
    avoided = _quoted_memory(store, text="不想再碰的那一段。")
    deferred = _quoted_memory(store, target="lento:home/private:U#m_0009",
                              text="先别催的那一段。")
    for target, level in ((avoided, "avoid"), (deferred, "defer")):
        run(store.create("条子", tags=["t"], room="EVENT/SELF", direction_of_fit="telic",
                         exception_of=target, hold=level))
    assert {x.id for x in D.quote_pool(run(records()), W.now())} == {deferred}


# ── thread candidates, regrown wants, warmth, nightmares ────────────────────

def test_thread_candidates_ride_with_the_dream_and_write_nothing(store, monkeypatch):
    from web import loci_dream as L

    async def no_muse():
        return {"worth_poking": False}
    monkeypatch.setattr(L, "build_muse_pending", no_muse)
    run(heavy_want(store))
    candidates = [{"碰到": "下雨天", "想起": "旧房子的暖气"}, {"碰到": "灯塔", "想起": "那本书"},
                  {"碰到": "第三条", "想起": "不要"}, "not a candidate"]
    monkeypatch.setattr(D, "call_model", Weaver(线索=candidates))
    entries_before = len(run(store.list_all(include_archive=True)))
    run(D.weave(force=True))
    poke = run(L.build_poke())["dreams"][0]
    current = run(D.current_dream())
    for handed in (poke, current):
        assert handed["梦里想到"] == candidates[:2]
        assert "trace(" in handed["要留线索"] and "cue=" in handed["要留线索"]
    # Criterion: no cue was written anywhere, and no entry was added.
    rows = run(store.list_all(include_archive=True))
    assert len(rows) == entries_before and not any((r["metadata"] or {}).get("cue") for r in rows)


def test_a_dream_without_candidates_hands_out_none(store):
    assert D.handout_cues(a_dream()) == {}
    assert D.parse_dream(json.dumps({"完整": "a", "碎片": "b", "v": 0.5, "a": 0.5}))["线索"] == []


def test_a_regrown_want_is_back_in_the_want_pool(store):
    async def go():
        old = await heavy_want(store)
        new, _report = await F.save_gist("答应陪小周去看海，改成十月去。", "EVENT/SELF", 0.4, 0.6,
                                         [], supersedes=old)
        return old, new, await records()
    old, new, recs = run(go())
    pool = {x.id for x in D.want_pool(recs, W.now())}
    assert new in pool and old not in pool


def test_weaving_warms_nothing_and_notes_only_when_a_want_was_dreamt(store, monkeypatch):
    want = run(heavy_want(store))
    unclear = run(store.create("吵了一架，谁也没说话。", tags=["t"], room="EVENT/SELF",
                               valence=0.1, arousal=0.9))
    cold = _put_old(store, "很久以前那次搬家，箱子堆到天花板。", "sunk")
    quoted = _quoted_memory(store)
    monkeypatch.setattr(O, "fetch", Hosts({M_STR: _given()}))
    monkeypatch.setattr(D, "call_model", Weaver())
    before = {b: meta_of(store, b) for b in (want, unclear, cold, quoted)}
    rec = run(D.weave(force=True))
    assert set(D.ingredient_ids(rec)) == {want, unclear, cold, quoted}
    for bid, old in before.items():
        now = meta_of(store, bid)
        moved = {k for k in set(old) | set(now) if old.get(k) != now.get(k)}
        assert moved <= ({"last_dreamt"} if bid == want else set()), (bid, moved)
    assert meta_of(store, want)["last_dreamt"]


def test_a_nightmare_is_one_mark_and_nothing_more(store, monkeypatch):
    run(heavy_want(store))
    monkeypatch.setattr(D, "call_model", Weaver(v=0.1, a=0.9))
    rec = run(D.weave(force=True))
    assert rec["nightmare"] is True
    assert run(D.current_dream())["nightmare"] is True


def test_a_host_saying_withdrawn_holds_the_quoted_memory_for_every_road(store, monkeypatch):
    # The other team's 10-02 ruling: not refused just this once while the rest keeps
    # feeding it — held until the ordered change settles it; nothing written as its state.
    run(heavy_want(store))
    quoted = _quoted_memory(store)
    said = (O.NOT_ALLOWED, {"reason": "withdrawn", "holds": ((M_STR, "withdrawn"),)})
    monkeypatch.setattr(O, "fetch", Hosts({M_STR: said}))
    got, _fed = run(D._quoted(D.quote_pool(run(records()), W.now()), D.dream_config()))
    assert got == []
    assert store.sources.state_of(M_STR) == S.HELD and store.sources.describe(M_STR) is None
    meta = run(store.get_including_archive(quoted))["metadata"]
    assert V.source_gone(meta), "held on every road, not just this dream"
    assert quoted not in {x.id for x in D.quote_pool(run(records()), W.now())}


def test_an_unavailable_original_is_not_fed_once_the_memory_is_held_meanwhile(store,
                                                                            monkeypatch):
    run(heavy_want(store))
    quoted = _quoted_memory(store)

    class Meanwhile(Hosts):
        async def __call__(self, record, **kw):
            from core import _source_change as SCH
            await SCH.hold(store, M_STR, "deleted", "home")
            return await super().__call__(record, **kw)
    monkeypatch.setattr(O, "fetch", Meanwhile({M_STR: (O.UNAVAILABLE, {"why": O.TIMEOUT})}))
    got, _fed = run(D._quoted(D.quote_pool(run(records()), W.now()), D.dream_config()))
    assert quoted not in [x.id for x in got]
