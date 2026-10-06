# -*- coding: utf-8 -*-
"""
tests/test_scope_write_ids.py — under a read scope, a write tool given the id of an entry
the scope may not read answers exactly as for an id that names nothing, and writes nothing.

Every write tool that takes an id (trace, regrow, fold's cover, grow's `from`) resolves it
through tools/_common.resolve_bucket_id. A full id goes through the scope there as a short
handle does: an id out of scope is "查无此桶", word for word the answer for an id that does
not exist, and the entry's file stays as it was. A readable `feel_…` id of any shape is an
id of the library too, and is asked of the scope the same way. Outside a scope (an open
host) every one of these ids still resolves as before.
"""

import asyncio
import json

import pytest

import tools.grow as grow_mod
from core import _sources as S
from core import scope as SC
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.fold import dispatch as fold
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.regrow import dispatch as regrow
from tools.trace.core import trace_core as trace

GROUP = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
BOT = SC.Host("book-bot", max_grant=(S.Place("telegram", "bot-a"),), token="bot-key")
SCOPE = json.dumps({"v": 1, "entry": GROUP, "venue": "group", "audience": ["user:U"],
                    "grant": [GROUP]})
GHOST = "0123456789ab"                  # the right shape, never created
GHOST_FEEL = "feel_0000_nobody"         # a readable id, never created


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
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def run(coro):
    return asyncio.run(coro)


def scoped(coro):
    async def go():
        with SC.request_scope(SC.RequestScope.resolve(BOT, SCOPE)):
            return await coro
    return run(go())


def _library(store) -> dict:
    async def seed():
        return {
            "group": await store.create(
                "The reading club meets in the south hall.", room="MIND/VIEWS",
                sources=[{**GROUP, "id": "m_1", "use": {"venues": ["group"]}}]),
            "private": await store.create(
                "She has a counselling slot on Saturday.", room="MIND/VIEWS",
                sources=[{**GROUP, "id": "m_2", "use": {"venues": ["private"]}}]),
            "feel": await store.create(
                "Quietly glad the week is over.", room="MIND/VIEWS",
                bucket_id_override="feel_0801_private",
                sources=[{**GROUP, "id": "m_3", "use": {"venues": ["private"]}}]),
        }
    ids = run(seed())
    assert ids["feel"] == "feel_0801_private"
    return ids


def _bytes(store, bid) -> bytes:
    with open(store._find_bucket_file(bid), "rb") as f:
        return f.read()


def _same_as_missing(hidden_out: str, hidden: str, missing_out: str, missing: str) -> None:
    assert "查无此桶" in hidden_out, hidden_out
    assert hidden_out.replace(hidden, "<ID>") == missing_out.replace(missing, "<ID>")


def test_trace_on_an_entry_out_of_scope_is_trace_on_nothing(store):
    ids = _library(store)
    before = _bytes(store, ids["private"])
    hidden = scoped(trace(bucket_id=ids["private"], status="resolved"))
    missing = scoped(trace(bucket_id=GHOST, status="resolved"))
    _same_as_missing(hidden, ids["private"], missing, GHOST)
    assert _bytes(store, ids["private"]) == before, "the private entry was not touched"
    # The handle of it is nothing too, and the entry in scope is closed as asked.
    assert "查无此桶" in scoped(trace(bucket_id=ids["private"][:6], status="resolved"))
    assert "status=resolved" in scoped(trace(bucket_id=ids["group"], status="resolved"))
    # Without a scope the same id is the entry it names.
    assert "status=resolved" in run(trace(bucket_id=ids["private"], status="resolved"))


def test_a_readable_id_of_any_shape_is_asked_of_the_scope(store):
    ids = _library(store)
    before = _bytes(store, ids["feel"])
    hidden = scoped(trace(bucket_id=ids["feel"], status="resolved"))
    missing = scoped(trace(bucket_id=GHOST_FEEL, status="resolved"))
    _same_as_missing(hidden, ids["feel"], missing, GHOST_FEEL)
    assert _bytes(store, ids["feel"]) == before
    hidden = scoped(grow(kind="mind", room="MIND/VIEWS", text="Weekends feel lighter.",
                         from_=[ids["feel"]], v=0.5, a=0.4))
    missing = scoped(grow(kind="mind", room="MIND/VIEWS", text="Weekends feel lighter.",
                          from_=[GHOST_FEEL], v=0.5, a=0.4))
    _same_as_missing(hidden, ids["feel"], missing, GHOST_FEEL)
    assert "status=resolved" in run(trace(bucket_id=ids["feel"], status="resolved"))


def test_regrow_of_an_entry_out_of_scope_writes_nothing(store):
    ids = _library(store)
    before = _bytes(store, ids["private"])
    count = len(run(store.list_all(include_archive=True)))
    hidden = scoped(regrow(bucket_id=ids["private"], text="Saturday is taken.", v=0.5, a=0.4,
                           mode="supplement"))
    missing = scoped(regrow(bucket_id=GHOST, text="Saturday is taken.", v=0.5, a=0.4,
                            mode="supplement"))
    _same_as_missing(hidden, ids["private"], missing, GHOST)
    assert _bytes(store, ids["private"]) == before
    assert len(run(store.list_all(include_archive=True))) == count


def test_fold_and_grow_from_refuse_an_entry_out_of_scope_as_one_that_is_not_there(store):
    ids = _library(store)
    count = len(run(store.list_all(include_archive=True)))
    hidden = scoped(fold(text="Weekends are full.", room="MIND/VIEWS", v=0.5, a=0.4,
                         cover=[ids["group"], ids["private"]]))
    missing = scoped(fold(text="Weekends are full.", room="MIND/VIEWS", v=0.5, a=0.4,
                          cover=[ids["group"], GHOST]))
    _same_as_missing(hidden, ids["private"], missing, GHOST)
    hidden = scoped(grow(kind="mind", room="MIND/VIEWS", text="Saturdays fill up.",
                         from_=[ids["private"]], v=0.5, a=0.4))
    missing = scoped(grow(kind="mind", room="MIND/VIEWS", text="Saturdays fill up.",
                          from_=[GHOST], v=0.5, a=0.4))
    _same_as_missing(hidden, ids["private"], missing, GHOST)
    assert len(run(store.list_all(include_archive=True))) == count, "nothing was written"
    # The entry in scope is taken as a source.
    out = scoped(grow(kind="mind", room="MIND/VIEWS", text="The club is a fixed point.",
                      from_=[ids["group"]], v=0.5, a=0.4))
    assert out.startswith("🧠mind→"), out


# ───────────────────────── name cards ─────────────────────────

def test_a_card_out_of_scope_is_not_named_by_the_one_card_per_name_rule(store):
    private_card = run(store.create(
        "Ming keeps every promise.", room="MIND/TRAITS", card_of="Ming",
        sources=[{**GROUP, "id": "m_7", "use": {"venues": ["private"]}}]))
    entry = run(store.create(
        "Ming runs the club's sign-up sheet.", room="MIND/TRAITS",
        sources=[{**GROUP, "id": "m_8", "use": {"venues": ["group"]}}]))
    # In the group the name has no card yet: one is written, and the private one is not named.
    out = scoped(grow(kind="mind", room="MIND/TRAITS", text="Ming is quick to help.",
                      card_of="Ming", sources=[{**GROUP, "id": "m_9"}], v=0.5, a=0.4))
    assert out.startswith("🧠mind→") and private_card not in out, out
    group_card = out.split("🧠mind→", 1)[1].split()[0]
    # Now it has one the group may read: that one is named, never the private one.
    out = scoped(trace(bucket_id=entry, card_of="Ming"))
    assert "已经有名字卡了" in out and group_card in out and private_card not in out, out
    # Without a scope the one card the name has is named.
    out = run(grow(kind="mind", room="MIND/TRAITS", text="Ming is kind.", card_of="Ming",
                   v=0.5, a=0.4))
    assert "已经有名字卡了" in out and private_card in out


# ───────────────────────── from= a source outside the grant ─────────────────────────

def _from(source: str) -> str:
    return scoped(grow(kind="mind", room="MIND/VIEWS", text="Something said elsewhere.",
                       from_=[source], v=0.5, a=0.4))


def test_a_source_outside_the_grant_gets_one_answer_whatever_its_state(store):
    registry = store.sources
    run(registry.apply_change({"change_id": "w-1", "source": "telegram:bot-a/group:H#m_8",
                               "kind": "withdrawn", "host_seq": 1}))
    registry.record_order({**GROUP, "container": "group:H"}, ["m_1", "m_2", "m_3"])
    active = _from("telegram:bot-a/group:H#m_9")
    withdrawn = _from("telegram:bot-a/group:H#m_8")
    known_run = _from("telegram:bot-a/group:H#m_1..m_3")
    unknown_run = _from("telegram:bot-a/group:K#m_1..m_3")
    assert "不在这一轮" in active, active
    assert withdrawn.replace("#m_8", "#X") == active.replace("#m_9", "#X")
    assert known_run.replace("group:H#m_1..m_3", "X") == active.replace("group:H#m_9", "X")
    assert unknown_run.replace("group:K#m_1..m_3", "X") == active.replace("group:H#m_9", "X")
    # Inside the grant the state is the scope's to know, and is said.
    run(registry.apply_change({"change_id": "w-2", "source": "telegram:bot-a/group:G#m_5",
                               "kind": "withdrawn", "host_seq": 1}))
    assert "撤回" in _from("telegram:bot-a/group:G#m_5")
    assert "没交过这段里有哪几行" in _from("telegram:bot-a/group:G#m_1..m_4")
