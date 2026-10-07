# -*- coding: utf-8 -*-
"""
tests/test_config_api_embed_key.py — the embedding key saved from the panel survives a restart.

The same rules as the side model's key (tests/test_config_api_side_key.py): POST
/api/config with `persist: true` writes `embedding.api_key` to config.yaml, where keys
live, and a restart reads it back through utils.load_config; an empty key keeps the saved
one; GET shows it masked only; a backup and an export package leave config.yaml out; and
LOCI_EMBED_API_KEY still wins at startup. A key typed together with a new model goes live
and into config.yaml with that model, when the recompute publishes it — and a key the
running settings got from the environment is never written into the file.
"""

import asyncio
import json
import zipfile

import pytest
import yaml

from _config_kit import config_world

FAKE_KEY = "test-embed-key-0123456789abcdef"
ENV_KEY = "test-env-embed-key-9876543210"


class _Engine:
    def __init__(self, model, db_path):
        self.model = model
        self.db_path = db_path
        self.enabled = True


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA

    monkeypatch.delenv("LOCI_EMBED_API_KEY", raising=False)
    w = config_world(tmp_path, monkeypatch,
                     "embedding:\n  model: emb-model\n  enabled: true\n",
                     {"embedding": {"model": "emb-model", "enabled": True,
                                    "api_format": "openai_compat"}})
    db = str(tmp_path / "embeddings.db")
    monkeypatch.setattr(sh, "embedding_engine", _Engine("emb-model", db))
    built = []

    def rebuild():
        built.append(dict(sh.config["embedding"]))
        sh.embedding_engine = _Engine(sh.config["embedding"].get("model", ""), db)
        return sh.embedding_engine
    monkeypatch.setattr(CA, "_rebuild_embedding_runtime", rebuild)
    w.update(dir=tmp_path, built=built, CA=CA, sh=sh)
    return w


def _saved(world) -> dict:
    return yaml.safe_load(world["path"].read_text(encoding="utf-8")) or {}


def test_the_key_goes_live_is_written_and_read_back_after_a_restart(world):
    status, out = world["call"]("POST", "/api/config",
                                {"persist": True, "embedding": {"api_key": FAKE_KEY}})
    assert status == 200, out
    assert "embedding.api_key" in out["updated"]
    assert world["built"][-1]["api_key"] == FAKE_KEY, "the engine is rebuilt with it"
    assert _saved(world)["embedding"]["api_key"] == FAKE_KEY

    from utils import load_config
    assert load_config(str(world["path"]))["embedding"]["api_key"] == FAKE_KEY


def test_an_empty_key_keeps_the_saved_one(world):
    world["call"]("POST", "/api/config", {"persist": True, "embedding": {"api_key": FAKE_KEY}})
    status, out = world["call"]("POST", "/api/config", {"persist": True, "embedding": {
        "api_key": "", "timeout_seconds": 45}})
    assert status == 200, out
    assert "embedding.api_key" not in out["updated"]
    saved = _saved(world)["embedding"]
    assert saved["api_key"] == FAKE_KEY and saved["timeout_seconds"] == 45
    assert world["config"]["embedding"]["api_key"] == FAKE_KEY


def test_a_key_that_is_not_a_string_is_refused(world):
    status, out = world["call"]("POST", "/api/config", {"embedding": {"api_key": 12345}})
    assert status == 400 and "api_key" in out["error"]
    assert "api_key" not in world["config"]["embedding"]


def test_get_masks_it_and_neither_a_backup_nor_an_export_carries_it(world):
    world["call"]("POST", "/api/config", {"persist": True, "embedding": {"api_key": FAKE_KEY}})
    status, out = world["call"]("GET", "/api/config")
    assert status == 200 and FAKE_KEY not in json.dumps(out, ensure_ascii=False)
    assert out["embedding"]["api_key_masked"] == f"{FAKE_KEY[:4]}...{FAKE_KEY[-4:]}"

    (world["dir"] / "a_memory.md").write_text("---\nid: a\n---\nhello\n", encoding="utf-8")
    from core import schema as S
    with zipfile.ZipFile(S.backup(world["dir"], "keytest")) as zf:
        assert not any(n.endswith("config.yaml") for n in zf.namelist())
        assert all(FAKE_KEY.encode() not in zf.read(n) for n in zf.namelist())

    from core import export_package as E
    pattern, why = E._left_behind_reason("config.yaml")
    assert pattern == "config.yaml*" and "credentials" in why, \
        "the export package leaves config.yaml out"


def test_the_environment_variable_still_wins_at_startup(world, monkeypatch):
    world["call"]("POST", "/api/config", {"persist": True, "embedding": {"api_key": FAKE_KEY}})
    monkeypatch.setenv("LOCI_EMBED_API_KEY", ENV_KEY)
    from utils import load_config
    assert load_config(str(world["path"]))["embedding"]["api_key"] == ENV_KEY


def test_a_key_typed_with_a_new_model_is_saved_when_the_recompute_publishes(world, monkeypatch):
    from core import embedding_switch as ES
    world["config"]["embedding"]["api_key"] = ENV_KEY      # the running key, from the env
    monkeypatch.setattr(ES, "needs_reembed", lambda *a, **k: True)
    monkeypatch.setattr(ES, "busy", lambda: False)
    started = {}

    async def start(*, config, store, db_path, target, persist, publish):
        started.update(target=dict(target), persist=persist)
        publish(target, persist)               # the recompute is done at once
        return {"phase": "done"}
    monkeypatch.setattr(ES, "start", start)

    status, out = world["call"]("POST", "/api/config", {"persist": True, "embedding": {
        "model": "emb-model-2", "api_key": FAKE_KEY, "reembed": "confirm"}})
    assert status == 200, out
    assert started["target"]["api_key"] == FAKE_KEY
    assert world["config"]["embedding"]["api_key"] == FAKE_KEY
    saved = _saved(world)["embedding"]
    assert saved["model"] == "emb-model-2" and saved["api_key"] == FAKE_KEY

    # The same switch with no key typed: the env key runs, and is not written.
    world["path"].write_text("embedding:\n  model: emb-model\n", encoding="utf-8")
    world["config"]["embedding"]["api_key"] = ENV_KEY
    status, out = world["call"]("POST", "/api/config", {"persist": True, "embedding": {
        "model": "emb-model-3", "api_key": "", "reembed": "confirm"}})
    assert status == 200, out
    saved = _saved(world)["embedding"]
    assert saved["model"] == "emb-model-3" and "api_key" not in saved
    assert ENV_KEY not in world["path"].read_text(encoding="utf-8")
