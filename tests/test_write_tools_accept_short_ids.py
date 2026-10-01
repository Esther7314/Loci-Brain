# -*- coding: utf-8 -*-
"""
tests/test_write_tools_accept_short_ids.py — the handle a read tool prints is the
handle a write tool accepts.

breath and recall show a 6-character handle next to every item. A model that reads an
item there and closes it with trace, re-versions it with regrow, folds it, or names it
in grow's `from` passes exactly that handle on. Each write tool resolves it to the one
full id it names (over the whole store, archive included), refuses with the candidates
when several match, and says plainly when none does — and whatever lands on disk holds
full ids. Checked through the tools' dispatch against a real BucketManager, with the
results read back from the files.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.fold import dispatch as fold
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.recall import core as R
from tools.regrow import dispatch as regrow
from tools.trace.core import trace_core as trace

EVENT = "Saturday we went up the hill."
FIRST_VIEW = "Tired days are the days they want to go out."
SECOND_VIEW = "A walk resets the week better than a lie-in."

# Two ids that share their first six characters, and one that shares nothing.
TWIN_A = "abcdef111111"
TWIN_B = "abcdef222222"
LONE = "b13001000000"
GHOST = "0123456789ab"   # the right shape, never created


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


def _files(tmp_path):
    return sorted(p for p in tmp_path.rglob("*.md"))


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


async def _lone(store):
    bid = await store.create(EVENT, room="EVENT/SELF", bucket_id_override=LONE)
    assert bid == LONE
    return bid


async def _twins(store):
    a = await store.create(FIRST_VIEW, room="MIND/VIEWS", bucket_id_override=TWIN_A)
    b = await store.create(SECOND_VIEW, room="MIND/VIEWS", bucket_id_override=TWIN_B)
    assert (a, b) == (TWIN_A, TWIN_B)
    return a, b


# ───────────────────────── trace ─────────────────────────

def test_trace_closes_an_item_by_the_handle_breath_prints(store, tmp_path):
    async def go():
        await _lone(store)
        out = await trace(bucket_id=LONE[:6], status="resolved")
        # Criterion: the receipt names the full id, and the close is on disk.
        assert LONE in out and "status=resolved" in out
        assert _disk(tmp_path, LONE)["status"] == "resolved"
    run(go())


def test_trace_edits_metadata_by_a_short_id(store, tmp_path):
    async def go():
        await _lone(store)
        out = await trace(bucket_id=LONE[:7], name="Up the hill")
        assert LONE in out
        assert _disk(tmp_path, LONE)["name"] == "Up the hill"
    run(go())


def test_trace_restores_an_archived_entry_by_its_short_id(store):
    async def go():
        await _lone(store)
        assert await store.delete(LONE)
        # The archive is part of the store the prefix is matched over, so the
        # answer is "restored", never "no such bucket".
        out = await trace(bucket_id=LONE[:6], restore=True)
        assert LONE in out and "恢复" in out
        assert await store.get(LONE)
    run(go())


def test_trace_keeps_its_own_message_for_a_full_id_that_does_not_exist(store):
    async def go():
        await _lone(store)
        assert await trace(bucket_id=GHOST, status="resolved") == f"未找到记忆桶: {GHOST}"
    run(go())


# ───────────────────────── regrow ─────────────────────────

def test_regrow_takes_the_old_version_by_short_id(store, tmp_path):
    async def go():
        await _lone(store)
        out = await regrow(bucket_id=LONE[:6], text=EVENT + " It rained halfway up.",
                           v=0.6, a=0.4, mode="supplement")
        new = _disk(tmp_path, LONE)["superseded_by"]
        assert f"{LONE} → {new}" in out
        assert _disk(tmp_path, new)["supersedes"] == LONE
    run(go())


def test_regrow_resolves_short_ids_in_from_and_stores_full_ones(store, tmp_path):
    async def go():
        await _lone(store)
        a, _b = await _twins(store)
        out = await regrow(bucket_id=a[:7], text=FIRST_VIEW + " Especially in winter.",
                           v=0.5, a=0.4, mode="supplement", from_=[LONE[:6]])
        new = _disk(tmp_path, a)["superseded_by"]
        assert new in out
        # Criterion: what the from chain holds on disk is the full id, not the handle.
        assert _disk(tmp_path, new)["from"] == LONE
    run(go())


def test_regrow_keeps_its_own_message_for_a_full_id_that_does_not_exist(store):
    async def go():
        await _lone(store)
        out = await regrow(bucket_id=GHOST, text="x", v=0.5, a=0.5, mode="supplement")
        assert out == f"找不到 {GHOST}。"
    run(go())


# ───────────────────────── fold ─────────────────────────

def test_fold_folds_the_handles_breath_prints(store, tmp_path):
    async def go():
        a, b = await _twins(store)
        out = await fold(text="Going out is how they recover.", room="MIND/VIEWS",
                         v=0.6, a=0.4, cover=[a[:7], b[:7]])
        assert out.startswith("▣gist→")
        new = out.split("▣gist→", 1)[1].split()[0]
        # Criterion: both directions of the link are written with full ids.
        assert sorted(_disk(tmp_path, new)["cover"]) == [a, b]
        assert new in _disk(tmp_path, a)["covered_by"]
        assert new in _disk(tmp_path, b)["covered_by"]
    run(go())


# ───────────────────────── grow ─────────────────────────

def test_grow_mind_takes_a_short_id_in_from(store, tmp_path):
    async def go():
        await _lone(store)
        out = await grow(kind="mind", room="MIND/VIEWS", text=FIRST_VIEW,
                         from_=[LONE[:6]], v=0.5, a=0.4)
        assert out.startswith("🧠mind→")
        new = out.split("🧠mind→", 1)[1].split()[0]
        assert _disk(tmp_path, new)["from"] == LONE
    run(go())


def test_grow_event_takes_a_short_id_in_from(store, tmp_path):
    async def go():
        await _lone(store)
        before = set(_files(tmp_path))
        out = await grow(kind="event", from_=LONE[:6],
                         items=[{"room": "EVENT/SELF", "text": "Sunday we rested.",
                                 "v": 0.6, "a": 0.2, "when": "2026-09-27"}])
        assert "不存在" not in out and "查无此桶" not in out
        [added] = set(_files(tmp_path)) - before
        assert dict(frontmatter.load(added).metadata)["from"] == LONE
    run(go())


# ───────────────────────── the refusals ─────────────────────────

def test_an_ambiguous_prefix_is_refused_with_the_candidates_and_writes_nothing(store, tmp_path):
    async def go():
        a, b = await _twins(store)
        await _lone(store)
        before = _files(tmp_path)
        a_before, b_before = _disk(tmp_path, a), _disk(tmp_path, b)

        out = await trace(bucket_id="abcdef", status="resolved")
        assert "撞了 2 个" in out and a in out and b in out

        out = await regrow(bucket_id="abcdef", text="x", v=0.5, a=0.5, mode="overturn")
        assert "撞了 2 个" in out

        out = await fold(text="x", room="MIND/VIEWS", v=0.5, a=0.5, cover=["abcdef", LONE[:6]])
        assert "folds 里 abcdef" in out and "撞了 2 个" in out

        out = await grow(kind="mind", room="MIND/VIEWS", text="x", from_=["abcdef"], v=0.5, a=0.5)
        assert "from 里 abcdef" in out and "撞了 2 个" in out

        out = await trace(bucket_id=LONE[:6], folds_append=["abcdef"])
        assert "folds_append 里 abcdef" in out and "撞了 2 个" in out

        # Criterion: a refusal is not a write — no new file, and neither twin moved.
        assert _files(tmp_path) == before
        assert _disk(tmp_path, a) == a_before
        assert _disk(tmp_path, b) == b_before
        # One more character settles it.
        assert a in await trace(bucket_id="abcdef1", name="Settled")
        assert _disk(tmp_path, a)["name"] == "Settled"
    run(go())


def test_an_unknown_prefix_is_refused_plainly(store, tmp_path):
    async def go():
        await _lone(store)
        before = _files(tmp_path)
        assert "查无此桶：ffffff" in await trace(bucket_id="ffffff", status="resolved")
        assert "查无此桶：ffffff" in await regrow(bucket_id="ffffff", text="x", v=0.5, a=0.5,
                                               mode="supplement")
        assert "查无此桶：ffffff" in await grow(kind="mind", room="MIND/VIEWS", text="x",
                                             from_=["ffffff"], v=0.5, a=0.5)
        assert _files(tmp_path) == before
        assert "status" not in _disk(tmp_path, LONE)
    run(go())


def test_recall_by_id_follows_the_same_rule(store):
    async def go():
        a, b = await _twins(store)
        await _lone(store)
        out = await R.recall_core(when="", room="", tag="", query="abcdef")
        assert "撞了 2 个" in out and a in out and b in out
        assert "查无此桶：ffffff" in await R.recall_core(when="", room="", tag="", query="ffffff")
        assert LONE in await R.recall_core(when="", room="", tag="", query=LONE[:6])
    run(go())
