# -*- coding: utf-8 -*-
"""
tests/test_health_groups.py — the setting page's five 体检 rows over the health checks.

WHAT IS AGREED
    GET /api/loci/health keeps every check in `checks` and adds `groups`: the canvas's five
    rows — Loci 活着没 · 副模型 · 向量模型 · 来源登记和变化口 · 跑着的这份 — each gathering
    checks by key, every check under exactly one row (one no row names falls under the
    first). A row's state is the worst of its checks'; its words are the row's own when
    all is well, else the worst check's, with that check's label when the row gathers
    several and how many more are not well; its `checks` say which rows of `checks` it
    stands for. The source registry is a check of its own, and the version the process
    started with is checked against the code on disk.
"""

import asyncio

import pytest

from core import health as H
from core import schema
from core.bucket_manager import BucketManager
from utils import get_version

LABELS = ["Loci 活着没", "副模型", "向量模型", "来源登记和变化口", "跑着的这份"]


def _persistent(bd):
    return {"persistent": True, "note": "test"}


@pytest.fixture
def check(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    schema.stamp_new_library(str(tmp_path))

    def go(config=None, running=""):
        cfg = {"buckets_dir": str(tmp_path)} if config is None else config
        return asyncio.run(H.health(store, cfg, _persistent, running=running))
    return go


def test_five_rows_and_every_check_under_exactly_one(check):
    out = check(running=get_version())
    groups = out["groups"]
    assert [g["label"] for g in groups] == LABELS
    assert [g["key"] for g in groups] == ["alive", "side_model", "embedding", "sources",
                                         "running"]
    under = [k for g in groups for k in g["checks"]]
    assert sorted(under) == sorted(r["key"] for r in out["checks"])
    assert len(under) == len(set(under))
    by = {g["key"]: g for g in groups}
    assert by["side_model"]["checks"] == ["dehydration"]
    assert set(by["embedding"]["checks"]) == {"vector_coverage", "embedding"}
    assert by["sources"]["checks"] == ["source_registry"]
    assert by["running"]["checks"] == ["schema", "running_version"]
    assert {"total", "tz", "disk", "literal_search", "dreams"} <= set(by["alive"]["checks"])


def test_the_registry_and_the_running_copy_are_checks_of_their_own(check):
    rows = {r["key"]: r for r in check(running=get_version())["checks"]}
    assert rows["source_registry"]["status"] == "ok"
    assert rows["source_registry"]["message"].startswith("读得出")
    assert rows["running_version"]["status"] == "ok"
    out = check(running="0.0.1")
    rows = {r["key"]: r for r in out["checks"]}
    assert rows["running_version"]["status"] == "warn"
    assert "重启" in rows["running_version"]["message"]
    running = next(g for g in out["groups"] if g["key"] == "running")
    assert running["status"] == "warn" and running["action"] == "重启 Loci"
    assert running["message"].startswith("跑着的这份：跑着的是 v0.0.1")
    # Without the version handed in there is nothing to compare: no row.
    assert "running_version" not in {r["key"] for r in check()["checks"]}


def test_the_worst_check_wins_and_says_why():
    rows = [
        {"key": "total", "label": "记忆总量", "status": "ok", "message": "3 条", "action": ""},
        {"key": "tz", "label": "时区", "status": "error", "message": "没设", "action": "设 LOCI_TZ"},
        {"key": "disk", "label": "磁盘", "status": "warn", "message": "快满了", "action": "腾地方"},
        {"key": "recent_writes", "label": "最近七天", "status": "note", "message": "还没存过",
         "action": ""},
        {"key": "dehydration", "label": "摘要/标签", "status": "ok", "message": "配着 deepseek",
         "action": ""},
        {"key": "embedding", "label": "向量", "status": "warn", "message": "关着",
         "action": "开 embedding.enabled"},
        {"key": "vector_coverage", "label": "语义搜索覆盖", "status": "ok", "message": "3/3",
         "action": ""},
    ]
    by = {g["key"]: g for g in H.groups(rows)}
    alive = by["alive"]
    assert alive["status"] == "error" and alive["action"] == "设 LOCI_TZ"
    assert alive["message"] == "时区：没设（还有 1 处）"
    assert alive["checks"] == ["total", "tz", "disk", "recent_writes"]
    # All well: the one check's own words.
    assert by["side_model"] == {"key": "side_model", "label": "副模型", "status": "ok",
                                "message": "配着 deepseek", "action": "",
                                "checks": ["dehydration"]}
    assert by["embedding"]["status"] == "warn"
    assert by["embedding"]["message"] == "向量：关着"
    # No check ran under it: said, never called well.
    assert by["sources"]["status"] == "unknown" and by["sources"]["message"] == "没查"
    assert by["running"]["checks"] == []


def test_a_note_is_not_a_problem():
    rows = [{"key": "total", "label": "记忆总量", "status": "ok", "message": "0 条", "action": ""},
            {"key": "pinned", "label": "钉着的准则", "status": "note", "message": "没有",
             "action": ""}]
    alive = H.groups(rows)[0]
    assert alive["status"] == "note" and alive["message"] == "正常" and alive["action"] == ""


def test_the_route_carries_both(monkeypatch, tmp_path):
    from starlette.requests import Request
    import json
    from web import _shared as sh
    from web import loci_health as LH

    store = BucketManager({"buckets_dir": str(tmp_path)})
    schema.stamp_new_library(str(tmp_path))
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(sh, "data_dir_persistence", _persistent, raising=False)
    monkeypatch.setattr(sh, "version", get_version())
    req = Request({"type": "http", "method": "GET", "path": "/", "headers": [],
                   "query_string": b""})
    out = json.loads(asyncio.run(LH.api_loci_health(req)).body)
    assert len(out["groups"]) == 5 and len(out["checks"]) >= 18
    assert "running_version" in {r["key"] for r in out["checks"]}
