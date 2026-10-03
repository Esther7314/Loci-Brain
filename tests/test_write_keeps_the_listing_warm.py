# -*- coding: utf-8 -*-
"""
tests/test_write_keeps_the_listing_warm.py — what one write costs the next read.

A managed write changes one entry: it puts that entry's new form into the parsed listing
in place instead of throwing the whole listing away (a full re-parse is most of a second
locally and seconds on a bind mount, paid by whatever reads next — breath, the cue poke).
One body is embedded once even when the write's look-back and the vector queue ask for it
at the same moment. A look-back that runs out of time says so at WARNING.

No real embedding service: the backend is a stub that counts its calls.
"""

import asyncio
import logging

import pytest

from core import _reconsolidation as R
from core.bucket_manager import BucketManager
from core.embedding_engine import EmbeddingEngine


@pytest.fixture
def store(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path)})


def _parses(monkeypatch, store) -> list:
    seen: list = []
    real = store._load_bucket

    def counting(path, *a, **k):
        seen.append(path)
        return real(path, *a, **k)
    monkeypatch.setattr(store, "_load_bucket", counting)
    return seen


def test_create_and_update_keep_the_listing_and_show_the_change(store, monkeypatch):
    async def go():
        ids = [await store.create(f"entry {i}", room="EVENT/SELF") for i in range(5)]
        await store.list_all()
        parses = _parses(monkeypatch, store)
        new = await store.create("a new one", room="EVENT/SELF")
        assert await store.update(ids[0], summary="changed")
        listed = {b["id"]: b for b in await store.list_all()}
        return ids, new, listed, len(parses)
    ids, new, listed, parsed = asyncio.run(go())
    # Criterion: the listing after two writes is current…
    assert new in listed and listed[ids[0]]["metadata"]["summary"] == "changed"
    assert len(listed) == 6
    # …and was not rebuilt by parsing every file again (one parse per written entry).
    assert parsed <= 2, parsed


class _Backend:
    def __init__(self, delay=0.05):
        self.calls = 0
        self.delay = delay

    def model_name(self):
        return "stub"

    async def generate_async(self, text):
        self.calls += 1
        await asyncio.sleep(self.delay)
        return [0.1, 0.2, 0.3, float(len(text) % 7)]


def _engine(tmp_path, backend) -> EmbeddingEngine:
    eng = EmbeddingEngine({"buckets_dir": str(tmp_path), "embedding": {"enabled": False}})
    eng._backend = backend
    eng.enabled = True
    eng.model = "stub"
    return eng


def test_one_body_is_embedded_once_when_two_ask_at_once(tmp_path):
    backend = _Backend()
    eng = _engine(tmp_path, backend)

    async def go():
        eng._store_embedding("older", [0.1, 0.2, 0.3, 0.4])
        body = "We walked to the lake and talked about moving."
        await asyncio.gather(eng.generate_and_store("new1", body),
                             eng.search_similar_strict(body, top_k=3))
        return await eng.get_embedding("new1")
    stored = asyncio.run(go())
    assert stored and backend.calls == 1


def test_a_look_back_that_runs_out_of_time_says_so_loudly(tmp_path, monkeypatch, caplog):
    class Slow:
        enabled = True
        calls = 0

        async def search_similar_strict(self, text, top_k=8, among=None):
            Slow.calls += 1
            await asyncio.sleep(1.0)
            return []

    class Store:
        embedding_engine = Slow()

    views = R._Views([{"id": "v1", "content": "a view",
                       "metadata": {"id": "v1", "room": "MIND/VIEWS"}}], None,
                     R._w.now(), set())
    monkeypatch.setattr(R, "MEANING_BUDGET_SECONDS", 0.05)
    with caplog.at_level(logging.INFO, logger="loci_brain.reconsolidation"):
        assert asyncio.run(R._meaning_hits(Store(), views, "text")) == []
    assert any(r.levelno >= logging.WARNING for r in caplog.records), caplog.text
    # A hung backend is left alone for a while: the next write does not wait again.
    assert asyncio.run(R._meaning_hits(Store(), views, "text")) == []
    assert Slow.calls == 1
