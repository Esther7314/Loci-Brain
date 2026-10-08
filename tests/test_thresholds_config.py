# -*- coding: utf-8 -*-
"""
tests/test_thresholds_config.py — the similarity lines live in config's `thresholds:` section.

Five cosine lines and recall's relevance floor (core/thresholds.py). With no section the
code runs on the values it was tuned with; the setting page reads and edits them through
/api/config, a bad value is refused whole with a note, and an edit reaches the code that
decides at its next call, with no restart. A change of embedding model raises a note that
the lines may need retuning; saving a line or dismissing it clears the note, and nothing
ever resets the lines.
"""

import asyncio
import json

import pytest
import yaml
from starlette.requests import Request

from core import runtime as rt
from core import thresholds as T

TUNED = {"reconsolidation": 0.80, "fold_merge": 0.80, "backfill_duplicate": 0.80,
         "recall_meaning": 0.65, "slice_guess": 0.65, "recall_floor": 35.0}


class _Engine:
    def __init__(self, model):
        self.model = model
        self.enabled = True


@pytest.fixture
def world(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import config_api as CA
    from web import panel_auth as PA

    config_path = tmp_path / "config.yaml"
    config_path.write_text("surfacing:\n  involuntary_lines: 2\n", encoding="utf-8")
    monkeypatch.setenv("LOCI_CONFIG_PATH", str(config_path))
    monkeypatch.delenv(T.FLOOR_ENV, raising=False)
    config = {"buckets_dir": str(tmp_path), "transport": "stdio"}
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(rt, "config", config)
    monkeypatch.setattr(sh, "embedding_engine", _Engine("bge-m3"))
    monkeypatch.setattr(PA, "gate_needed", lambda: False)

    def swap_engine():
        engine = _Engine(sh.config["embedding"]["model"])
        sh.embedding_engine = engine
        return engine
    monkeypatch.setattr(CA, "_rebuild_embedding_runtime", swap_engine)

    found = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                found[(methods[0], path)] = fn
                return fn
            return keep
    CA.register(_Mcp())

    def call(method, path, payload=None):
        raw = json.dumps(payload).encode() if payload is not None else b""

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}

        async def go():
            req = Request({"type": "http", "method": method, "path": path,
                           "query_string": b"",
                           "headers": [(b"host", b"127.0.0.1:8000"),
                                       (b"origin", b"http://127.0.0.1:8000"),
                                       (b"content-type", b"application/json")]}, receive)
            resp = await found[(method, path)](req)
            return resp.status_code, json.loads(resp.body)
        return asyncio.run(go())

    return {"call": call, "config": config, "config_path": config_path, "sh": sh,
            "CA": CA, "dir": tmp_path}


def _lines(out):
    return {r["key"]: r for r in out["thresholds"]["lines"]}


def test_with_no_section_every_line_runs_on_the_value_it_was_tuned_with(world):
    assert {line.key: T.value(line.key) for line in T.LINES} == TUNED
    assert {line.key: T.value(line.key, {}) for line in T.LINES} == TUNED
    status, out = world["call"]("GET", "/api/config")
    assert status == 200
    rows = _lines(out)
    assert {k: r["value"] for k, r in rows.items()} == TUNED
    assert {k: r["default"] for k, r in rows.items()} == TUNED
    assert not any(r["set"] for r in rows.values())
    assert out["thresholds"]["retune"]["needed"] is False


def test_the_two_older_homes_still_count_as_the_default(world, monkeypatch):
    monkeypatch.setenv(T.FLOOR_ENV, "28")
    world["config"]["slices"] = {"guess_threshold": 0.7}
    assert T.value(T.RECALL_FLOOR) == 28.0
    assert T.value(T.SLICE_GUESS) == 0.7
    world["config"]["thresholds"] = {"recall_floor": 40, "slice_guess": 0.6}
    assert T.value(T.RECALL_FLOOR) == 40.0
    assert T.value(T.SLICE_GUESS) == 0.6


def test_an_edit_reaches_the_deciding_code_at_its_next_call(world):
    from tools.grow import _backfill
    from tools.recall import core as recall_core
    from web.host_api import _slices_config

    class _Similar:
        enabled = True

        async def search_similar(self, text, top_k=3):
            return [("aaaaaaaaaaaa", 0.75)]

    class _Mgr:
        embedding_engine = _Similar()

    old_mgr, old_log = rt.bucket_mgr, rt.logger
    rt.bucket_mgr = _Mgr()

    class _Log:
        def info(self, *a, **k):
            pass
    rt.logger = _Log()
    try:
        assert asyncio.run(_backfill._possibly_same("bbbbbbbbbbbb", "x")) == []
        status, out = world["call"]("POST", "/api/config", {"thresholds": {
            "backfill_duplicate": 0.7, "recall_floor": 20, "slice_guess": 0.5}})
        assert status == 200, out
        assert asyncio.run(_backfill._possibly_same("bbbbbbbbbbbb", "x")) == [
            "疑似同件:aaaaaa"]
    finally:
        rt.bucket_mgr, rt.logger = old_mgr, old_log
    assert recall_core.relevance_floor() == 20.0
    assert _slices_config()[1] == 0.5
    rows = _lines(world["call"]("GET", "/api/config")[1])
    assert rows["backfill_duplicate"]["value"] == 0.7 and rows["backfill_duplicate"]["set"]
    assert rows["backfill_duplicate"]["default"] == 0.80


def test_a_bad_value_refuses_the_whole_request_and_says_which(world):
    for bad, word in (({"recall_meaning": 1.5}, "recall_meaning"),
                      ({"recall_floor": -1}, "recall_floor"),
                      ({"fold_merge": "high"}, "fold_merge"),
                      ({"fold_merge": True}, "fold_merge"),
                      ({"nonsense": 0.5}, "nonsense"),
                      ({"dismiss_retune": "yes"}, "dismiss_retune")):
        status, out = world["call"]("POST", "/api/config", {
            "thresholds": {"reconsolidation": 0.7, **bad}, "ai_name": "x"})
        assert status == 400, (bad, out)
        assert word in out["error"]
    status, out = world["call"]("POST", "/api/config", {"thresholds": [0.7]})
    assert status == 400
    # Nothing of the refused requests was applied.
    assert "thresholds" not in world["config"]
    assert "ai_name" not in world["config"]
    assert T.value(T.RECONSOLIDATION) == 0.80


def test_persisted_and_taken_back_to_the_default(world):
    status, out = world["call"]("POST", "/api/config", {"persist": True, "thresholds": {
        "reconsolidation": 0.75, "recall_floor": 30}})
    assert status == 200, out
    saved = yaml.safe_load(world["config_path"].read_text(encoding="utf-8"))
    assert saved["thresholds"] == {"reconsolidation": 0.75, "recall_floor": 30.0}
    assert saved["surfacing"]["involuntary_lines"] == 2

    status, out = world["call"]("POST", "/api/config", {"persist": True, "thresholds": {
        "reconsolidation": None}})
    assert status == 200, out
    assert T.value(T.RECONSOLIDATION) == 0.80
    saved = yaml.safe_load(world["config_path"].read_text(encoding="utf-8"))
    assert saved["thresholds"] == {"recall_floor": 30.0}


def test_a_value_written_by_hand_out_of_range_is_said_and_not_run(world):
    world["config"]["thresholds"] = {"recall_meaning": 7}
    assert T.value(T.RECALL_MEANING) == 0.65
    row = _lines(world["call"]("GET", "/api/config")[1])["recall_meaning"]
    assert row["value"] == 0.65 and not row["set"] and "缺省" in row["note"]


def test_a_model_change_raises_the_note_and_nothing_resets_the_lines(world):
    call = world["call"]
    call("POST", "/api/config", {"thresholds": {"recall_meaning": 0.6}})
    status, out = call("POST", "/api/config", {"embedding": {"model": "nomic-embed-text"}})
    assert status == 200, out
    status, out = call("GET", "/api/config")
    note = out["thresholds"]["retune"]
    assert note["needed"] is True
    assert (note["tuned_on"], note["model"]) == ("bge-m3", "nomic-embed-text")
    assert "bge-m3" in note["words"] and "不会自动改回缺省" in note["words"]
    assert _lines(out)["recall_meaning"]["value"] == 0.6

    status, out = call("POST", "/api/config", {"thresholds": {"dismiss_retune": True}})
    assert status == 200, out
    status, out = call("GET", "/api/config")
    assert out["thresholds"]["retune"]["needed"] is False
    assert _lines(out)["recall_meaning"]["value"] == 0.6


def test_saving_a_line_settles_the_note_and_the_recompute_switch_raises_it(world):
    from web.config_api import publish_embedding
    call = world["call"]
    assert call("GET", "/api/config")[1]["thresholds"]["retune"]["needed"] is False
    publish_embedding({"model": "text-embedding-3-small"}, False)
    assert call("GET", "/api/config")[1]["thresholds"]["retune"]["needed"] is True
    status, out = call("POST", "/api/config", {"thresholds": {"fold_merge": 0.85}})
    assert status == 200, out
    assert call("GET", "/api/config")[1]["thresholds"]["retune"]["needed"] is False


def test_the_first_switch_is_noticed_even_before_the_page_was_ever_read(world):
    publish = world["CA"].publish_embedding
    publish({"model": "text-embedding-3-small"}, False)
    note = world["call"]("GET", "/api/config")[1]["thresholds"]["retune"]
    assert note["needed"] is True and note["tuned_on"] == "bge-m3"


def test_the_same_model_spelled_another_way_is_not_a_change(world):
    T.settle(str(world["dir"]), "bge-m3")
    assert T.retune(str(world["dir"]), "bge-m3:latest")["needed"] is False
