# -*- coding: utf-8 -*-
"""
tests/test_panel_vectors.py — the settings page's vector block.

GET /api/loci/embedding/missing: the memories with no vector and why — keeps failing
(how many tries, the last error, when next), queued, or not queued at all (the index has
no vector and the outbox no item). POST /api/loci/embedding/backfill: 「现在补」 queues
what has no vector and makes every waiting item due now; like every panel write it
answers only a same-origin JSON request.
"""

import asyncio
import json
from urllib.parse import urlencode

import pytest

from core.bucket_manager import BucketManager
from core.embedding_outbox import EmbeddingOutbox, content_hash


def run(coro):
    return asyncio.run(coro)


class _Engine:
    enabled = True

    def __init__(self):
        self.have: set = set()

    def list_content_ids(self):
        return sorted(self.have)


@pytest.fixture
def panel(tmp_path, monkeypatch):
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    config = {"buckets_dir": str(tmp_path), "embedding": {"background_indexing": False}}
    store = BucketManager(config)
    engine = _Engine()
    outbox = EmbeddingOutbox(config, store, engine)
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "config", config)
    monkeypatch.setattr(sh, "embedding_engine", engine)
    monkeypatch.setattr(sh, "embedding_outbox", outbox)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))

    def call(method, route, headers=None, body=None, **query):
        from starlette.requests import Request
        raw = body if isinstance(body, bytes) else json.dumps(body or {}).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": method, "path": route, "path_params": {},
                       "headers": headers or [], "query_string": urlencode(query).encode()},
                      receive)
        resp = run(routes[(method, route)](req))
        return resp.status_code, json.loads(resp.body)

    async def seed():
        texts = ["考完去吃那家甜品。", "周末去海边。", "读书会改到周四。"]
        return [await store.create(t, room="EVENT/SELF") for t in texts], texts
    ids, texts = run(seed())
    # The index holds the third one's vector; the outbox holds the first, failed once,
    # and the second is in neither (what the creates queued is cleared first).
    for bid in ids:
        outbox.discard(bid)
    engine.have.add(ids[2])
    outbox.enqueue(ids[0], texts[0])
    outbox._fail(ids[0], content_hash(texts[0]), "429 Too Many Requests")
    return {"call": call, "store": store, "outbox": outbox, "engine": engine, "ids": ids,
            "sh": sh}


SAME_ORIGIN = [(b"host", b"127.0.0.1:8000"), (b"origin", b"http://127.0.0.1:8000"),
               (b"content-type", b"application/json")]


def test_what_has_no_vector_and_why(panel):
    status, out = panel["call"]("GET", "/api/loci/embedding/missing")
    assert status == 200, out
    a, b, c = panel["ids"]
    assert out["missing"] == 2 and out["total"] == 2
    assert out["circuit"] == "closed" and out["provider_ready"] is True and out["note"] == ""
    assert out["scope"] == "〔范围：全库（open）〕"
    failing, unqueued = out["items"]
    assert failing["id"] == a and failing["short"] == a[:6]
    assert failing["text"] == "考完去吃那家甜品。" and failing["why"] == "retrying"
    assert failing["why_words"] == "一直失败（试了 1 次：429 Too Many Requests）"
    assert failing["attempts"] == 1 and failing["next_try"]
    assert unqueued["id"] == b and unqueued["why"] == "not_queued"
    assert "现在补" in unqueued["why_words"] and unqueued["next_try"] is None
    assert c not in {i["id"] for i in out["items"]}, "it has a vector"


def test_missing_pages_and_says_when_the_model_is_off(panel, monkeypatch):
    _s, first = panel["call"]("GET", "/api/loci/embedding/missing", limit=1)
    assert first["total"] == 2 and first["next_offset"] == 1 and len(first["items"]) == 1
    monkeypatch.setattr(panel["engine"], "enabled", False)
    _s, off = panel["call"]("GET", "/api/loci/embedding/missing")
    assert off["provider_ready"] is False and off["note"]


def test_the_backfill_refuses_anything_but_a_same_origin_json_post(panel):
    call = panel["call"]
    status, out = call("POST", "/api/loci/embedding/backfill")
    assert status == 403 and "Origin" in out["error"]
    status, _out = call("POST", "/api/loci/embedding/backfill",
                        headers=[(b"host", b"127.0.0.1:8000"),
                                 (b"origin", b"http://127.0.0.1:9999"),
                                 (b"content-type", b"application/json")])
    assert status == 403
    status, _out = call("POST", "/api/loci/embedding/backfill",
                        headers=SAME_ORIGIN[:2] + [(b"content-type", b"text/plain")])
    assert status == 400
    assert panel["outbox"].pending_ids() == {panel["ids"][0]}, "nothing was queued"


def test_backfill_queues_what_has_no_vector_and_makes_the_waiting_due(panel):
    a, b, _c = panel["ids"]
    status, out = panel["call"]("POST", "/api/loci/embedding/backfill", headers=SAME_ORIGIN)
    assert status == 200, out
    assert out == {"ok": True, "queued": 1, "made_due": 1}
    assert panel["outbox"].pending_ids() == {a, b}
    _s, after = panel["call"]("GET", "/api/loci/embedding/missing")
    assert {i["why"] for i in after["items"]} == {"retrying", "queued"}
    queued = next(i for i in after["items"] if i["why"] == "queued")
    assert queued["id"] == b and queued["why_words"] == "排着队，还没轮到"
    assert all(i["next_try"] is None for i in after["items"]), "everything is due now"


def test_no_outbox_says_so(panel, monkeypatch):
    monkeypatch.setattr(panel["sh"], "embedding_outbox", None)
    status, out = panel["call"]("POST", "/api/loci/embedding/backfill", headers=SAME_ORIGIN)
    assert status == 503 and out["error"]
    status, out = panel["call"]("GET", "/api/loci/embedding/missing")
    assert status == 200 and out["missing"] == 2
    assert {i["why"] for i in out["items"]} == {"not_queued"}, "only what the index lacks is known"


def test_an_unreadable_index_lists_only_the_outbox(panel, monkeypatch):
    def broken():
        raise OSError("database is locked")
    monkeypatch.setattr(panel["engine"], "list_content_ids", broken)
    _s, out = panel["call"]("GET", "/api/loci/embedding/missing")
    assert [i["why"] for i in out["items"]] == ["retrying"]
