# -*- coding: utf-8 -*-
"""
tests/test_panel_prompts.py — the prompt cards on grow's and dream's 高级设置
(GET / POST /api/loci/prompts, core/prompts.py).

The owner may rewrite two side-model prompts from the panel: `backfill` (the one call
that fills a new entry's blanks) and `dream` (weaving). What the side model receives is
her rewrite when there is one and the shipped text otherwise. A rewrite that lost a piece
the parser reads — backfill's `{kinds}` or one of its output keys, one of the dream JSON
line's five keys — is refused with a 400 naming it, and nothing is written. 恢复默认 goes
back to the shipped text. The rewrite lives in `<buckets>/_state/prompts.json` (per
library, inside the library folder, carried into an empty library by an export package),
never in the ledger. The write is the panel's alone: a host's credential is refused.
"""

import json

import pytest

from _panel_kit import make_store, routes, run
from core import _dream as D
from core import dehydrator as DH
from core import export_package as EP
from core import prompts as P
from core import runtime as rt
from tools.grow import _backfill as BF

BACKFILL_NOTE = ("不建议改：最后「输出格式」那一段，还有 `{kinds}` 这个占位 —— 程序按这些键名读结果，"
                 "改了名字或删了键，那一格就再也填不上；`{kinds}` 会换成你在「名字」页用的类别。")
DREAM_NOTE = ("不建议改：最后「只返回 JSON」那一行 —— 程序靠「完整」「碎片」「v」「a」「线索」"
              "这几个名字拆出梦的全文、碎片、情绪和线索，改了名字，梦就存不进去。")


@pytest.fixture
def panel(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    return {"store": store, "call": routes(monkeypatch), "file": tmp_path / "_state" / P.FILE}


class _Model:
    """Stands in for the side model: records each system text, answers `answer`."""

    def __init__(self, answer):
        self.answer = answer
        self.systems = []
        self.api_available = True

    async def _chat(self, system, user, **kw):
        self.systems.append(system)
        return self.answer


def _backfill_system(monkeypatch) -> str:
    model = _Model(json.dumps({"name": "标题", "summary": "一句话"}, ensure_ascii=False))
    monkeypatch.setattr(rt, "dehydrator", model)
    context = {"room": "EVENT/SELF", "telic": False, "created_day": None,
               "cue_condition": "", "existing": {}}
    answer, empty = run(BF._ask_backfill("b1", "今天去了海边。", context, ("人", "书")))
    assert answer is not None and not empty
    return model.systems[-1]


def _dream_system(monkeypatch) -> str:
    model = _Model(json.dumps({"完整": "走廊。", "碎片": "钥匙。", "v": 0.4, "a": 0.6,
                               "线索": []}, ensure_ascii=False))
    monkeypatch.setattr(rt, "dehydrator", model)
    monkeypatch.setattr(D, "build_user_message", lambda ingredients, c: "料")
    out = run(D.call_model({}, D.dream_config({})))
    assert out["完整"] == "走廊。"
    return model.systems[-1]


def _card(call, key):
    out = call("GET", "/api/loci/prompts")
    assert out.status == 200, out.json
    return next(c for c in out.json["items"] if c["key"] == key)


# ───────────────────────── what the card shows ─────────────────────────

def test_the_cards_show_the_shipped_text_and_the_approved_notes(panel):
    out = panel["call"]("GET", "/api/loci/prompts")
    assert out.status == 200
    assert [c["key"] for c in out.json["items"]] == ["backfill", "dream"]
    assert out.json["scope"]
    backfill, dream = out.json["items"]
    assert backfill == {"key": "backfill", "text": DH.BACKFILL_PROMPT,
                        "default": DH.BACKFILL_PROMPT, "edited": False, "changed": None,
                        "changed_at": None, "notes": [BACKFILL_NOTE]}
    assert dream["text"] == dream["default"] == D.DREAM_PROMPT
    assert dream["notes"] == [DREAM_NOTE] and dream["edited"] is False
    # The shipped texts pass their own check: nothing is missing from them.
    assert P.missing("backfill", DH.BACKFILL_PROMPT) == []
    assert P.missing("dream", D.DREAM_PROMPT) == []


# ───────────────────────── what the side model receives ─────────────────────────

def test_without_a_rewrite_the_side_model_gets_the_shipped_text(panel, monkeypatch):
    assert _backfill_system(monkeypatch) == DH.BACKFILL_PROMPT.replace("{kinds}", "人 / 书")
    assert _dream_system(monkeypatch) == D.DREAM_PROMPT
    assert not panel["file"].exists()


def test_a_saved_backfill_prompt_is_what_the_side_model_receives(panel, monkeypatch):
    mine = DH.BACKFILL_PROMPT.replace("10字以内的简短标题", "8字以内的标题")
    out = panel["call"]("POST", "/api/loci/prompts", body={"key": "backfill", "text": mine})
    assert out.status == 200 and out.json["ok"] is True, out.json
    item = out.json["item"]
    assert item["text"] == mine and item["edited"] is True
    assert item["default"] == DH.BACKFILL_PROMPT
    assert item["changed"] == item["changed_at"][:10] and len(item["changed"]) == 10
    assert _card(panel["call"], "backfill") == item
    assert _backfill_system(monkeypatch) == mine.replace("{kinds}", "人 / 书")
    # The other prompt is untouched.
    assert _dream_system(monkeypatch) == D.DREAM_PROMPT


def test_a_saved_dream_prompt_is_what_the_weave_receives(panel, monkeypatch):
    mine = D.DREAM_PROMPT.replace("300~600 字", "200~400 字")
    out = panel["call"]("POST", "/api/loci/prompts", body={"key": "dream", "text": mine})
    assert out.status == 200, out.json
    assert _dream_system(monkeypatch) == mine
    assert _backfill_system(monkeypatch) == DH.BACKFILL_PROMPT.replace("{kinds}", "人 / 书")


def test_the_rewrite_lives_in_the_librarys_state_and_not_the_ledger(panel):
    ledger = list(panel["store"].ledger_mirror.iter_events())
    mine = D.DREAM_PROMPT + "\n"
    panel["call"]("POST", "/api/loci/prompts", body={"key": "dream", "text": mine})
    kept = json.loads(panel["file"].read_text(encoding="utf-8"))
    assert kept["prompts"]["dream"]["text"] == mine and kept["prompts"]["dream"]["at"]
    assert list(panel["store"].ledger_mirror.iter_events()) == ledger
    # An export package carries it into a new library, and only into an empty one.
    spec = next(s for s in EP.STATE_FILES if s.path == f"_state/{P.FILE}")
    assert spec.merge == EP.FRESH


# ───────────────────────── a rewrite that would break parsing ─────────────────────────

@pytest.mark.parametrize("key, cut, named", [
    ("backfill", "{kinds}", "{kinds}"),
    ("backfill", '"looks_like_promise"', '"looks_like_promise"'),
    ("backfill", '"phrase"', '"phrase"'),
    ("dream", '"线索"', '"线索"'),
    ("dream", '"完整"', '"完整"'),
])
def test_a_rewrite_that_lost_a_piece_the_parser_reads_is_refused(panel, monkeypatch, key, cut,
                                                                  named):
    shipped = P.default(key)
    broken = shipped.replace(cut, "")
    out = panel["call"]("POST", "/api/loci/prompts", body={"key": key, "text": broken})
    assert out.status == 400 and named in out.json["error"], out.json
    assert not panel["file"].exists(), "nothing is written"
    assert _card(panel["call"], key)["edited"] is False


def test_a_refusal_names_every_missing_piece_and_keeps_an_earlier_rewrite(panel):
    call = panel["call"]
    mine = D.DREAM_PROMPT + "\n多一行。"
    assert call("POST", "/api/loci/prompts", body={"key": "dream", "text": mine}).status == 200
    out = call("POST", "/api/loci/prompts", body={"key": "dream", "text": "随便写写"})
    assert out.status == 400
    for piece in ('"完整"', '"碎片"', '"v"', '"a"', '"线索"'):
        assert piece in out.json["error"]
    assert _card(call, "dream")["text"] == mine


def test_a_bad_request_is_a_400(panel):
    call = panel["call"]
    assert call("POST", "/api/loci/prompts", body={"key": "slicer", "text": "x"}).status == 400
    assert call("POST", "/api/loci/prompts", body={"key": "dream"}).status == 400
    long = D.DREAM_PROMPT + "。" * P.TEXT_MAX
    assert call("POST", "/api/loci/prompts", body={"key": "dream", "text": long}).status == 400
    assert not panel["file"].exists()


def test_a_saved_rewrite_a_later_parser_cannot_read_falls_back(panel, monkeypatch):
    # A rewrite kept on disk that lacks a key (a later version reading more) is not used.
    panel["file"].parent.mkdir(parents=True, exist_ok=True)
    panel["file"].write_text(json.dumps({"version": 1, "prompts": {
        "dream": {"text": "只有一句", "at": "2026-10-01T09:00:00+08:00"}}}), encoding="utf-8")
    assert _dream_system(monkeypatch) == D.DREAM_PROMPT


# ───────────────────────── 恢复默认 ─────────────────────────

def test_reset_goes_back_to_the_shipped_text(panel, monkeypatch):
    call = panel["call"]
    mine = DH.BACKFILL_PROMPT + "\n多一行。"
    call("POST", "/api/loci/prompts", body={"key": "backfill", "text": mine})
    call("POST", "/api/loci/prompts", body={"key": "dream", "text": D.DREAM_PROMPT + "\n"})
    out = call("POST", "/api/loci/prompts", body={"key": "backfill", "reset": True})
    assert out.status == 200
    assert out.json["item"]["text"] == DH.BACKFILL_PROMPT
    assert (out.json["item"]["edited"], out.json["item"]["changed"]) == (False, None)
    assert _backfill_system(monkeypatch) == DH.BACKFILL_PROMPT.replace("{kinds}", "人 / 书")
    kept = json.loads(panel["file"].read_text(encoding="utf-8"))["prompts"]
    assert "backfill" not in kept and "dream" in kept, "only that card is reset"


def test_saving_the_shipped_text_is_no_rewrite(panel):
    out = panel["call"]("POST", "/api/loci/prompts",
                        body={"key": "dream", "text": D.DREAM_PROMPT})
    assert out.status == 200 and out.json["item"]["edited"] is False


# ───────────────────────── the panel's alone ─────────────────────────

def test_the_write_is_same_origin_json(panel):
    call = panel["call"]
    body = {"key": "dream", "text": D.DREAM_PROMPT + "\n"}
    assert call("POST", "/api/loci/prompts", body=body, origin=False).status == 403
    assert call("POST", "/api/loci/prompts", body=body, ctype="text/plain").status == 400
    assert not panel["file"].exists()


def test_a_host_credential_is_refused(panel, monkeypatch):
    from web import _shared as sh
    from web import panel_auth as PA
    assert "/api/loci/prompts" not in PA.HOOK_PATHS
    assert not PA.is_host_read("/api/loci/prompts")
    sh.config["hosts"] = {"life": {"token_env": "T_LIFE", "scope_mode": "open"}}
    monkeypatch.setenv("T_LIFE", "life-key")
    monkeypatch.delenv("LOCI_HOOK_TOKEN", raising=False)
    monkeypatch.setattr(PA, "_lock_logged", {"message": "", "at": 0.0})
    call = routes(monkeypatch, locked=True)
    body = {"key": "dream", "text": D.DREAM_PROMPT + "\n"}
    out = call("POST", "/api/loci/prompts", body=body, key="life-key")
    assert out.status == 403 and "宿主" in out.json["error"], out.json
    assert call("GET", "/api/loci/prompts", key="life-key").status == 403
    assert not panel["file"].exists()
