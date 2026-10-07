# -*- coding: utf-8 -*-
"""
tests/test_panel_breath_last.py — the panel's breath page: the last breath handed out

`GET /api/loci/breath/last` shows the breath a host's model was actually given (the tool,
or `/api/v2/breath` without `peek`), one per host, with the scope line of that moment. The
copy is kept when breath is handed out (tools/breath/awaken.keep_last) and holds no text of
any memory (core/breath_snapshot.skeleton); the titles are read now, through the gate.

The words of a 惦记的事 reason moved into core (core/profile.reason_words) so breath and the
panel say the same thing; breath's own text must not change by one byte, which the first
test pins against the text the code rendered before the move.
"""

import json
from datetime import timedelta

import pytest

from _panel_kit import make_store, routes, run
from core import _invalidation as I
from core import _when as W
from core import breath_snapshot as S
from core import scope as SC
from core.profile import reason_words
from tools.breath import awaken as A


# ── breath's text is unchanged by the move of its words ─────────────────────

def _item(i, kind, reason, **kw):
    d = {"id": (i * 12)[:12], "short": (i * 6)[:6],
         "text": f"entry {i} text that is quite long indeed ok",
         "kind": kind, "bound": ["AI", "小林"], "weight": 0.5, "reason": reason}
    d.update(kw)
    return d


PROBE = {
    "core": {"facts": {"id": "0" * 12, "short": "000000", "text": "我叫沈慢"}, "facts_extra": 1,
             "facts_covered": None, "rules": [{"id": "1" * 12, "short": "111111", "text": "rule"}],
             "rules_more": 2},
    "prospective": {"items": [
        _item("a", "dated", {"kind": "dated", "days": -3, "date": "2026-10-04", "loud": "overdue"}),
        _item("b", "dated", {"kind": "dated", "days": 0, "date": "2026-10-07", "loud": "now"}),
        _item("c", "dated", {"kind": "dated", "days": 2, "date": "2026-10-09", "loud": "soon",
                             "yearly": True}),
        _item("d", "dated", {"kind": "dated", "days": 9, "date": "2026-10-16", "loud": "near",
                             "length": "3w"}),
        _item("e", "dated", {"kind": "dated", "days": 20, "date": "2026-10-27", "loud": "far",
                             "backfilled": True}),
        _item("f", "undated", {"kind": "undated", "held": 4, "ask": "set_time"}, bound=[]),
        _item("g", "undated", {"kind": "undated", "held": 40, "ask": "still_counts"}),
        _item("h", "review", {"kind": "dated", "days": -1, "date": "2026-10-06", "loud": "overdue"},
              hold={"id": "9" * 12, "short": "999999", "text": "hold text"}),
    ], "more": 3, "questions": [{"id": "2" * 12, "short": "222222", "text": "q"}],
        "slices_pending": 2, "imports_pending": 1},
    "recent": {"text": "近三天 card", "items": []},
    "involuntary": {"items": [{"id": "3" * 12, "short": "333333", "text": "inv", "how": "linked",
                               "via": "火锅", "why": "因为最近提到火锅"}]},
    "invalidation": {"items": [], "more": 0},
    "earliest": "2026-07-14",
}

# What render_breath(PROBE) printed at f55d99e, before the reason words moved into core.
BEFORE = "\n".join([
    "═══ 核心（门口那张纸） ═══",
    "我叫沈慢",
    "   └ 这张纸是 000000000000：改它就 regrow 这个 id（正文括号里的是来源，不是这张纸）",
    "⚠️ 有 2 个 __档案事实__ 桶——只该有一个，去合并",
    "— 准则（钉着的）—",
    "· rule (111111)",
    "⚠️ 还有 2 条钉着的没排上门口（这儿只放得下 8 行）——摘几条钉，或者合并成一条。",
    "",
    "═══ 惦记的事 ═══",
    "‼️ 过了 3 天：entry a text that is quite lon（我、小林欠着） (aaaaaa)",
    "⏰ 就是今天：entry b text that is quite lon（我、小林欠着） (bbbbbb)",
    "⏰ 马上（还有 2 天）：entry c text that is quite lon（我、小林欠着）（每年） (cccccc)",
    "⏰ 快到了（还有 9 天）：entry d text that is quite lon（我、小林欠着）（说的是 3w 内） (dddddd)",
    "⏰ 记着（还有 20 天）：entry e text that is quite lon（我、小林欠着）（日子是补的：2026-10-27，"
    "不对就 trace 改 when） (eeeeee)",
    "🫀 答应了，没定时间——要不要定个时间或条件？entry f text that is quite lon (ffffff)",
    "🫀 挂了 40 天——还算数吗？entry g text that is quite lon（我、小林欠着） (gggggg)",
    "❓ 之前说先放着的那件事，现在还放着吗？entry h text that is quite lon (hhhhhh) ← 条子 (999999)：hold text",
    "…还有 3 条排不下（recall 翻得到）",
    '   └ 做完了 trace(status="resolved")；不做了 trace(status="abandoned")；定时间 trace(when="YYYY-MM-DD")，'
    '等某件事就 trace(cue={"condition": "…"})',
    '   └ 还放着：给条子定个到哪天 trace(bucket_id=条子, when="YYYY-MM-DD")；不放了：trace(bucket_id=条子, '
    'status="resolved")',
    "❓ 这条像是答应过的，要挂上吗？q (222222)",
    '   └ 是就 trace(bucket_id=…, direction_of_fit="telic", bound=["我"])；不是就不用管，不会再问',
    '📥 有 2 段原话切片等着认领：recall(view="slices")',
    '📥 有 1 段导入的原话还没核：recall(view="slices") 看候选，有空时翻原话核对、自己写',
    "",
    "═══ 近三天 ═══",
    "近三天 card",
    "",
    "═══ 忽然想起 ═══",
    "· inv (333333) —— 因为最近提到火锅",
    "",
    "📍 最早的一条落在 2026-07-14。再往前的事没有自己的条目，只可能被后来的记忆顺带提到。",
])


def test_breath_text_is_byte_identical_after_the_words_moved_into_core():
    # Criterion: every loudness, both undated asks and the review line render exactly as
    # they did before reason_words existed.
    assert A.render_breath(PROBE) == BEFORE
    # A reason carrying its words (what build_breath now hands out) renders the same.
    with_words = json.loads(json.dumps(PROBE, ensure_ascii=False))
    for it in with_words["prospective"]["items"]:
        it["reason"]["words"] = reason_words(it["reason"])
    assert A.render_breath(with_words) == BEFORE


def test_reason_words_are_the_contract_words():
    assert reason_words({"kind": "dated", "days": 4, "loud": "near"}) == "快到了（还有 4 天）"
    assert reason_words({"kind": "dated", "days": -2, "loud": "overdue"}) == "过了 2 天"
    assert reason_words({"kind": "dated", "days": 0, "loud": "now"}) == "就是今天"
    assert reason_words({"kind": "undated", "held": 40, "ask": "still_counts"}) == "挂了 40 天——还算数吗？"


# ── the copy kept when breath is handed out ─────────────────────────────────

@pytest.fixture
def store(tmp_path, monkeypatch):
    return make_store(tmp_path, monkeypatch)


SECRET = "the body nobody should find in the copy"


def _seed(store):
    async def go():
        promise = await store.create(f"Promised to fix the bike. {SECRET}", room="EVENT/SELF",
                                     direction_of_fit="telic", bound=["AI"], name="fix the bike")
        dated = await store.create(f"Her move is on the 20th. {SECRET}", room="EVENT/WORLD",
                                   name="her move",
                                   when=(W.now() + timedelta(days=4)).strftime("%Y-%m-%d"))
        return promise, dated
    return run(go())


def _file(tmp_path):
    return (tmp_path / "_state" / S.FILE).read_text(encoding="utf-8")


def test_nothing_handed_out_yet_says_so(store, monkeypatch):
    call = routes(monkeypatch)
    r = call("GET", "/api/loci/breath/last")
    assert r.status == 200
    assert r.json["breath"] is None and r.json["note"] == "还没递过" and r.json["hosts"] == []


def test_the_tool_keeps_a_copy_without_text_and_the_page_reads_titles_now(store, tmp_path,
                                                                         monkeypatch):
    promise, dated = _seed(store)
    run(A.surface_awaken())
    raw = _file(tmp_path)
    # Criterion: no memory's text and no `text` key at any depth reach the copy.
    assert SECRET not in raw and '"text"' not in raw
    kept = json.loads(raw)["hosts"][""]["breath"]
    assert [it["id"] for it in kept["prospective"]["items"]] == [dated, promise]
    assert kept["prospective"]["items"][0]["reason"]["words"] == "快到了（还有 4 天）"

    call = routes(monkeypatch)
    r = call("GET", "/api/loci/breath/last")
    assert r.status == 200
    page = r.json
    assert page["host"] == "" and page["scope"] == SC.OPEN_LINE and page["at"]
    items = page["breath"]["prospective"]["items"]
    assert [(it["id"], it["short"], it["text"]) for it in items] == [
        (dated, dated[:6], "her move"), (promise, promise[:6], "fix the bike")]
    assert items[0]["reason"]["words"] == reason_words(items[0]["reason"])


def test_an_entry_deleted_since_says_so(store, tmp_path, monkeypatch):
    promise, _dated = _seed(store)
    run(A.surface_awaken())
    run(store.delete(promise))
    page = routes(monkeypatch)("GET", "/api/loci/breath/last").json
    [gone] = [it for it in page["breath"]["prospective"]["items"] if it["id"] == promise]
    assert gone["state_words"] == "在归档区（已删除）"


def test_one_copy_per_host_with_the_scope_line_of_that_moment(store, tmp_path, monkeypatch):
    _seed(store)
    b = run(A.build_breath())
    for name in ("lento", "phone"):
        req = SC.RequestScope(SC.Host(name, scope_mode=SC.OPEN), SC.OPEN)
        with SC.request_scope(req):
            A.keep_last(b)
    assert S.hosts(str(tmp_path))[0] == "phone"
    call = routes(monkeypatch)
    assert call("GET", "/api/loci/breath/last").json["host"] == "phone"
    lento = call("GET", "/api/loci/breath/last", "host=lento").json
    assert lento["host"] == "lento" and lento["scope"] == SC.OPEN_LINE
    assert set(lento["hosts"]) == {"lento", "phone"}
    nobody = call("GET", "/api/loci/breath/last", "host=nobody").json
    assert nobody["breath"] is None and nobody["note"] == "还没递过"


def test_the_host_route_keeps_a_copy_and_a_peek_does_not(store, tmp_path, monkeypatch):
    _seed(store)
    call = routes(monkeypatch)
    assert call("GET", "/api/v2/breath", "peek=1&format=json", key="s3cret").status == 200
    assert not (tmp_path / "_state" / S.FILE).exists()
    assert call("GET", "/api/v2/breath", "format=json", key="s3cret").status == 200
    entry = call("GET", "/api/loci/breath/last").json
    assert entry["breath"] is not None and entry["scope"] == SC.OPEN_LINE
    assert entry["host"] in S.hosts(str(tmp_path))


def test_the_json_skin_carries_the_reason_words(store, monkeypatch):
    _seed(store)
    body = routes(monkeypatch)("GET", "/api/v2/breath", "peek=1&format=json", key="s3cret").json
    for it in body["prospective"]["items"]:
        assert it["reason"]["words"] == reason_words(it["reason"])


def test_the_page_is_behind_the_panel_gate(store, monkeypatch):
    call = routes(monkeypatch, locked=True)
    assert call("GET", "/api/loci/breath/last").status == 401


# ── 依据变了的: why each item is there ──────────────────────────────────────

NOTE = "not nervous, tired"


def _disputed(store):
    async def go():
        judgement = await store.create(f"She wants company when nervous. {SECRET}",
                                       room="MIND/VIEWS", evidential="inference",
                                       name="company when nervous")
        await store.add_invalidation_record(
            judgement, I.dispute_record(judgement, NOTE, W.now().isoformat(timespec="seconds")))
        return judgement
    return run(go())


def test_a_moved_item_carries_why_in_the_words_breath_showed(store, tmp_path, monkeypatch):
    # Criterion: the page's `why` is, phrase for phrase, what the model read after the
    # item's title — her note included — while the copy keeps neither the note nor any
    # other text.
    judgement = _disputed(store)
    text = run(A.surface_awaken())
    raw = _file(tmp_path)
    assert NOTE not in raw and SECRET not in raw and '"text"' not in raw
    page = routes(monkeypatch)("GET", "/api/loci/breath/last").json
    [it] = page["breath"]["invalidation"]["items"]
    assert it["id"] == judgement and it["text"] == "company when nervous"
    [why] = it["why"]
    assert why.startswith("人在面板上说这条不对（") and why.endswith(f"）：「{NOTE}」")
    assert f"· company when nervous ({judgement[:6]}) —— {why}" in text


def test_her_note_is_read_now_and_left_out_once_the_entry_cannot_be_read(store, tmp_path):
    # Criterion: the note is not kept; it comes from the entry at reading time, so an
    # entry the gate no longer gives (here: gone from the library) loses it, and the
    # phrase stops at the day, as breath words a dispute left without a note.
    judgement = _disputed(store)
    run(A.surface_awaken())
    kept = S.load(str(tmp_path))["breath"]
    [gone] = S.relabel(kept, [])["invalidation"]["items"]
    assert gone["text"] is None and gone["state_words"] == "找不到了"
    [why] = gone["why"]
    assert why.startswith("人在面板上说这条不对（") and why.endswith("）") and NOTE not in why
    [here] = S.relabel(kept, run(store.list_all(include_archive=True)))["invalidation"]["items"]
    assert here["id"] == judgement and here["why"][0].endswith(f"「{NOTE}」")


def test_why_words_are_the_contract_words_for_every_reason():
    item = {"edited": True,
            "disputed": [{"at": "2026-10-05T09:00:00+08:00", "text": "x" * 90}],
            "overturned": [{"of": "a" * 12, "by": "b" * 12, "at": "2026-10-04T08:00:00+08:00"}],
            "revised": [{"source": "lento:home/p#m_1", "revision": "sha256:" + "f" * 64}],
            "basis_revised": [{"source": "lento:home/p#m_2", "revision": "r9", "via": "c" * 12}],
            "restored": [{"source": "lento:home/p#m_3", "at": "2026-10-03"}],
            "failed": [{"source": "lento:home/p#m_4", "state": "withdrawn"}],
            "remaining": ["lento:home/p#m_5"]}
    assert I.why_words(item) == [
        "人在面板上改过，你还没看",
        f"人在面板上说这条不对（10-05）：「{'x' * 80}…」",
        "它站着的 aaaaaa 被 bbbbbb 推翻了（10-04）",
        "来源 lento:home/p#m_1 出了新版本（sha256:fffffffff）",
        "它站着的 cccccc 的来源 lento:home/p#m_2 出了新版本（r9）",
        "来源 lento:home/p#m_3 撤回或删除过、现在恢复了，这条是从站在它上面的记忆派生的，等你看过才回来",
        "依据 lento:home/p#m_4 已撤回，正文不给了；还剩 lento:home/p#m_5：只凭它们重写",
    ]
