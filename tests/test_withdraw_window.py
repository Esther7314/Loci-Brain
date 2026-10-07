# -*- coding: utf-8 -*-
"""
tests/test_withdraw_window.py — between the registry recording a withdrawal and the
clearing writing its records, nothing that stood on the source is read.

A host's withdrawn change is recorded in the source registry first; the `source_gone`
records on what stood on the source are written after, one memory at a time
(core/_source_change.py). The clearing is paused right there — the registry says
withdrawn, no memory carries a record yet — and every read exit is asked: the cue route
open and under a read scope, recall by id, breath, and the panel's detail window and
awake list. Each is
asked the same before the change (the positive control: it does show the memory, for the
derived one too, open and scoped) and after the clearing finished.
"""

import asyncio
import json

import pytest
from starlette.requests import Request

from core import _cue as C
from core import _source_change as SC
from core import scope as SCOPE
from core.scope import Host
from tools.breath import awaken as A
from tools.recall import core as R
from utils import WAS_DERIVED_FROM
from web import loci_detail as LD
from web import loci_mind as LM

from _panel_kit import make_store

M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0007"}
M_STR = "lento:home/private:U#m_0007"
OPEN_HOST = Host("life", scope_mode="open", may_restore=True)
OWN = "海边的蓝色风车"          # the memory resting on M
GREW = "周六总想出门吹风"       # the memory derived from it


@pytest.fixture
def library(tmp_path, monkeypatch):
    monkeypatch.delenv("LOCI_ALIAS_TABLE", raising=False)
    monkeypatch.setenv("LOCI_BUCKETS_DIR", str(tmp_path))
    (tmp_path / "aliases.yaml").write_text("", encoding="utf-8")
    store = make_store(tmp_path, monkeypatch)
    e = asyncio.run(store.create(f"小周说周六去看{OWN}。", room="EVENT/WORLD", sources=[M],
                                 direction_of_fit="telic",
                                 cue={"condition": "看风车", "phrasings": ["风车"]}))
    d = asyncio.run(store.create(f"{GREW}。", room="MIND/TRAITS",
                                 prov=[{"rel": WAS_DERIVED_FROM, "target": e}],
                                 direction_of_fit="telic",
                                 cue={"condition": "出门吹风", "phrasings": ["吹风"]}))
    return store, e, d


def _open():
    return SCOPE.RequestScope.resolve(OPEN_HOST, None)


def _scoped():
    return SCOPE.RequestScope.resolve(
        Host("bot", max_grant=None),
        json.dumps({"v": 1, "entry": {"system": "telegram", "instance": "b"},
                    "venue": "private", "audience": ["user:U"],
                    "grant": [{k: M[k] for k in ("system", "instance", "container")}]}))


async def _cards(store, req, text, window) -> list[str]:
    status, out = await C.handle_cue(store, {"text": text, "window": window, "turn": "t"},
                                     req)
    assert status == 200, out
    return [c["id"] for c in out["cards"]]


async def _panel(bid) -> dict:
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b"", "path_params": {"bucket_id": bid}})
    return json.loads((await LD.api_loci_bucket(req)).body)


async def _reads(store, e, d, tag: str) -> dict:
    """What every read exit gives of the two memories (each window fresh)."""
    breath = A.render_breath(await A.build_breath())
    return {
        "cue_open": await _cards(store, _open(), "去看风车，顺便吹风", f"o-{tag}"),
        "cue_scoped": await _cards(store, _scoped(), "去看风车，顺便吹风", f"s-{tag}"),
        "recall_e": await R.recall_core(when="", room="", tag="", query=e),
        "recall_d": await R.recall_core(when="", room="", tag="", query=d),
        "breath": breath,
        "panel_e": (await _panel(e)).get("content", ""),
        "panel_awake": json.dumps(await LM.build_awake(), ensure_ascii=False),
    }


def _assert_shown(got: dict, e: str, d: str) -> None:
    assert sorted(got["cue_open"]) == sorted([e, d]), got["cue_open"]
    assert sorted(got["cue_scoped"]) == sorted([e, d]), got["cue_scoped"]
    assert OWN in got["recall_e"] and GREW in got["recall_d"]
    assert OWN in got["breath"] and GREW in got["breath"]
    assert OWN in got["panel_e"]
    assert OWN in got["panel_awake"] and GREW in got["panel_awake"]


def _assert_hidden(got: dict) -> None:
    assert got["cue_open"] == [] and got["cue_scoped"] == [], got
    for k in ("recall_e", "recall_d", "breath", "panel_e", "panel_awake"):
        assert OWN not in got[k] and GREW not in got[k], (k, got[k])


def test_nothing_resting_on_a_source_is_read_while_its_withdrawal_is_cleared(library):
    store, e, d = library

    async def main():
        before = await _reads(store, e, d, "before")
        paused, go = asyncio.Event(), asyncio.Event()
        real = store.add_invalidation_record

        async def held(bid, record):
            paused.set()
            await go.wait()
            return await real(bid, record)
        store.add_invalidation_record = held
        change = asyncio.create_task(SC.handle(store, {
            "change_id": "c-1", "source": M_STR, "host_seq": 1, "change": "withdrawn"},
            OPEN_HOST))
        await paused.wait()
        assert store.sources.state_of(M) == "withdrawn"
        assert not any(r.get("kind") == "source_gone"
                       for bid in (e, d)
                       for r in (store.meta_of(bid) or {}).get("invalidation") or [])
        during = await _reads(store, e, d, "during")
        go.set()
        status, out = await change
        assert status == 200 and out["status"] == "applied", out
        after = await _reads(store, e, d, "after")
        return before, during, after

    before, during, after = asyncio.run(main())
    _assert_shown(before, e, d)
    _assert_hidden(during)
    _assert_hidden(after)
    # Read by id, the memory reads as one standing on a withdrawn source, not as missing.
    assert "依据的来源被撤回或删除了" in during["recall_e"]


def test_a_registry_with_nothing_withdrawn_is_not_walked(library, monkeypatch):
    store, e, _d = library
    view = asyncio.run(SCOPE.view_of(store, None))
    assert view.whole_library and view.metas == {}, "nothing to walk for"
    monkeypatch.setattr(store, "meta_of", lambda bid: pytest.fail("read by id"))
    assert not view.source_blocked(asyncio.run(store.get(e)))


# ── the card ledger keeps no words of what was cleared ─────────────────────

RAIN = "明天可能下雨要带伞。"


def _turn_cards(store, window: str, buckets) -> dict:
    from datetime import timedelta

    from core import _when as W
    from core import activity as ACT
    now = W.now() + timedelta(minutes=1)
    out = ACT.turns(store.cues, [], buckets, host=OPEN_HOST.name, window=window, now=now,
                    offset=0, limit=20, as_of=now)
    return {c["id"]: c for item in out["items"] for c in item["cards"]}


def test_the_clearing_blanks_why_a_card_was_picked_and_keeps_the_rest(library):
    store, e, _d = library
    x = asyncio.run(store.create(RAIN, room="EVENT/SELF", direction_of_fit="telic",
                                 cue={"condition": "下雨", "phrasings": ["下雨"]}))
    shown = asyncio.run(_cards(store, _open(), "去看风车，怕下雨", "w"))
    assert sorted(shown) == sorted([e, x])
    store.cues.deliver(OPEN_HOST.name, "w", turn="t")
    before = _turn_cards(store, "w", asyncio.run(store.list_all()))
    assert before[e]["why"] == "看风车" and before[x]["why"] == "下雨", "positive control"
    key = before[e]["card"]

    status, out = asyncio.run(SC.handle(store, {
        "change_id": "c-1", "source": M_STR, "host_seq": 1, "change": "withdrawn"},
        OPEN_HOST))
    assert status == 200 and out["cleanup"]["cue_ledger"] == "done", out
    raw = store.cues.path.read_bytes().decode("utf-8")
    assert "风车" not in raw and "下雨" in raw
    after = _turn_cards(store, "w", asyncio.run(store.list_all(include_archive=True)))
    assert after[e]["why"] == "" and after[e]["card"] == key
    assert after[e]["state"] == "delivered" and after[x]["why"] == "下雨"
    assert store.cues.is_delivered(OPEN_HOST.name, "w", key), "the delivery is kept"


def test_the_replay_says_no_why_where_the_memory_is_not_shown(library):
    """A ledger written before the clearing blanked anything: the panel still says no why
    for a card whose memory stands on a withdrawn source."""
    store, e, _d = library
    asyncio.run(_cards(store, _open(), "去看风车", "w"))
    rows = asyncio.run(store.list_all())
    assert _turn_cards(store, "w", rows)[e]["why"] == "看风车", "positive control"
    gone = [{**r, "metadata": {**r["metadata"], "invalidation": [
        {"kind": "source_gone", "of": M_STR, "by": "withdrawn", "at": "2026-10-01T10:00:00"}]}}
        if r["metadata"]["id"] == e else r for r in rows]
    card = _turn_cards(store, "w", gone)[e]
    assert card["why"] == "" and card["text"] == "" and card["state"] == "offered"
