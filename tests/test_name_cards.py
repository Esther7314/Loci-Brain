# -*- coding: utf-8 -*-
"""
tests/test_name_cards.py — a card is one MIND entry filed as the card of a name
(stage 4.3).

`card_of` is written by grow(kind="mind") and trace, read back by recall by id. What the
name is decides the room once the names table knows it: a person's card is MIND/TRAITS,
a thing's MIND/VIEWS; an unknown name is taken in either and the reply says the table does
not know yet. One live card per name; regrow carries the field, so a reworded card is
still the card.
"""

import asyncio

import pytest

import tools.grow as grow_mod
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools import _subjects as S
from tools.grow import rooms_path
from tools.recall.core import recall_core
from tools.regrow import dispatch as regrow
from tools.trace.core import trace_core

TABLE = """\
Connor:
  aliases: [RK800]
  instance_of: 人
  present_in: [Detroit]
Detroit:
  instance_of: 游戏
__不是人__:
  - Galaxy
"""


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path / "buckets")})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    table = tmp_path / "aliases.yaml"
    table.write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(table))
    monkeypatch.setattr(S, "_cache", None)
    return mgr


def run(coro):
    return asyncio.run(coro)


async def _source(store) -> str:
    return await store.create("We played Detroit together until late.", tags=["t"],
                              room="EVENT/SELF")


async def _card(store, room: str, name: str, text: str = "How I see it.") -> str:
    src = await _source(store)
    return await grow_mod.dispatch(kind="mind", room=room, text=text, from_=[src],
                                   v=0.6, a=0.4, card_of=name)


def _id(reply: str) -> str:
    return reply.split("🧠mind→", 1)[1].split()[0]


async def _card_of(store, bid: str):
    return ((await store.get_including_archive(bid)) or {}).get("metadata", {}).get("card_of")


def test_grow_writes_a_card_and_recall_by_id_names_it(store):
    async def go():
        out = await _card(store, "MIND/TRAITS", "RK800")
        assert "[名字卡:Connor]" in out, out
        bid = _id(out)
        # An alias spelling is filed under the entry it stands for.
        assert await _card_of(store, bid) == "Connor"
        shown = await recall_core("", "", "", bid)
        assert "名字卡:Connor" in shown
    run(go())


def test_a_person_card_is_traits_and_a_thing_card_is_views(store):
    async def go():
        refused = await _card(store, "MIND/VIEWS", "Connor")
        assert "MIND/TRAITS" in refused and "🧠" not in refused
        refused = await _card(store, "MIND/TRAITS", "Detroit")
        assert "MIND/VIEWS" in refused and "游戏" in refused
        # A name on the not-a-person list is a thing, though the table names no kind.
        refused = await _card(store, "MIND/TRAITS", "Galaxy")
        assert "MIND/VIEWS" in refused
        ok = await _card(store, "MIND/VIEWS", "Galaxy")
        assert "[名字卡:Galaxy]" in ok, ok
        cards = [b for b in await store.list_all() if b["metadata"].get("card_of")]
        assert len(cards) == 1, "a refused card is not written"
    run(go())


def test_an_unknown_name_is_taken_in_either_mind_room_and_the_reply_says_so(store):
    async def go():
        out = await _card(store, "MIND/VIEWS", "Kara")
        assert "[名字卡:Kara]" in out
        assert "人名表里还不知道「Kara」是什么" in out
    run(go())


def test_an_event_is_never_a_card(store):
    async def go():
        out = await grow_mod.dispatch(
            kind="event", card_of="Connor",
            items=[{"room": "EVENT/SELF", "text": "Met Connor.", "v": 0.5, "a": 0.5}])
        assert "名字卡是一条 mind" in out
        event = await _source(store)
        out = await trace_core(bucket_id=event, card_of="Connor")
        assert "名字卡是一条 mind" in out
        assert await _card_of(store, event) is None
    run(go())


def test_one_live_card_per_name_and_regrow_keeps_it_the_card(store):
    async def go():
        first = _id(await _card(store, "MIND/TRAITS", "Connor"))
        refused = await _card(store, "MIND/TRAITS", "connor")
        assert first in refused and "已经有名字卡了" in refused
        # A reworded card is still the card: the new version carries card_of, and the
        # name still has exactly one live card — the new one.
        out = await regrow(bucket_id=first, text="How I see him now.", v=0.6, a=0.4,
                           mode="supplement")
        assert "→" in out, out
        newer = (await store.get(first))["metadata"]["superseded_by"]
        assert await _card_of(store, newer) == "Connor"
        refused = await _card(store, "MIND/TRAITS", "Connor")
        assert newer in refused
        # Archiving the newest version brings the one before it back, the way a cover
        # whose gist was archived stops covering: that version is the card again.
        assert await store.delete(newer)
        assert first in await _card(store, "MIND/TRAITS", "Connor")
        # With the whole chain archived, the name is free.
        assert await store.delete(first)
        assert "[名字卡:Connor]" in await _card(store, "MIND/TRAITS", "Connor")
    run(go())


def test_trace_sets_clears_and_rechecks_a_card_on_a_move(store):
    async def go():
        src = await _source(store)
        mind = _id(await grow_mod.dispatch(kind="mind", room="MIND/TRAITS",
                                           text="Patient, until he is not.",
                                           from_=[src], v=0.5, a=0.5))
        out = await trace_core(bucket_id=mind, card_of="Connor")
        assert await _card_of(store, mind) == "Connor", out
        # Setting it again on the same entry is not a second card.
        out = await trace_core(bucket_id=mind, card_of="RK800")
        assert "已经有名字卡了" not in out
        # A person's card does not drift into MIND/VIEWS, nor out of MIND at all.
        out = await trace_core(bucket_id=mind, room="MIND/VIEWS")
        assert "MIND/TRAITS" in out
        assert (await store.get(mind))["metadata"]["room"] == "MIND/TRAITS"
        out = await trace_core(bucket_id=mind, room="EVENT/WORLD")
        assert "名字卡是一条 mind" in out
        # Another entry cannot take the name while this one holds it.
        other = _id(await grow_mod.dispatch(kind="mind", room="MIND/TRAITS",
                                            text="Another view.", from_=[src],
                                            v=0.5, a=0.5))
        out = await trace_core(bucket_id=other, card_of="Connor")
        assert mind in out
        # "" takes it off; then the move is an ordinary move.
        await trace_core(bucket_id=mind, card_of="")
        assert await _card_of(store, mind) is None
        await trace_core(bucket_id=mind, room="MIND/VIEWS")
        assert (await store.get(mind))["metadata"]["room"] == "MIND/VIEWS"
        assert await _card_of(store, other) is None
        out = await trace_core(bucket_id=other, card_of="Connor")
        assert await _card_of(store, other) == "Connor", out
    run(go())


def test_card_of_names_a_name_not_a_pronoun_or_a_shared_alias(store, tmp_path):
    async def go():
        assert "写名字" in await _card(store, "MIND/TRAITS", "我")
        (tmp_path / "aliases.yaml").write_text(
            TABLE + "Leon:\n  - Lee\nLeon (Detroit):\n  - Lee\n", encoding="utf-8")
        S._cache = None
        out = await _card(store, "MIND/VIEWS", "Lee")
        assert "好几个名字底下" in out and "Leon (Detroit)" in out
        assert "[名字卡:Leon]" in await _card(store, "MIND/VIEWS", "Leon")
    run(go())
