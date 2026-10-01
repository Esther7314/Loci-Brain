# -*- coding: utf-8 -*-
"""
tests/test_cue.py — the strong reminder: which cards a message brings, and how the card
ledger counts them.

Matching is code only: a cue's phrasings and the names table, with the rules core/_cue.py
states (short spellings and words cut in half do not fire; negation and hypotheticals do,
quoted and marked). A card is offered, delivered only when the host says it reached the
model, not handed again to that window at the same version, and handed again once the
host strikes it or the entry changes. A clock time that passes in an open window comes as
a 「提醒」 card with the next message, whatever it says; a backfilled clock is worded as a
reference time. Nothing a hold covers, nothing the core block holds, nothing out of scope,
and never the text of a memory standing on a withdrawn source.
"""

import asyncio
import json
from datetime import datetime, timedelta

import pytest

from core import _cue as C
from core import _when as W
from core._cue_ledger import CueLedger
from core._sources import SourceRegistry
from core._usage import UsageLog
from core.profile import CUED, awake_reasons

TODAY = datetime(2026, 10, 1, 10, 0, 0, tzinfo=W.LOCAL_TZ)


def run(coro):
    return asyncio.run(coro)


def at(hour: int, minute: int = 0) -> datetime:
    return TODAY.replace(hour=hour, minute=minute)


def bucket(bid, content, *, created=None, room="EVENT/SELF", **meta) -> dict:
    bid = (bid * 12)[:12]
    m = {"id": bid, "room": room,
         "created": (created or TODAY - timedelta(days=3)).isoformat(timespec="seconds")}
    m.update(meta)
    return {"id": bid, "content": content, "metadata": m}


EXAM = bucket("a", "考完试一起去吃那家甜品。", direction_of_fit="telic", bound=["AI"],
              cue={"condition": "考完试", "phrasings": ["考完", "甜品店"]})


class Store:
    """What the cue route needs of a library: the rows, and the real registries on disk."""

    def __init__(self, root, rows):
        self.rows = rows
        self.sources = SourceRegistry(root)
        self.cues = CueLedger(root)
        self.usage = UsageLog(root)

    async def list_all(self, include_archive=False):
        return [dict(r) for r in self.rows]


@pytest.fixture
def clock(monkeypatch):
    now = {"t": TODAY}
    monkeypatch.setattr(W, "now", lambda: now["t"])
    return now


@pytest.fixture
def names(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCI_ALIAS_TABLE", raising=False)
    monkeypatch.setenv("LOCI_BUCKETS_DIR", str(tmp_path))

    def write(text: str):
        (tmp_path / "aliases.yaml").write_text(text, encoding="utf-8")
    write("")
    return write


def ask(store, text, window="w1", turn="t1", host="life", scope=None):
    return run(C.cue(store, text=text, window=window, turn=turn, host=host, scope=scope))


def ids(out):
    return [c["id"] for c in out["cards"]]


# ── matching ────────────────────────────────────────────────────────────────

def test_a_phrasing_brings_its_card_and_says_what_matched(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    out = ask(store, "终于考完了！！")
    assert ids(out) == [EXAM["id"]]
    card = out["cards"][0]
    assert card["kind"] == "memory" and card["why"] == "考完"
    assert "【相关记忆】" in card["text"] and "「考完」" in card["text"]
    assert EXAM["id"][:6] in card["text"] and "答应了的（我欠着）" in card["text"]
    assert EXAM["metadata"]["bound"] == ["AI"], "the stored name is untouched"
    assert out["text"] == card["text"]


def test_negation_and_hypotheticals_bring_the_card_marked(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    neg = ask(store, "还没考完呢，烦死了", turn="t1")
    assert ids(neg) == [EXAM["id"]] and "否定" in neg["text"] and "还没考完呢" in neg["text"]
    hyp = ask(store, "要是考完了的话就去", window="w2")
    assert ids(hyp) == [EXAM["id"]] and "假设" in hyp["text"]
    q = ask(store, "你考完了吗？", window="w3")
    assert "在问" in q["text"]


def test_nothing_said_brings_nothing(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    assert ask(store, "今天天气不错")["cards"] == []


def test_short_spellings_and_words_cut_in_half_do_not_fire(tmp_path, clock, names):
    names("阿明:\n  aliases: [明明, 老明头]\n  instance_of: 人\n"
          "Leon:\n  instance_of: 人\n  present_in: [Detroit]\n"
          "小周:\n  instance_of: 人\n  member_of: [读书会]\n")
    store = Store(tmp_path, [bucket("b", "阿明来过。", subjects=["阿明"]),
                             bucket("c", "Leon 在故事里。", subjects=["Leon"]),
                             bucket("d", "小周说了一件事。", subjects=["小周"])])
    assert ask(store, "我阿明白了")["cards"] == [], "a name cut in half by a word"
    assert ask(store, "明明是你说的", window="w2")["cards"] == [], "a 2-character alias"
    assert ask(store, "Leonardo 画的", window="w3")["cards"] == [], "inside a longer word"
    out = ask(store, "老明头今天来了，还有 leon", window="w4")
    assert sorted(c["why"] for c in out["cards"]) == ["Leon", "老明头"]
    out = ask(store, "明天找小周玩", window="w5")
    assert [c["kind"] for c in out["cards"]] == ["name_bare"]
    assert "读书会" in out["text"] and "相关 1 条" in out["text"]


def test_a_two_character_phrasing_inside_a_word_does_not_fire(tmp_path, clock, names):
    store = Store(tmp_path, [bucket("q", "等考试结束。", direction_of_fit="telic",
                                    cue={"condition": "考试结束", "phrasings": ["考完"]})])
    assert ask(store, "考完试卷发下来")["cards"] == [], "考完 cut inside 考完试"
    assert len(ask(store, "终于考完了", window="w2")["cards"]) == 1
    store = Store(tmp_path, [EXAM])
    assert ids(ask(store, "去甜品店吧", window="w3")) == [EXAM["id"]]


def test_name_cards_rank_after_memories_and_skip_my_own_and_the_core(tmp_path, clock, names,
                                                                     monkeypatch):
    names("小林:\n  instance_of: 人\n小周:\n  instance_of: 人\n")
    monkeypatch.setattr(C, "get_owner_name", lambda: "小周")
    card = bucket("e", "小林是大学同学，在找工作。", room="MIND/TRAITS", card_of="小林")
    other = bucket("f", "小林来吃饭。", subjects=["小林"])
    store = Store(tmp_path, [EXAM, card, other])
    out = ask(store, "小林和小周说考完了")
    assert [c["kind"] for c in out["cards"]] == ["memory", "name"]
    assert "【相关名字】小林：小林是大学同学" in out["text"] and "相关 1 条" in out["text"]
    page = bucket("g", "名字：小林（大学同学）", tags=["__档案事实__"])
    store = Store(tmp_path, [card, other, page])
    assert ask(store, "小林来了", window="w2")["cards"] == [], "the core page names it already"


def test_held_closed_and_core_entries_bring_nothing(tmp_path, clock, names):
    hold = bucket("h", "这周先别催。", direction_of_fit="telic", exception_of=EXAM["id"],
                  hold="defer", when="2026-10-05")
    closed = bucket("i", "想去那家甜品店。", direction_of_fit="telic", status="abandoned",
                    cue={"condition": "甜品店开门", "phrasings": ["甜品店"]})
    pinned = bucket("j", "考完要先休息。", pinned=True,
                    cue={"condition": "考完", "phrasings": ["考完"]})
    store = Store(tmp_path, [EXAM, hold, closed, pinned])
    assert ask(store, "考完了，去甜品店")["cards"] == []


def test_a_hold_waiting_on_an_event_is_carded_and_left_open(tmp_path, clock, names):
    trip = bucket("k", "考完带去海边玩。", direction_of_fit="telic", bound=["AI"])
    hold = bucket("l", "考完之前别提出去玩。", direction_of_fit="telic", exception_of=trip["id"],
                  hold="defer", cue={"condition": "考完", "phrasings": ["考完"]})
    store = Store(tmp_path, [trip, hold])
    for text, window in (("还没考完", "a"), ("考完了，终于解放", "b")):
        out = ask(store, text, window=window)
        assert ids(out) == [hold["id"]] and out["cards"][0]["kind"] == "hold"
        assert "撤条子" in out["text"] and trip["id"][:6] in out["text"]
    assert "status" not in hold["metadata"], "a card changes nothing"


# ── the ledger ──────────────────────────────────────────────────────────────

def test_once_per_window_only_after_the_host_confirms(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    first = ask(store, "甜品店", turn="t1")
    again = ask(store, "甜品店", turn="t2")
    assert ids(again) == ids(first), "offered but never confirmed: handed again"
    done, unknown = store.cues.deliver("life", "w1", turn="t2")
    assert done == [first["cards"][0]["card"]] and unknown == []
    assert ask(store, "甜品店", turn="t3")["cards"] == []
    assert ids(ask(store, "甜品店", window="w2")) == [EXAM["id"]], "another window"
    assert ids(ask(store, "甜品店", host="bot")) == [EXAM["id"]], "another host's w1"
    assert store.cues.deliver("life", "w1", cards=["nope@0"]) == ([], ["nope@0"])


def test_struck_cards_and_cleared_windows_are_handed_again(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    card = ask(store, "甜品店", turn="t1")["cards"][0]["card"]
    store.cues.deliver("life", "w1", turn="t1")
    assert store.cues.drop("life", "w1", turns=["t1"]) == [card]
    assert ids(ask(store, "甜品店", turn="t2")) == [EXAM["id"]]
    store.cues.deliver("life", "w1", cards=[card])
    assert store.cues.drop("life", "w1", everything=True) == [card]
    assert ids(ask(store, "甜品店", turn="t3")) == [EXAM["id"]]


def test_a_new_version_is_carded_again_in_the_same_window(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    first = ask(store, "甜品店", turn="t1")["cards"][0]["card"]
    store.cues.deliver("life", "w1", turn="t1")
    EXAM_MOVED = json.loads(json.dumps(EXAM))
    EXAM_MOVED["metadata"]["when"] = "2026-10-02T12:00+08:00"
    store.rows = [EXAM_MOVED]
    second = ask(store, "甜品店", turn="t2")["cards"]
    assert [c["id"] for c in second] == [EXAM["id"]] and second[0]["card"] != first
    assert "10-02 12:00" in second[0]["text"]


def test_a_lifted_hold_is_a_new_version(tmp_path, clock, names):
    hold = bucket("m", "先别催。", direction_of_fit="telic", exception_of=EXAM["id"], hold="defer")
    store = Store(tmp_path, [EXAM])
    first = ask(store, "甜品店", turn="t1")["cards"][0]["card"]
    store.cues.deliver("life", "w1", turn="t1")
    store.rows = [EXAM, hold]
    assert ask(store, "甜品店", turn="t2")["cards"] == [], "held"
    lifted = json.loads(json.dumps(hold))
    lifted["metadata"]["status"] = "resolved"
    store.rows = [EXAM, lifted]
    again = ask(store, "甜品店", turn="t3")["cards"]
    assert [c["id"] for c in again] == [EXAM["id"]] and again[0]["card"] != first


def test_the_ledger_survives_a_restart_and_another_process_sees_it(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    ask(store, "甜品店", turn="t1")
    store.cues.deliver("life", "w1", turn="t1")
    later = Store(tmp_path, [EXAM])          # a fresh process on the same library
    assert ask(later, "甜品店", turn="t2")["cards"] == []
    assert later.cues.delivered_at(EXAM["id"]) == TODAY
    assert CUED in awake_reasons(EXAM["metadata"], TODAY + timedelta(days=6),
                                 delivered_at=later.cues.delivered_at)
    assert CUED not in awake_reasons(EXAM["metadata"], TODAY + timedelta(days=8),
                                     delivered_at=later.cues.delivered_at)


def test_windows_opened_at_once_are_opened_once(tmp_path, clock):
    import threading
    ledgers = [CueLedger(tmp_path) for _ in range(8)]       # as many readers as callers
    barrier = threading.Barrier(len(ledgers))

    def go(ledger, i):
        barrier.wait()
        ledger.open_window("life", "same", [])
        ledger.open_window("life", f"own{i}", [])
    threads = [threading.Thread(target=go, args=(lg, i)) for i, lg in enumerate(ledgers)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rows = [json.loads(line) for line in
            (tmp_path / "_cue" / "ledger.jsonl").read_text(encoding="utf-8").splitlines()]
    opens = [r["window"] for r in rows if r["op"] == "open"]
    assert opens.count("same") == 1 and len(opens) == 1 + len(ledgers)


def test_compaction_keeps_the_state(tmp_path, clock, names, monkeypatch):
    from core import _cue_ledger as L
    monkeypatch.setattr(L, "COMPACT_LINES", 5)
    store = Store(tmp_path, [EXAM])
    card = ask(store, "甜品店", turn="t1")["cards"][0]["card"]
    store.cues.deliver("life", "w1", turn="t1")
    for i in range(8):
        ask(store, "没什么", window=f"x{i}", turn="t")
    fresh = CueLedger(tmp_path)
    assert fresh.is_delivered("life", "w1", card)
    assert fresh.delivered_at(EXAM["id"]) == TODAY
    assert '"op": "gen"' in (tmp_path / "_cue" / "ledger.jsonl").read_text(encoding="utf-8")


def test_cards_are_recorded_as_shown_on_their_own_road(tmp_path, clock, names):
    store = Store(tmp_path, [EXAM])
    ask(store, "甜品店")
    rows = store.usage.read()
    assert any(r["kind"] == "shown" and r["road"] == "cue.memory" and r["ids"] == [EXAM["id"]]
               for r in rows)


# ── time is up ──────────────────────────────────────────────────────────────

TONIGHT = bucket("n", "答应今晚回来说面试结果。", created=at(10), direction_of_fit="telic",
                 bound=["AI"], when="2026-10-01T19:00+08:00")


def test_time_is_up_comes_with_the_next_message_in_an_open_window(tmp_path, clock, names):
    store = Store(tmp_path, [TONIGHT])
    clock["t"] = at(10, 30)
    assert ask(store, "早", turn="t1")["cards"] == []
    clock["t"] = at(14)
    assert ask(store, "面试的事", turn="t2")["cards"] == [], "not yet"
    clock["t"] = at(19, 10)
    out = ask(store, "回来了", turn="t3")
    assert ids(out) == [TONIGHT["id"]] and out["cards"][0]["kind"] == "due"
    assert "【提醒】" in out["text"] and "约的是今天 19:00，到点了" in out["text"]
    assert "（我欠着）" in out["text"] and "AI欠着" not in out["text"]
    assert ask(store, "回来了", window="fresh")["cards"] == [], "a new window reads breath"
    store.cues.deliver("life", "w1", turn="t3")
    assert ask(store, "嗯", turn="t4")["cards"] == [], "delivered, not done: not re-sent"
    store.cues.drop("life", "w1", turns=["t3"])
    assert ids(ask(store, "嗯", turn="t5")) == [TONIGHT["id"]], "struck by compaction"


def test_a_backfilled_clock_is_a_reference_time(tmp_path, clock, names):
    guess = json.loads(json.dumps(TONIGHT))
    guess["metadata"]["backfilled"] = ["when"]
    store = Store(tmp_path, [guess])
    ask(store, "早")
    clock["t"] = at(19, 5)
    out = ask(store, "回来啦", turn="t2")
    assert ids(out) == [guess["id"]]
    assert out["text"].startswith("【提醒】") and "大约到时候了" in out["text"]
    assert "原话「今晚」" in out["text"] and "参考时间" in out["text"]
    assert "约的是" not in out["text"] and "说好的点" in out["text"]


# ── the gate ────────────────────────────────────────────────────────────────

def test_out_of_scope_reads_like_nothing_matched(tmp_path, clock, names):
    from core import scope as S
    store = Store(tmp_path, [EXAM])
    req = S.RequestScope.resolve(
        S.Host("bot", max_grant=None),
        json.dumps({"v": 1, "entry": {"system": "telegram", "instance": "b", "container": "g"},
                    "venue": "group", "audience": ["user:U"],
                    "grant": [{"system": "telegram", "instance": "b", "container": "g"}]}))
    view = S.ScopeView(req, {r["id"]: r["metadata"] for r in store.rows}, store.sources)
    scoped = ask(store, "甜品店", scope=view)
    assert scoped["cards"] == [] and scoped["text"] == ""
    assert scoped == {**ask(store, "天气", window="w9", scope=view), "window": "w1"}


def test_a_withdrawn_source_never_puts_its_text_on_a_card(tmp_path, clock, names):
    gone = bucket("o", "考完试那天说的秘密话。", direction_of_fit="telic",
                  cue={"condition": "考完", "phrasings": ["考完"]},
                  invalidation=[{"kind": "source_gone", "of": "lento:home/p#m1",
                                 "by": "withdrawn", "at": TODAY.isoformat(), "cleared": True}])
    derived = bucket("p", "猜考完会很累。", room="MIND/VIEWS",
                     prov=[{"rel": "wasDerivedFrom", "target": gone["id"]}])
    store = Store(tmp_path, [derived])
    ask(store, "早", turn="t1")                          # the window opens before the change
    pending = json.loads(json.dumps(derived))
    pending["metadata"]["invalidation"] = [
        {"kind": "source_gone", "of": "lento:home/p#m1", "by": "withdrawn",
         "at": TODAY.isoformat()}]
    store.rows = [gone, pending]
    out = ask(store, "考完了", turn="t2")
    # Both came into 依据变了的 after the window opened: id and status, no text, and the
    # cue on the withdrawn one matched nothing it may show.
    assert [c["kind"] for c in out["cards"]] == ["review", "review"]
    assert sorted(ids(out)) == sorted([gone["id"], derived["id"]])
    assert out["text"].count("（正文不给）") == 2 and "撤回" in out["text"]
    assert "秘密" not in out["text"] and "猜考完" not in out["text"]
    assert ask(store, "考完了", window="later")["cards"] == [], "breath listed it there"


# ── the route ───────────────────────────────────────────────────────────────

def test_the_routes_answer_through_the_hook_guard(tmp_path, clock, names, monkeypatch):
    from starlette.requests import Request
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    store = Store(tmp_path, [EXAM])
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setenv("T_BOT", "bot-key")
    monkeypatch.setenv("T_LIFE", "life-key")
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "config", {"hosts": {
        "life": {"token_env": "T_LIFE", "scope_mode": "open"},
        "bot": {"token_env": "T_BOT", "max_grant": [{"system": "telegram"}]}}})
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    monkeypatch.setattr(PA, "has_session", lambda r: False)

    def call(path, payload, key=None):
        raw = json.dumps(payload, ensure_ascii=False).encode()
        headers = [(b"content-type", b"application/json")]
        if key:
            headers.append((b"x-loci-hook-token", key.encode()))

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": "POST", "path": path, "headers": headers,
                       "query_string": b""}, receive)
        resp = asyncio.run(routes[("POST", path)](req))
        return resp.status_code, json.loads(resp.body)

    body = {"text": "去甜品店", "window": "w1", "turn": "t1"}
    assert call("/api/v2/cue", body)[0] == 401
    status, out = call("/api/v2/cue", body, key="bot-key")
    assert status == 403 and "Loci-Scope" in out["scope"], "a restricted host without a scope"
    status, out = call("/api/v2/cue", body, key="life-key")
    assert status == 200 and [c["id"] for c in out["cards"]] == [EXAM["id"]]
    assert out["scope"] == "〔范围：全库（open）〕"
    card = out["cards"][0]["card"]
    assert call("/api/v2/cue", {"text": "x", "window": "w1"}, key="life-key")[0] == 400
    status, got = call("/api/v2/cue/delivered", {"window": "w1", "turn": "t1"}, key="life-key")
    assert status == 200 and got == {"window": "w1", "delivered": [card], "unknown": []}
    assert call("/api/v2/cue", {**body, "turn": "t2"}, key="life-key")[1]["cards"] == []
    status, got = call("/api/v2/cue/dropped", {"window": "w1", "cards": [card]}, key="life-key")
    assert status == 200 and got == {"window": "w1", "dropped": [card], "cleared": False}
    assert call("/api/v2/cue/dropped", {"window": "w1"}, key="life-key")[0] == 400
    assert call("/api/v2/cue", {**body, "turn": "t3"}, key="life-key")[1]["cards"]


def test_who_owes_it_reads_as_the_ai_reads_it(names, monkeypatch):
    from core import profile as P
    from tools.breath import awaken as A
    names("沈慢:\n  aliases: [小慢慢]\n  instance_of: 人\n")
    monkeypatch.setattr(P, "get_ai_name", lambda: "沈慢")
    assert P.owed_names(["AI"]) == "我"
    assert P.owed_names(["沈慢", "小林"]) == "我、小林"
    assert P.owed_names(["小慢慢", "AI", "小林"]) == "我、小林", "an alias of mine, said once"
    assert P.owed_names(["小林"]) == "小林" and P.owed_names([]) == ""
    assert A._owed({"bound": ["小林", "沈慢"]}) == "（小林、我欠着）"
