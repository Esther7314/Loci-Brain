# -*- coding: utf-8 -*-
"""
tests/test_embedding_switch.py — changing the embedding model recomputes every vector,
and the old and new vectors are never compared with each other.

The engines are the real EmbeddingEngine (its storage, its meta table) with a fake backend
in place of the model: no call leaves the machine. The panel's one doorway for engine
settings, POST /api/config, is driven through its registered handler.

  · asking for another model while the library holds vectors is refused until confirmed,
    and the refusal says how many vectors are invalid, an estimate, and the similarity
    lines tuned on the old model, read from the code that uses them;
  · confirmed, the old model stays the live one until every vector is redone; then the
    new vectors replace the old in one swap, the new model goes live and is persisted;
  · a failed recompute leaves the live library as it was, says so in the status route, and
    resumes from where it stopped; an abandoned one leaves nothing behind;
  · the same model, or a library with no vectors, needs no confirmation.
"""

import asyncio
import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
from starlette.requests import Request

from core import embedding_switch as ES
from core import migration_engine as ME
from core.bucket_manager import BucketManager
from core.embedding_engine import EmbeddingEngine

DIMS = {"old-embed": 3, "new-embed": 4}


class _Backend:
    """A model that answers from a hash: a vector size per model, a call count, and a set
    of texts it fails on."""

    def __init__(self, model: str, failing: set):
        self._model = model
        self._dim = DIMS.get(model, 5)
        self.failing = failing
        self.calls: list[str] = []
        self.api_format = "ollama"
        self.base_url = ""

    def model_name(self):
        return self._model

    def vector_dim(self):
        return self._dim

    def generate(self, text):
        raise AssertionError("sync generate is not used")

    async def generate_async(self, text):
        self.calls.append(text)
        if text in self.failing:
            raise RuntimeError("the fake model refuses this one")
        h = hashlib.sha256(f"{self._model}:{text}".encode()).digest()
        return [b / 255 for b in h[:self._dim]]


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA
    from web import loci as Wb
    from web import panel_auth as PA

    ME.reset_for_test()
    monkeypatch.setattr(ME, "BATCH_SIZE", 2)
    monkeypatch.setattr(ME, "BATCH_INTERVAL_SEC", 0.0)
    failing: set = set()
    backends: dict = {}

    def factory(config):
        engine = EmbeddingEngine(config)
        model = str((config.get("embedding") or {}).get("model") or "")
        backend = backends.setdefault(model, _Backend(model, failing))
        engine._backend = backend
        engine.model = model
        engine.enabled = True
        return engine
    monkeypatch.setattr(ES, "ENGINE_FACTORY", factory)
    monkeypatch.setattr(CA, "_rebuild_embedding_runtime",
                        lambda: sh.replace_embedding_engine(factory(sh.config)))

    config_path = tmp_path / "config.yaml"
    config_path.write_text("embedding:\n  model: old-embed\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    config = {"buckets_dir": str(tmp_path / "lib"), "transport": "stdio",
              "embedding": {"enabled": True, "api_format": "ollama", "model": "old-embed",
                            "base_url": "http://127.0.0.1:9/v1"}}
    (tmp_path / "lib").mkdir()
    store = BucketManager({"buckets_dir": config["buckets_dir"]})
    live = factory(config)
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "embedding_engine", live)
    monkeypatch.setattr(sh, "embedding_outbox", None)
    monkeypatch.setattr(sh, "import_engine", None)
    monkeypatch.setattr(sh, "migrate_engine", None)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    CA.register(_Mcp())
    Wb.register(_Mcp())

    async def call(method, path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "query_string": b"",
                       "headers": [(b"host", b"127.0.0.1:8000"),
                                   (b"origin", b"http://127.0.0.1:8000"),
                                   (b"content-type", b"application/json")]}, receive)
        resp = await routes[(method, path)](req)
        return resp.status_code, json.loads(resp.body)

    texts = ["第一条：海边。", "第二条：读书会。", "第三条：烤蛋糕。"]

    async def seed():
        ids = []
        for t in texts:
            bid = await store.create(t, room="EVENT/SELF")
            assert await live.generate_and_store(bid, t)
            ids.append(bid)
        await store.update(ids[0], meaning_append="那天很开心")
        await live.generate_and_store_meaning(ids[0], "那天很开心")
        return ids
    ids = asyncio.run(seed())
    yield {"sh": sh, "call": call, "store": store, "live": live, "ids": ids,
           "texts": texts, "failing": failing, "backends": backends,
           "db": Path(config["buckets_dir"]) / "embeddings.db",
           "config_path": config_path, "lib": Path(config["buckets_dir"])}
    ME.reset_for_test()


def _dims(db: Path) -> dict:
    # closing(): a connection left open holds the file, and on Windows the swap's rename
    # then fails.
    with closing(sqlite3.connect(db)) as c:
        return {bid: len(json.loads(e)) for bid, e in c.execute(
            "SELECT bucket_id, embedding FROM embeddings WHERE TRIM(embedding) <> ''")}


def _meaning_dims(db: Path) -> dict:
    with closing(sqlite3.connect(db)) as c:
        return {bid: len(json.loads(e)) for bid, e in c.execute(
            "SELECT bucket_id, meaning_embedding FROM embeddings "
            "WHERE meaning_embedding IS NOT NULL")}


async def _finish():
    task = ME._migration_task
    if task is not None:
        await task


def test_switching_asks_first_and_says_what_it_costs(world):
    async def go():
        status, out = await world["call"]("POST", "/api/config",
                                          {"embedding": {"model": "new-embed"}})
        assert status == 409 and out["needs_confirmation"], out
        p = out["reembed"]
        assert (p["vectors"], p["from_model"], p["to_model"], p["to_dim"]) == (
            3, "old-embed", "new-embed", 4)
        assert p["estimated_seconds"] is not None and p["probe_error"] == ""
        values = {t["where"]: t["value"] for t in p["thresholds"]}
        assert values["core/_reconsolidation.SIMILARITY_LINE"] == 0.80
        assert values["core/bucket_manager._VECTOR_RECALL_THRESHOLD"] == 0.65
        assert values["tools/recall/core.RELEVANCE_FLOOR"] == 35.0
        assert "3 条向量全部作废" in p["say"] and "0.8" in p["say"] and "35" in p["say"]
        # Nothing moved.
        assert world["sh"].config["embedding"]["model"] == "old-embed"
        assert world["sh"].embedding_engine is world["live"]
        assert set(_dims(world["db"]).values()) == {3}
        assert not ME.staging_db_path_for(str(world["db"])) or not Path(
            ME.staging_db_path_for(str(world["db"]))).exists()
    asyncio.run(go())


def test_confirmed_the_old_model_serves_until_every_vector_is_redone(world):
    sh = world["sh"]

    async def go():
        status, out = await world["call"]("POST", "/api/config", {
            "persist": True, "embedding": {"model": "new-embed", "reembed": "confirm"}})
        assert status == 200 and out["reembed"]["phase"] in ("running", "idle"), out
        # Not yet done: the live model, the live vectors, the live config are the old ones.
        assert sh.embedding_engine is world["live"]
        assert sh.config["embedding"]["model"] == "old-embed"
        assert set(_dims(world["db"]).values()) == {3}
        st = await world["call"]("GET", "/api/loci/embedding/migration")
        assert st[1]["running"] and st[1]["target"]["model"] == "new-embed"
        await _finish()
        st = await world["call"]("GET", "/api/loci/embedding/migration")
        assert st[1]["phase"] == "completed", st
        # Swapped whole: every vector and meaning vector is the new model's.
        assert _dims(world["db"]) == {bid: 4 for bid in world["ids"]}
        assert _meaning_dims(world["db"]) == {world["ids"][0]: 4}
        assert sh.embedding_engine.model == "new-embed"
        assert sh.config["embedding"]["model"] == "new-embed"
        assert "new-embed" in world["config_path"].read_text(encoding="utf-8")
        assert ES.vectors_in(str(world["db"]))["model"] == "new-embed"
        assert not Path(ME.staging_db_path_for(str(world["db"]))).exists()
        # Now it is the library's model: the same model again needs no confirmation.
        status, out = await world["call"]("POST", "/api/config",
                                          {"embedding": {"model": "new-embed"}})
        assert status == 200, out
    asyncio.run(go())


def test_a_failed_recompute_changes_nothing_and_resumes_where_it_stopped(world):
    sh = world["sh"]
    world["failing"].add(world["texts"][2])

    async def go():
        status, out = await world["call"]("POST", "/api/config", {
            "embedding": {"model": "new-embed", "reembed": "confirm"}})
        assert status == 200, out
        await _finish()
        status, st = await world["call"]("GET", "/api/loci/embedding/migration")
        assert st["phase"] == "failed" and st["resumable"], st
        assert st["failed_count"] == 1 and st["failed_items"][0]["bucket_id"] == world["ids"][2]
        assert sh.embedding_engine is world["live"]
        assert set(_dims(world["db"]).values()) == {3}, "the live vectors are untouched"
        health = await world["call"]("GET", "/api/loci/health")
        rows = {c["label"]: c for c in health[1]["checks"]}
        assert rows["换向量模型"]["status"] == "error"

        world["failing"].clear()
        new = world["backends"]["new-embed"]
        before = list(new.calls)
        status, out = await world["call"]("POST", "/api/loci/embedding/migration",
                                          {"action": "resume"})
        assert status == 200, out
        await _finish()
        status, st = await world["call"]("GET", "/api/loci/embedding/migration")
        assert st["phase"] == "completed", st
        redone = [t for t in new.calls[len(before):] if t in world["texts"]]
        assert redone == [world["texts"][2]], "only what had failed is computed again"
        assert _dims(world["db"]) == {bid: 4 for bid in world["ids"]}
        assert sh.embedding_engine.model == "new-embed"
    asyncio.run(go())


def test_abandoning_leaves_the_old_model_and_no_staging(world):
    world["failing"].add(world["texts"][1])

    async def go():
        await world["call"]("POST", "/api/config", {
            "embedding": {"model": "new-embed", "reembed": "confirm"}})
        await _finish()
        status, out = await world["call"]("POST", "/api/loci/embedding/migration",
                                          {"action": "abandon"})
        assert status == 200 and out["phase"] == "idle" and not out["resumable"], out
        assert not Path(ME.staging_db_path_for(str(world["db"]))).exists()
        assert not Path(ME.checkpoint_path_for(str(world["lib"]))).exists()
        assert world["sh"].embedding_engine is world["live"]
        assert set(_dims(world["db"]).values()) == {3}
    asyncio.run(go())


def test_no_vectors_or_the_same_model_needs_no_confirmation(world, tmp_path):
    async def go():
        status, out = await world["call"]("POST", "/api/config",
                                          {"embedding": {"model": "old-embed"}})
        assert status == 200, out
    asyncio.run(go())
    empty = tmp_path / "empty.db"
    assert not ES.needs_reembed(str(empty), "new-embed", "old-embed")
    assert ES.needs_reembed(str(world["db"]), "new-embed", "old-embed")
    assert not ES.needs_reembed(str(world["db"]), "models/old-embed", "")
