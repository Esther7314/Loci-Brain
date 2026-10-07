# -*- coding: utf-8 -*-
"""
tests/test_health_row_keys.py — every row of the health check carries a stable `key`.

The panel matches rows by id, not by their words: each row of core/health.health has a
`key`, one per row in a response, the same from one run to the next, the same whatever the
row's status (ok or error), kept on the red row of a check that blew up or could not run;
the other fields are as they were.
"""

import asyncio

import pytest

from core import _when
from core import bm25_index
from core import health as H
from core import schema
from core.bucket_manager import BucketManager

FIELDS = {"key", "label", "status", "message", "action"}


def _persistent(bd):
    return {"persistent": True, "note": "test"}


@pytest.fixture
def run(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    schema.stamp_new_library(str(tmp_path))

    def go(config=None, bucket_mgr=None):
        cfg = {"buckets_dir": str(tmp_path)} if config is None else config
        out = asyncio.run(H.health(bucket_mgr or store, cfg, _persistent))
        return out["checks"]
    return go


def _by_key(rows):
    return {r["key"]: r for r in rows}


def test_every_row_has_a_key_and_the_same_fields(run):
    rows = run()
    assert rows
    for r in rows:
        assert set(r) == FIELDS
        assert isinstance(r["key"], str) and r["key"]
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys))


def test_keys_are_the_same_from_one_run_to_the_next(run):
    assert [r["key"] for r in run()] == [r["key"] for r in run()]


def test_a_row_keeps_its_key_whatever_its_status(run, monkeypatch):
    monkeypatch.setattr(bm25_index, "_BM25_AVAILABLE", True)
    monkeypatch.setattr(bm25_index, "_JIEBA_AVAILABLE", True)
    monkeypatch.setattr(_when, "_TZ_PROBLEM", "")
    green = _by_key(run())
    assert green["literal_search"]["status"] == "ok"
    assert green["tz"]["status"] == "ok"

    monkeypatch.setattr(bm25_index, "_JIEBA_AVAILABLE", False)
    monkeypatch.setattr(_when, "_TZ_PROBLEM", "LOCI_TZ is not set; using Asia/Shanghai")
    red = _by_key(run())
    assert red["literal_search"]["status"] == "error"
    assert red["tz"]["status"] == "error"
    assert red["literal_search"]["label"] == green["literal_search"]["label"]


def test_a_check_that_blows_up_keeps_its_key(run, tmp_path):
    rows = _by_key(run({"buckets_dir": str(tmp_path), "dehydration": "not a block"}))
    assert rows["dehydration"]["status"] == "error"
    assert "这一项自己出错了" in rows["dehydration"]["message"]


def test_an_unreadable_library_keeps_every_row_keyed(run):
    class _Broken:
        async def list_all(self, include_archive=False):
            raise OSError("disk gone")

    rows = run(bucket_mgr=_Broken())
    by_key = _by_key(rows)
    assert by_key["buckets_read"]["status"] == "error"
    for key in ("total", "recent_writes", "unbound_wants", "bare_quotes",
                "profile_page", "pinned", "periods", "from_links"):
        assert by_key[key]["status"] == "error"
        assert "读不到记忆库" in by_key[key]["message"]
    keys = [r["key"] for r in rows]
    assert len(keys) == len(set(keys))
