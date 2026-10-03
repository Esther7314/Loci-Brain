# -*- coding: utf-8 -*-
"""
tests/test_import_withdraw_bites.py — what withdrawing an import, or one of its lines,
leaves findable, checked while the batch's folder is still on disk.

  · a line withdrawn on its own is not found by a word search of the imports, and not
    counted either; the other lines holding the word still are
  · a withdrawal whose clearing breaks off in one place says `incomplete` and leaves the
    batch's text and drafts where they are (they go only once every place is done); the
    same call again carries on and finishes
"""

import asyncio

import pytest

from core import _source_change as SC
from core import _sources as S

from test_import_two_steps import EXPORT, Pipe, by_topic, engine, original, store  # noqa: F401


def run(coro):
    return asyncio.run(coro)


def test_a_line_withdrawn_on_its_own_is_neither_found_nor_counted(store, monkeypatch):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    before = original("周六")
    assert f"c0001#l0003" in before and "有「周六」的 2 行" in before, before
    run(store.sources.apply_change({"change_id": "w-l3", "kind": "withdrawn", "host_seq": 1,
                                    "source": f"import:{batch}/c0001#l0003"}))
    assert "导入的原话里没有" in original("晴天")
    after = original("周六")
    assert f"c0001#l0001" in after and f"c0001#l0003" not in after, after
    assert "有「周六」的 1 行" in after
    assert "晴天" not in after


def test_an_incomplete_withdrawal_keeps_the_text_and_the_drafts_until_it_finishes(
        store, tmp_path, monkeypatch):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    run(eng.draft(batch))
    assert store.slices.pending_count() == 2
    folder = tmp_path / "_sources" / "imports" / batch
    real = SC._RUN["slices"]
    calls = {"n": 0}

    async def breaks_once(store_, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk hiccup")
        return await real(store_, ctx)
    monkeypatch.setitem(SC._RUN, "slices", breaks_once)

    status, out = run(eng.withdraw(batch))
    assert status == 200 and out["status"] == "incomplete" and not out["ok"], out
    assert out["text_deleted"] is False and out["drafts_deleted"] == 0
    assert folder.is_dir(), "the text stays until every place is done"
    # The second conversation's change ran its own places whole (its draft went with it);
    # the first one's broke off, and its draft is still there — nothing purged the batch.
    left = [b["source"]["container"] for b in store.slices.open_batches() if b["slices"]]
    assert left == ["c0001"], left
    assert eng.get_status(batch)["status"] == "withdrawing"
    assert store.sources.state_of(f"import:{batch}/c0001#l0002") == S.WITHDRAWN

    status, out = run(eng.withdraw(batch))
    assert status == 200 and out["status"] == "withdrawn" and out["ok"], out
    assert out["text_deleted"] and not folder.exists()
    assert store.slices.pending_count() == 0
