# -*- coding: utf-8 -*-
"""
tests/test_write_returns.py — what grow and regrow find out on the spot and say in their
own return: an old view the new body runs into (回望, core/_reconsolidation.py), and a
scene the last two weeks keep coming back to (场景常来, core/_case_recall.py).

Both are computed before the return is handed back, from the library as it stood before
the write, through the gate; neither may fail or stall the write. Checked through the
tools against a real BucketManager, with a fake vector engine whose similarities the test
sets, and a fake clock for the fourteen days.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

import tools.grow as grow_mod
from core import _case_recall as CR
from core import _cue as C
from core import _reconsolidation as RC
from core import _when as W
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.regrow import dispatch as regrow

TODAY = datetime(2026, 10, 1, 20, 0, 0, tzinfo=W.LOCAL_TZ)
LOOK_BACK = "撞上了旧看法"


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


class Vectors:
    """A vector engine whose answers the test sets: {body substring: {id: similarity}}."""

    enabled = True

    def __init__(self):
        self.answers: dict[str, dict[str, float]] = {}
        self.delay = 0.0
        self.fail = False
        self.asked_among: list = []

    async def generate_and_store(self, bucket_id, content):
        return True

    def delete_embedding(self, bucket_id):
        pass

    async def search_similar_strict(self, query, top_k=10, among=None):
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.fail:
            raise RuntimeError("embedding backend is down")
        self.asked_among.append(set(among or ()))
        scores: dict[str, float] = {}
        for key, hits in self.answers.items():
            if key in query:
                scores.update(hits)
        ranked = sorted(((b, s) for b, s in scores.items() if among is None or b in among),
                        key=lambda p: -p[1])
        return ranked[:top_k]


@pytest.fixture
def clock(monkeypatch):
    now = {"t": TODAY}
    monkeypatch.setattr(W, "now", lambda: now["t"])
    return now


@pytest.fixture
def store(tmp_path, monkeypatch, clock):
    vectors = Vectors()
    mgr = BucketManager({"buckets_dir": str(tmp_path)}, embedding_engine=vectors)
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    monkeypatch.delenv("LOCI_ALIAS_TABLE", raising=False)
    monkeypatch.setenv("LOCI_BUCKETS_DIR", str(tmp_path))
    (tmp_path / "aliases.yaml").write_text("阿明:\n  instance_of: 人\n", encoding="utf-8")
    monkeypatch.setattr(C, "get_owner_name", lambda: "小周")
    monkeypatch.setattr(C, "get_ai_name", lambda: "沈慢")

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    mgr.vectors = vectors
    return mgr


def run(coro):
    return asyncio.run(coro)


def event(text, **kw):
    return run(grow(kind="event", items=[{"room": "EVENT/SELF", "text": text,
                                          "v": 0.5, "a": 0.4}], **kw))


def mind(text, from_, **kw):
    return run(grow(kind="mind", room="MIND/VIEWS", text=text, from_=from_, v=0.5, a=0.4, **kw))


def view(store, text, **meta):
    return run(store.create(text, room=meta.pop("room", "MIND/VIEWS"), **meta))


def later(store, bid, **fields):
    """Fields create() does not take, set the way the tools set them."""
    assert run(store.update(bid, **fields))
    return bid


def look_back_lines(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if LOOK_BACK in ln]


# ── 回望: names ──────────────────────────────────────────────────────────────

def test_a_name_in_the_body_brings_back_its_card(store):
    card = view(store, "阿明做事急，但答应的事都会做到。", room="MIND/TRAITS", card_of="阿明")
    out = event("今天阿明又迟到了半小时。")
    [line] = look_back_lines(out)
    # Criterion: the return names the old view, its handle and why it came up, and asks.
    assert "『" in line and card[:6] in line and "阿明" in line and "要看一眼吗" in line


def test_the_owners_own_name_does_not_bring_back_a_card(store):
    view(store, "小周累的时候不爱说话。", room="MIND/TRAITS", card_of="小周")
    assert not look_back_lines(event("小周今天加班到很晚。"))


def test_a_name_inside_a_longer_word_does_not_fire(store):
    view(store, "阿明做事急。", room="MIND/TRAITS", card_of="阿明")
    assert not look_back_lines(event("我阿明白了这件事的意思。"))


# ── 回望: meaning ────────────────────────────────────────────────────────────

def test_a_close_view_comes_back_and_a_third_is_not_shown(store):
    a = view(store, "下雨天适合待在家里看书。")
    b = view(store, "雨天出门总会后悔。")
    c = view(store, "阴天人会变懒。")
    store.vectors.answers["下雨"] = {a: 0.91, b: 0.86, c: 0.83}
    out = event("又下雨了，一整天没出门，看完了半本书。")
    lines = look_back_lines(out)
    # Criterion: at most two, the closest first; the third is not mentioned at all.
    assert len(lines) == 2
    assert a[:6] in lines[0] and b[:6] in lines[1]
    assert c[:6] not in out


def test_a_view_under_the_line_is_not_mentioned(store):
    a = view(store, "下雨天适合待在家里看书。")
    store.vectors.answers["下雨"] = {a: 0.79}
    assert not look_back_lines(event("又下雨了。"))


def test_only_old_views_are_compared(store):
    ev = run(store.create("昨天下雨，没出门。", room="EVENT/SELF"))
    a = view(store, "下雨天适合待在家里看书。")
    store.vectors.answers["下雨"] = {ev: 0.99, a: 0.9}
    out = event("又下雨了。")
    # Criterion: an event is never "an old view"; the vector search is asked among views only.
    assert ev[:6] not in out and a[:6] in out
    assert all(ev not in among for among in store.vectors.asked_among)


def test_names_come_before_meaning(store):
    card = view(store, "阿明做事急。", room="MIND/TRAITS", card_of="阿明")
    a = view(store, "下雨天适合待在家里看书。")
    b = view(store, "雨天出门总会后悔。")
    store.vectors.answers["下雨"] = {a: 0.95, b: 0.9}
    lines = look_back_lines(event("下雨天阿明还是出门了。"))
    assert len(lines) == 2 and card[:6] in lines[0] and a[:6] in lines[1]


def test_a_thought_does_not_look_back_at_what_it_stands_on(store):
    ev = run(store.create("周六爬了山。", room="EVENT/SELF"))
    base = view(store, "累的时候反而想出门。", prov=[{"rel": "wasDerivedFrom", "target": ev}])
    other = view(store, "出门能把一周重置。")
    store.vectors.answers["出门"] = {base: 0.95, other: 0.85}
    out = mind("累的那几天，出门比睡懒觉管用。", [ev, base])
    # Criterion: a view in the entry's own lineage (what it stands on) is not "run into".
    assert base[:6] not in "\n".join(look_back_lines(out))
    assert other[:6] in "\n".join(look_back_lines(out))


def test_a_new_version_does_not_look_back_at_its_own_line(store):
    ev = run(store.create("周六爬了山。", room="EVENT/SELF"))
    old = view(store, "累的时候反而想出门。", prov=[{"rel": "wasDerivedFrom", "target": ev}])
    child = view(store, "所以周末要留一天出门。", prov=[{"rel": "wasDerivedFrom", "target": old}])
    other = view(store, "出门能把一周重置。")
    store.vectors.answers["出门"] = {old: 0.97, child: 0.93, other: 0.85}
    out = run(regrow(bucket_id=old, text="累的时候出门，比在家躺着恢复得快。",
                     v=0.6, a=0.4, mode="supplement"))
    lines = "\n".join(look_back_lines(out))
    # Criterion: the version it replaces and what grew out of that version are its own
    # lineage; an unrelated close view is still mentioned.
    assert old[:6] not in lines and child[:6] not in lines
    assert other[:6] in lines


def test_a_faded_or_superseded_view_is_not_mentioned(store):
    faded = later(store, view(store, "下雨天适合待在家里看书。"), dont_surface=True)
    old = later(store, view(store, "雨天出门总会后悔。"), superseded_by="f" * 12)
    store.vectors.answers["下雨"] = {faded: 0.95, old: 0.9}
    assert not look_back_lines(event("又下雨了。"))


def test_no_vectors_never_fails_or_stalls_the_write(store, monkeypatch):
    card = view(store, "阿明做事急。", room="MIND/TRAITS", card_of="阿明")
    a = view(store, "下雨天适合待在家里看书。")
    store.vectors.answers["下雨"] = {a: 0.95}
    monkeypatch.setattr(RC, "MEANING_BUDGET_SECONDS", 0.05)
    store.vectors.delay = 5.0
    out = event("下雨天阿明还是出门了。")
    # Criterion: the write lands and the name hit is still said; the meaning hit that ran
    # out of time is silently left out.
    assert "条 event 已落盘" in out
    lines = look_back_lines(out)
    assert len(lines) == 1 and card[:6] in lines[0]
    store.vectors.delay = 0.0
    store.vectors.fail = True
    out = event("下雨了，阿明没来。")
    assert "条 event 已落盘" in out and a[:6] not in out


def test_what_a_return_shows_is_in_the_usage_log(store):
    a = view(store, "下雨天适合待在家里看书。")
    store.vectors.answers["下雨"] = {a: 0.95}
    event("又下雨了。")
    rows = [json.loads(ln) for ln in store.usage.path.read_text(encoding="utf-8").splitlines()]
    # One line says all three: merged rows could pass on a mixture of lines.
    assert any(r.get("kind") == "shown" and r.get("road") == RC.ROAD and r.get("ids") == [a]
               for r in rows), rows


# ── 场景常来 ──────────────────────────────────────────────────────────────────

def _six_days_of(store, word="健身房", days=6, **meta):
    for k in range(days, 0, -1):
        day = (TODAY - timedelta(days=k)).strftime("%Y-%m-%d")
        run(store.create(f"下班去{word}练了一小时。", room="EVENT/SELF", tags=[word],
                         when=day, **meta))


def scene_line(out: str) -> list[str]:
    return [ln for ln in out.splitlines() if "最近常出现" in ln]


def test_a_scene_on_six_days_is_asked_on_the_seventh_and_not_on_the_eighth(store, clock):
    _six_days_of(store)
    out = event("今天又去健身房，换了新教练。")
    [line] = scene_line(out)
    assert "「健身房」最近常出现（14 天里 6 天）" in line
    assert "前几次有没有经验值得留下" in line
    clock["t"] = TODAY + timedelta(days=1)
    # Criterion: the same word is not asked again within fourteen days.
    assert not scene_line(event("健身房人很多，等了半天器械。"))


def test_the_asked_word_is_asked_again_after_the_quiet_days(store, clock):
    _six_days_of(store)
    assert scene_line(event("今天又去健身房。"))
    clock["t"] = TODAY + timedelta(days=CR.QUIET_DAYS)
    for k in range(6):
        day = (clock["t"] - timedelta(days=k + 1)).strftime("%Y-%m-%d")
        run(store.create("去健身房。", room="EVENT/SELF", tags=["健身房"], when=day))
    assert scene_line(event("健身房今天停电了。"))


def test_four_days_are_not_enough(store):
    _six_days_of(store, days=4)
    assert not scene_line(event("今天又去健身房。"))


def test_days_outside_the_fourteen_do_not_count(store):
    for k in (20, 18, 16, 3, 2):
        day = (TODAY - timedelta(days=k)).strftime("%Y-%m-%d")
        run(store.create("去健身房。", room="EVENT/SELF", tags=["健身房"], when=day))
    assert not scene_line(event("今天又去健身房。"))


def test_a_word_with_a_cue_hanging_on_it_is_not_asked(store):
    _six_days_of(store)
    run(store.create("上次膝盖疼是没热身。", room="MIND/VIEWS",
                     cue={"condition": "下次去健身房", "phrasings": ["健身房"]}))
    assert not scene_line(event("今天又去健身房。"))


def test_a_want_or_a_dream_does_not_earn_the_question(store):
    _six_days_of(store)
    assert not scene_line(event("明天想去健身房。", direction_of_fit="telic"))
    assert not scene_line(event("梦见健身房变成了游泳池。", internally_generated=True))


def test_wants_and_dreams_do_not_count_as_days(store):
    _six_days_of(store, days=3)
    for k in (5, 6, 7):
        day = (TODAY - timedelta(days=k)).strftime("%Y-%m-%d")
        run(store.create("梦见健身房。", room="EVENT/SELF", tags=["健身房"], when=day,
                         internally_generated=True))
    assert not scene_line(event("今天又去健身房。"))


def test_entries_the_gate_keeps_off_do_not_count(store):
    _six_days_of(store, days=4)
    later(store, run(store.create("去健身房。", room="EVENT/SELF", tags=["健身房"],
                                  when=(TODAY - timedelta(days=9)).strftime("%Y-%m-%d"))),
          dont_surface=True)
    assert not scene_line(event("今天又去健身房。"))


def test_the_asked_file_stays_small(store, clock):
    for i, word in enumerate(("健身房", "图书馆")):
        clock["t"] = TODAY + timedelta(days=20 * i)
        for k in range(6):
            day = (clock["t"] - timedelta(days=k + 1)).strftime("%Y-%m-%d")
            run(store.create(f"去{word}。", room="EVENT/SELF", tags=[word], when=day))
        assert scene_line(event(f"今天又去{word}。"))
    asked = CR.load_asked(str(store.base_dir))
    # Criterion: a word past its quiet days is dropped when the file is written.
    assert list(asked) == ["图书馆"]
