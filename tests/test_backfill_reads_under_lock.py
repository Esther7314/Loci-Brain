# -*- coding: utf-8 -*-
"""
tests/test_backfill_reads_under_lock.py — the backfill decides what is blank at the
moment it writes, not at the moment it asked.

The side-model call takes seconds. Whatever a trace or the panel writes in that window is
the caller's, and the backfill must neither overwrite it nor list it in `backfilled`. An
entry the backfill cannot read is skipped for this round, never treated as a blank one.

Real store on a temp dir; the side model is a stub that edits the entry while it
"thinks". No real model is called.
"""

import asyncio
import json

import frontmatter
import pytest

from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools import _subjects as S
from tools.grow import rooms_path as R

BODY = "Promised Wang we would see Dune next Monday at two."


class _Log:
    def __init__(self):
        self.lines: list[str] = []

    def _rec(self, msg, *a, **k):
        self.lines.append(str(msg))
    warning = info = debug = error = _rec


class EditingModel:
    """Answers with every slot filled, after `during` has written to the entry."""

    def __init__(self, answer: dict, during=None):
        self.answer = answer
        self.during = during
        self.calls = 0

    async def _chat(self, system, user, max_tokens=0, temperature=0.0):
        self.calls += 1
        if self.during is not None:
            await self.during()
        return json.dumps(self.answer, ensure_ascii=False)


ANSWER = {"name": "side name", "summary": "side summary", "tags": ["Dune"],
          "domain": ["film"], "subjects": [{"name": "Wang", "kind": ""}],
          "bound": ["Wang"], "time": {"phrase": "", "absolute": "2026-12-01"},
          "cue_phrasings": ["side phrasing"]}


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    table = tmp_path / "aliases.yaml"
    table.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(table))
    monkeypatch.setattr(S, "_cache", None)
    return mgr


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def test_an_edit_made_while_the_model_thinks_is_kept(store, tmp_path, monkeypatch):
    async def go():
        bid = await store.create(BODY, room="EVENT/SELF", direction_of_fit="telic",
                                 cue={"condition": "after the exam", "phrasings": []})

        async def panel_edit():
            assert await store.update(bid, subjects=["Li"], bound=["Li"],
                                      when="2026-11-11", domain=["work"],
                                      cue={"condition": "after the exam",
                                           "phrasings": ["exam is over"]})

        monkeypatch.setattr(rt, "dehydrator", EditingModel(ANSWER, panel_edit))
        await R._backfill_one(bid, BODY, "event")
        return bid

    bid = asyncio.run(go())
    meta = _disk(tmp_path, bid)
    # Criterion: what the edit wrote stands, and the backfill does not claim it.
    assert meta["bound"] == ["Li"]
    assert meta["when"] == "2026-11-11"
    assert meta["domain"] == ["work"]
    assert meta["cue"]["phrasings"] == ["exam is over"]
    assert meta["subjects"][0] == "Li"
    backfilled = set(meta.get("backfilled") or [])
    assert not backfilled & {"bound", "when", "domain", "cue.phrasings"}
    # What was still blank is filled as usual.
    assert meta["summary"] == "side summary"


def test_an_unreadable_entry_is_skipped_not_overwritten(store, tmp_path, monkeypatch):
    async def go():
        bid = await store.create(BODY, room="EVENT/SELF", summary="kept summary",
                                 subjects=["Li"])
        real_get = store.get

        async def broken_get(bucket_id):
            raise OSError("sharing violation")
        monkeypatch.setattr(store, "get", broken_get)
        monkeypatch.setattr(rt, "dehydrator", EditingModel({**ANSWER, "looks_like_promise": True}))
        await R._backfill_one(bid, BODY, "event")
        monkeypatch.setattr(store, "get", real_get)
        return bid

    bid = asyncio.run(go())
    meta = _disk(tmp_path, bid)
    # Criterion: nothing is written on an entry the backfill could not see.
    assert meta["summary"] == "kept summary"
    assert "backfilled" not in meta
    assert "looks_like_promise" not in meta
