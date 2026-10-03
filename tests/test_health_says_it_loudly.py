# -*- coding: utf-8 -*-
"""
tests/test_health_says_it_loudly.py — three things that used to fail quietly now have a
red row on the health page: literal search missing a dependency, LOCI_TZ unset, and a
library behind the code's version.
"""

import asyncio

import pytest

from core import _when
from core import bm25_index
from core import schema
from core.bucket_manager import BucketManager
from web import _shared as sh
from web import loci as W


@pytest.fixture
def health(tmp_path, monkeypatch):
    monkeypatch.setattr(sh, "bucket_mgr", BucketManager({"buckets_dir": str(tmp_path)}))
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)})

    def rows():
        out = asyncio.run(W.build_health())
        return {c["label"]: c for c in out["checks"]}
    return rows


def test_all_present_is_green(health, monkeypatch):
    monkeypatch.setattr(bm25_index, "_BM25_AVAILABLE", True)
    monkeypatch.setattr(bm25_index, "_JIEBA_AVAILABLE", True)
    monkeypatch.setattr(_when, "_TZ_PROBLEM", "")
    schema.stamp_new_library(sh.config["buckets_dir"])
    rows = health()
    assert rows["字面搜索"]["status"] == "ok"
    assert rows["时区"]["status"] == "ok"
    assert rows["库的版本"]["status"] == "ok"


def test_missing_jieba_is_red_and_says_what_to_install(health, monkeypatch):
    monkeypatch.setattr(bm25_index, "_JIEBA_AVAILABLE", False)
    row = health()["字面搜索"]
    assert row["status"] == "error"
    assert "pip install jieba" in row["action"]


def test_unset_timezone_is_red(health, monkeypatch):
    monkeypatch.setattr(_when, "_TZ_PROBLEM", "LOCI_TZ is not set; using Asia/Shanghai")
    row = health()["时区"]
    assert row["status"] == "error"
    assert "LOCI_TZ" in row["action"]


def test_a_library_behind_the_code_is_red(health, tmp_path):
    (tmp_path / "dynamic").mkdir()
    (tmp_path / "dynamic" / "a_aaaaaaaaaaaa.md").write_text(
        "---\nid: aaaaaaaaaaaa\nroom: EVENT/SELF\n---\nbody\n", encoding="utf-8")
    row = health()["库的版本"]
    assert row["status"] == "error"
    assert "scripts/migrate.py" in row["action"]


def test_open_wants_nobody_is_bound_by_are_red_and_named(health):
    store = sh.bucket_mgr

    async def seed():
        hidden = await store.create("Make the birthday game.", room="EVENT/SELF",
                                    direction_of_fit="telic")
        await store.create("Fix the bike.", room="EVENT/SELF", direction_of_fit="telic",
                           bound=["AI"])
        await store.create("Renew the passport.", room="EVENT/SELF",
                           direction_of_fit="telic", when="2026-12-01")
        await store.create("Ask after the exam.", room="EVENT/SELF",
                           direction_of_fit="telic", cue={"condition": "exam is over"})
        closed = await store.create("Old promise.", room="EVENT/SELF",
                                    direction_of_fit="telic")
        await store.update(closed, status="resolved")
        return hidden
    hidden = asyncio.run(seed())
    row = health()["没人认领的想要"]
    # Criterion: only the open, undated, condition-less want with no bound is named —
    # exactly the ones 惦记的事 never shows.
    assert row["status"] == "error"
    assert row["message"].startswith("1 条") and hidden in row["message"]
    assert "bound" in row["action"]


def test_no_unbound_wants_is_green(health):
    asyncio.run(sh.bucket_mgr.create("Fix the bike.", room="EVENT/SELF",
                                     direction_of_fit="telic", bound=["AI"]))
    assert health()["没人认领的想要"]["status"] == "ok"
