# -*- coding: utf-8 -*-
"""
tests/test_results_after_withdraw.py — a side model's result about material withdrawn
while it was thinking is never written back after the clearing.

Backfill, slicing and weaving each read material, wait for a model, then write what came
back. Each race is played in one event loop, in order: the model stand-in starts and waits
on an asyncio.Event; the host withdraws the source and the clearing runs to the end; the
Event is released. The phrase the material carried must then be nowhere under the library
(read back byte by byte), and the log, the exception or the dream state says why the result
was dropped. Each has a control where nothing changes during the wait and the result is
written as before. Backfill is raced against a body revise too: the answer is about the
old body, so nothing of it lands on the new one, and the backfill says it was dropped.
Slicing is raced against a new revision announced for a registered run holding the
batch's lines: the gists are of the old version, so the whole batch is dropped.

Real store on a temp dir; every model and the host are stand-ins.
"""

import asyncio
import json
import os

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _dream as D
from core import _slicer as SL
from core import _source_change as SC
from core import names as N
from core import runtime as rt
from core.bucket_manager import BucketManager
from core.scope import Host
from tools.grow import rooms_path as R

PHRASE = "暗号是青柠汽水"
WHERE = {"system": "lento", "instance": "home", "container": "private:U"}
M = {**WHERE, "id": "m_0003"}
M_STR = "lento:home/private:U#m_0003"
OPEN_HOST = Host("life", scope_mode="open", may_restore=True)
WITHDRAW = {"change_id": "c-1", "source": M_STR, "host_seq": 1, "change": "withdrawn"}


def run(coro):
    return asyncio.run(coro)


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def _n(self, msg, *a, **k):
        self.lines.append(str(msg) % a if a else str(msg))
    warning = info = debug = error = _n


class _Engine:
    async def ensure_started(self):
        pass


class Gate:
    """What a model stand-in waits on: `entered` once it is called, then `release`."""

    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def hold(self):
        self.entered.set()
        await self.release.wait()


async def race(work, gate: Gate, meanwhile=None):
    """Start `work`, wait until its model is called, run `meanwhile` (the withdrawal and
    its clearing) to the end, release the model, and return `work`'s result."""
    task = asyncio.ensure_future(work)
    await gate.entered.wait()
    if meanwhile is not None:
        await meanwhile()
    gate.release.set()
    return await task


async def withdraw(store):
    status, out = await SC.handle(store, dict(WITHDRAW), OPEN_HOST)
    assert status == 200 and out["status"] == "applied", out
    return out


def _files_holding(root, needle: str) -> list[str]:
    raw = needle.encode("utf-8")
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                if raw in fh.read():
                    hits.append(os.path.relpath(p, root))
    return hits


def _meta(store, bid) -> dict:
    return dict(frontmatter.load(store._find_bucket_file(bid)).metadata)


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    log = _Log()
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", log)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    table = tmp_path / "aliases.yaml"
    table.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(table))
    monkeypatch.setattr(N, "_cache", None)
    mgr.test_log = log
    return mgr


# ───────────────────────── backfill ─────────────────────────

BODY = f"小周说{PHRASE}，周六要去海边。"
ANSWER = {"name": f"{PHRASE}的约定", "summary": f"{PHRASE}与海边", "tags": [PHRASE],
          "domain": ["生活"], "subjects": [{"name": "小周", "kind": "人"}]}


class BackfillModel:
    def __init__(self, gate: Gate):
        self.gate = gate

    async def _chat(self, system, user, max_tokens=0, temperature=0.0):
        await self.gate.hold()
        return json.dumps(ANSWER, ensure_ascii=False)


def _entry(store):
    return run(store.create(BODY, room="EVENT/WORLD", sources=[M], valence=0.6,
                            arousal=0.3))


def test_a_backfill_answer_about_a_body_cleared_meanwhile_is_dropped(store, tmp_path,
                                                                      monkeypatch):
    e = _entry(store)
    gate = Gate()
    monkeypatch.setattr(rt, "dehydrator", BackfillModel(gate))
    run(race(R._backfill_one(e, BODY, "event"), gate, lambda: withdraw(store)))
    # Criterion: the entry is as the clearing left it — nothing of the answer came back.
    meta = _meta(store, e)
    assert not {"name", "summary", "tags", "subjects", "backfilled"} & set(meta), meta
    assert any(r.get("cleared") for r in meta["invalidation"])
    assert _files_holding(tmp_path, PHRASE) == []
    assert "小周" not in (tmp_path / "aliases.yaml").read_text(encoding="utf-8")
    assert any("作废" in ln and e in ln for ln in store.test_log.lines), store.test_log.lines


def test_a_cleared_entry_is_neither_asked_about_nor_swept(store, monkeypatch):
    e = _entry(store)
    run(withdraw(store))

    class Never:
        async def _chat(self, *a, **k):
            raise AssertionError("a cleared body is not sent to the side model")
    monkeypatch.setattr(rt, "dehydrator", Never())
    run(R._backfill_one(e, BODY, "event"))
    swept = []

    async def keep(pairs):
        swept.extend(pairs)
    monkeypatch.setattr(R, "_backfill_batch", keep)
    assert run(R.backfill_sweep()) == 0 and swept == []


def test_a_backfill_with_nothing_changed_meanwhile_is_written(store, monkeypatch):
    e = _entry(store)
    gate = Gate()
    monkeypatch.setattr(rt, "dehydrator", BackfillModel(gate))
    outcome = run(race(R._backfill_one(e, BODY, "event"), gate))
    assert outcome == "backfilled"
    meta = _meta(store, e)
    assert meta["name"] == ANSWER["name"] and meta["summary"] == ANSWER["summary"]
    assert PHRASE in meta["tags"] and "小周" in meta["subjects"]


NEW_BODY = "小周说周六改去山上看日出。"


def test_a_backfill_answer_about_a_body_revised_meanwhile_is_dropped(store, tmp_path,
                                                                      monkeypatch):
    e = _entry(store)
    gate = Gate()
    monkeypatch.setattr(rt, "dehydrator", BackfillModel(gate))

    async def revise_body():
        assert await store.update(e, content=NEW_BODY)
    outcome = run(race(R._backfill_one(e, BODY, "event"), gate, revise_body))
    # Criterion: the new body stands with its blanks; nothing about the old one is on it.
    meta = _meta(store, e)
    assert not {"summary", "subjects", "backfilled"} & set(meta), meta
    assert not meta.get("tags") and PHRASE not in str(meta.get("name") or "")
    assert run(store.get(e))["content"] == NEW_BODY
    assert "小周" not in (tmp_path / "aliases.yaml").read_text(encoding="utf-8")
    # And the caller is told so, not handed a quiet success.
    assert outcome == "body_changed"
    assert any("作废" in ln and e in ln for ln in store.test_log.lines), store.test_log.lines


# ───────────────────────── slicing ─────────────────────────

def _batch():
    return {"source": dict(WHERE), "day": "2026-10-01",
            "lines": [{"id": f"m_000{i}", "text": f"line {i}"} for i in range(1, 7)]}


def _slicer(gate: Gate):
    async def model(system, user):
        await gate.hold()
        return json.dumps({"slices": [{"from": 1, "to": 4, "gist": f"聊到{PHRASE}"},
                                      {"from": 5, "to": 6, "gist": "说晚安"}]},
                          ensure_ascii=False)
    return model


def test_slices_of_a_line_withdrawn_while_slicing_are_dropped(store, tmp_path):
    _entry(store)
    gate = Gate()

    async def go():
        with pytest.raises(SL.BatchStale) as refused:
            await race(SL.take_batch(store, _batch(), model=_slicer(gate)), gate,
                       lambda: withdraw(store))
        return refused.value
    refused = run(go())
    # Criterion: the refusal names the line and its state, and nothing was stored.
    assert refused.lines == {"m_0003": "withdrawn"}
    assert "nothing was stored" in str(refused)
    assert store.slices.pending_count() == 0 and store.slices.batches() == []
    assert not store.sources.orders_path.exists()
    assert _files_holding(tmp_path, PHRASE) == []


def test_slices_of_a_run_revised_while_slicing_are_dropped(store, tmp_path):
    # The run's lines are registered; the host announces a new revision for the run alone.
    assert store.sources.record_order(dict(WHERE), ["m_0002", "m_0003", "m_0004"]) == "recorded"
    run_ = "lento:home/private:U#m_0002..m_0004"

    async def revise():
        status, out = await SC.handle(store, {"change_id": "c-run", "source": run_,
                                              "host_seq": 1, "change": "revised",
                                              "revision": "r2"}, OPEN_HOST)
        # No memory names the run yet: recorded all the same, as unknown_source.
        assert status == 200 and out["status"] == "unknown_source", out

    gate = Gate()

    async def go():
        with pytest.raises(SL.BatchStale) as refused:
            await race(SL.take_batch(store, {**_batch(), "revision": "r1"},
                                     model=_slicer(gate)), gate, revise)
        return refused.value
    refused = run(go())
    assert refused.lines == {i: "revised" for i in ("m_0002", "m_0003", "m_0004")}
    assert "nothing was stored" in str(refused)
    assert store.slices.pending_count() == 0 and store.slices.batches() == []
    assert _files_holding(tmp_path, PHRASE) == []


def test_slices_with_nothing_changed_meanwhile_are_written(store):
    gate = Gate()
    out = run(race(SL.take_batch(store, _batch(), model=_slicer(gate)), gate))
    assert [s["gist"] for s in out["slices"]] == [f"聊到{PHRASE}", "说晚安"]
    assert store.slices.pending_count() == 2


# ───────────────────────── weaving ─────────────────────────

DREAM_TEXT = f"梦里{PHRASE}在发光，海在屋子里面。"


def _want(store):
    return run(store.create(f"答应小周：{PHRASE}，一直没去成。", tags=["t"],
                            room="EVENT/SELF", direction_of_fit="telic", weight=0.9,
                            valence=0.4, arousal=0.6, sources=[M]))


def _weaver(gate: Gate):
    async def call_model(ingredients, c):
        await gate.hold()
        return D.parse_dream(json.dumps({"完整": DREAM_TEXT, "碎片": "海。", "v": 0.4,
                                         "a": 0.6}, ensure_ascii=False))
    return call_model


def test_a_dream_woven_from_an_ingredient_withdrawn_meanwhile_is_dropped(store, tmp_path,
                                                                          monkeypatch):
    want = _want(store)
    gate = Gate()
    monkeypatch.setattr(D, "call_model", _weaver(gate))
    out = run(race(D.weave(force=True), gate, lambda: withdraw(store)))
    # Criterion: no dream is on disk, nothing was noted, and the state says why.
    assert out is None
    assert D.load_dreams() == []
    assert "last_dreamt" not in _meta(store, want)
    assert _files_holding(tmp_path, PHRASE) == []
    dropped = D.load_state()["最近一次作废"]
    assert dropped["料"] == [want] and dropped["于"]
    assert "今天几个" not in D.load_state()
    assert any("作废" in ln and want in ln for ln in store.test_log.lines)


def test_a_dream_with_nothing_changed_meanwhile_is_saved(store, monkeypatch):
    want = _want(store)
    gate = Gate()
    monkeypatch.setattr(D, "call_model", _weaver(gate))
    out = run(race(D.weave(force=True), gate))
    assert out["完整"] == DREAM_TEXT and out["记下了"] == [want]
    assert [r["id"] for r in D.load_dreams()] == [out["id"]]
    assert "最近一次作废" not in D.load_state()
