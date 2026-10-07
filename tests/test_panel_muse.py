# -*- coding: utf-8 -*-
"""
tests/test_panel_muse.py — GET /api/loci/muse: the clusters, and the days without a name.

The page reads the same cached pass the muse tool reads (core/_muse.both_sides) and
lays out one part at a time. A cluster's id is `c_` + sha1 of its sorted member ids, so
the same group in another order has the same id and one member more is a new group. Its
evidence is said in words: the shelf, the from-chains, the semantic top-up, each apart.
"""

import asyncio
import hashlib
import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest

from core import _muse as M
from core import _when as W
from core import muse_view as MV
from core.bucket_manager import BucketManager


def run(coro):
    return asyncio.run(coro)


def _item(bid, days_ago=3):
    return M.Item(id=bid, room="MIND/TRAITS", ts=None,
                  created=W.now() - timedelta(days=days_ago), v=0.6, a=0.3, tags=[], text="")


def _cluster(ids, from_core=(), semantic=(), days_ago=3):
    return M.Cluster(ids=list(ids), items=[_item(i, days_ago) for i in ids], shelf_v=0.6,
                     shelf_a=0.3, from_core=list(from_core), semantic_add=list(semantic))


A, B, C, D = "a1c3e5f7b9d2", "b2d4f6a8c0e1", "c3e5a7b9d1f2", "d4f6b8c0e2a3"


@pytest.fixture
def panel(tmp_path, monkeypatch):
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    monkeypatch.setattr(sh, "bucket_mgr", BucketManager({"buckets_dir": str(tmp_path)}))
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    clusters = [_cluster([A, B, C], from_core=[A, B], semantic=[C]),
                _cluster([C, D], semantic=[C, D], days_ago=0)]
    burst = M.Finger(name="词爆发", ids=[A, D], items=[_item(A), _item(D)],
                     start=W.now() - timedelta(days=9), end=W.now() - timedelta(days=6),
                     evidence="「海边」09-28~10-01 出现 5 次，之外 1 次")
    blank = M.Finger(name="空白记账", ids=[], items=[],
                     start=W.now() - timedelta(days=30), end=W.now() - timedelta(days=20),
                     evidence="09-07~09-17 有 4 条没落在任何一条时期的范围里")
    fingers = {"词爆发": [burst], "成分漂移": [], "空白记账": [blank]}

    async def both_sides(force=False, scope=None):
        return clusters, 0, 0, fingers, {}
    monkeypatch.setattr(M, "both_sides", both_sides)
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))

    def get(**query):
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": "/api/loci/muse",
                       "path_params": {}, "headers": [],
                       "query_string": urlencode(query).encode()})
        resp = run(routes[("GET", "/api/loci/muse")](req))
        return resp.status_code, json.loads(resp.body)
    return {"get": get, "clusters": clusters, "fingers": fingers}


def test_a_cluster_id_is_its_sorted_members():
    want = "c_" + hashlib.sha1(",".join(sorted([A, B, C])).encode()).hexdigest()[:10]
    assert MV.cluster_id([C, A, B]) == want == MV.cluster_id([A, B, C])
    assert MV.cluster_id([A, B, C, D]) != want, "one member more is a new group"


def test_evidence_is_said_in_words():
    assert MV.evidence_words(_cluster([A, B, C], from_core=[A, B], semantic=[C])) == \
        "心情落在一块 · 有 2 条是一路长出来的 · 意思上又补进 1 条"
    assert MV.evidence_words(_cluster([C, D], semantic=[C, D])) == \
        "心情落在一块 · 意思上又补进 2 条 · 没有一路长出来的痕迹，全靠意思相近"


def test_the_clusters_part(panel):
    status, out = panel["get"]()
    assert status == 200, out
    assert out["part"] == "clusters" and out["scope"] == "〔范围：全库（open）〕"
    first, second = out["items"]
    assert first["id"] == MV.cluster_id([A, B, C]) and first["ids"] == [A, B, C]
    assert first["kind"] == "thoughts" and first["n"] == 3
    assert first["evidence_words"].startswith("心情落在一块")
    assert first["oldest"] == (W.now() - timedelta(days=3)).date().isoformat()
    assert first["nudge"] == {"state": "none"}
    assert second["id"] == MV.cluster_id([C, D])
    assert out["total"] == 2


def test_the_days_part(panel):
    status, out = panel["get"](part="days")
    assert status == 200 and out["part"] == "days"
    burst, blank = out["items"]
    assert burst["kind"] == "burst" and burst["kind_words"] == "一个词扎堆出现"
    assert burst["id"] == MV.cluster_id([A, D]) and burst["ids"] == [A, D]
    assert burst["evidence_words"].startswith("「海边」") and burst["start"] and burst["end"]
    assert blank["kind"] == "blank" and blank["ids"] == [] and blank["n"] == 0
    assert blank["id"].startswith("c_") and len(blank["id"]) == 12


def test_muse_pages_with_as_of(panel):
    _s, first = panel["get"](limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1
    _s, earlier = panel["get"](as_of=(W.now() - timedelta(days=1)).isoformat(
        timespec="seconds"))
    assert [c["ids"] for c in earlier["items"]] == [[A, B, C]], \
        "a cluster with a member written after as_of is a new group since the first page"


def test_a_part_that_does_not_exist(panel):
    status, out = panel["get"](part="poke")
    assert status == 400 and "part" in out["error"]


def test_nothing_to_muse_on(panel, monkeypatch):
    async def empty(force=False, scope=None):
        return [], 0, 0, {"词爆发": [], "成分漂移": [], "空白记账": []}, {}
    monkeypatch.setattr(M, "both_sides", empty)
    for part in ("clusters", "days"):
        status, out = panel["get"](part=part)
        assert status == 200 and out["items"] == [] and out["total"] == 0
