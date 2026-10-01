# -*- coding: utf-8 -*-
"""
tests/test_source_change.py — a host's source change: state, block, clearing, resending.

A memory E is formed from one message M of the host; D is derived from E. M carries a
phrase nothing else has, and the phrase is put in every place Loci keeps text: E's body
and name, its sunk original, its vector row, a dream woven from it, the dehydration
cache, a pending slice's gist, a lookup's query in the usage log, and a ledger line
written the old way (whole metadata). After `withdrawn` the phrase is nowhere under the
library — read back byte by byte — and each place answered `done`. Then: D is held for
review on every road and E's body is gone for good; a crash halfway is finished by a
resend; a restore lifts D; a line inside a run reaches the run; the route answers
through the hook guard with a host's credential.
"""

import asyncio
import json
import os
import sqlite3

import frontmatter
import pytest

from core import _dream
from core import _invalidation as I
from core import _slicer as SL
from core import _source_change as SC
from core import _sources as S
from core import visibility as V
from core.bucket_manager import BucketManager
from core.scope import Host
from utils import WAS_DERIVED_FROM

PHRASE = "暗号是青柠汽水"
M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003"}
M_STR = "lento:home/private:U#m_0003"
OPEN_HOST = Host("life", scope_mode="open", may_restore=True)


def run(coro):
    return asyncio.run(coro)


def change(cid, kind, seq, source=M_STR, **kw):
    return {"change_id": cid, "source": source, "host_seq": seq, "change": kind, **kw}


def _files_holding(root, needle: str) -> list[str]:
    raw = needle.encode("utf-8")
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(dirpath, f)
            with open(p, "rb") as fh:
                if raw in fh.read():
                    hits.append(os.path.relpath(p, root))
    return hits


def _meta(store, bid) -> dict:
    path = store._find_bucket_file(bid)
    return dict(frontmatter.load(path).metadata)


@pytest.fixture
def library(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    e = run(store.create(f"小周说{PHRASE}，周六要去海边。", name=f"{PHRASE}的约定",
                         summary=f"{PHRASE}与海边", sources=[M], room="EVENT/WORLD"))
    d = run(store.create("小周喜欢夏天出门。", room="MIND/TRAITS",
                         prov=[{"rel": WAS_DERIVED_FROM, "target": e}]))
    run(store.sink_bucket(e))
    # The vector row, as the engine writes it.
    with sqlite3.connect(tmp_path / "embeddings.db") as c:
        c.execute("CREATE TABLE embeddings (bucket_id TEXT PRIMARY KEY, embedding TEXT, "
                  "updated_at TEXT, content_hash TEXT, meaning_embedding TEXT)")
        c.execute("INSERT INTO embeddings VALUES (?, '[0.1]', '', '', NULL)", (e,))
    with sqlite3.connect(tmp_path / "dehydration_cache.db") as c:
        c.execute("CREATE TABLE dehydration_cache (content_hash TEXT PRIMARY KEY, "
                  "summary TEXT NOT NULL, model TEXT NOT NULL, created_at TEXT)")
        c.execute("INSERT INTO dehydration_cache VALUES ('k1', ?, 'm', '')",
                  (json.dumps({"summary": f"关于{PHRASE}"}, ensure_ascii=False),))
        c.execute("INSERT INTO dehydration_cache VALUES ('k2', '别的事', 'm', '')")
    _dream.save_record({"id": "d00000000001", "完整": f"梦里{PHRASE}在发光",
                        "碎片": "海边", "素材": {"压在心头": [e], "想不明白": []}},
                       str(tmp_path))
    _dream.save_record({"id": "d00000000002", "完整": "梦见下雨", "碎片": "雨",
                        "素材": {"压在心头": [], "想不明白": []}}, str(tmp_path))
    media = tmp_path / "_media" / e
    media.mkdir(parents=True)
    (media / "photo.bin").write_bytes(b"x")

    async def side_model(system, user):
        return json.dumps({"slices": [{"from": 1, "to": 4, "gist": f"聊到{PHRASE}"},
                                      {"from": 5, "to": 6, "gist": "说晚安"}]})
    run(SL.take_batch(store, {
        "source": {k: M[k] for k in ("system", "instance", "container")},
        "day": "2026-10-01",
        "lines": [{"id": f"m_000{i}", "text": f"line {i}"} for i in range(1, 7)]},
        model=side_model))
    store.usage.record("found", [e], "recall.search", query=PHRASE, gates={"when": ""})
    store.usage.record("found", [d], "recall.search", query="夏天", gates={"when": ""})
    # A ledger line written before lines held names only.
    store.ledger_mirror.append_event(event_type="TraceUpdated", trace_id=e,
                                     trace_kind="dynamic",
                                     payload={"name": f"{PHRASE}的约定",
                                              "changed_fields": ["name"]}, body="x")
    return store, e, d, tmp_path


def test_withdrawn_clears_every_place_and_leaves_the_phrase_nowhere(library):
    store, e, d, root = library
    assert _files_holding(root, PHRASE), "the phrase is planted"
    status, out = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert status == 200 and out["status"] == "applied", out
    assert out["blocked"] is True and out["state"] == "withdrawn"
    assert out["entries"] == [e] and out["derived_pending"] == [d]
    assert all(v == "done" for v in out["cleanup"].values()), out["cleanup"]
    assert isinstance(out["applied_seq"], int)
    assert _files_holding(root, PHRASE) == []
    # One place by one: each is really empty.
    assert not os.path.exists(store._sunk_orig_path(e))
    assert os.path.basename(store._find_bucket_file(e)) == f"已清_{e}.md"
    with sqlite3.connect(root / "embeddings.db") as c:
        assert c.execute("SELECT count(*) FROM embeddings").fetchone()[0] == 0
    with sqlite3.connect(root / "dehydration_cache.db") as c:
        assert [r[0] for r in c.execute("SELECT content_hash FROM dehydration_cache")] == ["k2"]
    assert [r["id"] for r in _dream.load_dreams(str(root))] == ["d00000000002"]
    assert not (root / "_media" / e).exists()
    rows = store.usage.read()
    assert all("query" not in r for r in rows if e in r.get("ids", []) or d in r.get("ids", []))
    # Every place done wrote one ledger line, after the change's own.
    events = list(store.ledger_mirror.iter_since(out["applied_seq"] - 1))
    assert events[0]["event_type"] == "SourceChanged"
    cleared = [x["payload"]["place"] for x in events if x["event_type"] == "SourceCleared"]
    assert sorted(cleared) == sorted(SC.PLACES)


def test_what_stood_on_it_is_held_on_every_road_but_the_review(library):
    store, e, d, _root = library
    run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    for bid in (e, d):
        meta = _meta(store, bid)
        assert V.source_gone(meta)
        for road in V.ROADS:
            verdict = V.visible_for(meta, road=road, holds=_HoldsAll())
            assert bool(verdict) == (road == V.INVALIDATION), (bid, road)
    items = {it["id"]: it for it in I.block(run(store.list_all()), store.sources)}
    assert items[e]["text"] is None and items[d]["text"] is None
    assert _meta(store, d).get("name") is not None, "derived: held for review, not cleared"
    refusal = I.confirm(_meta(store, d), store.sources, edited=False, today="2026-10-01")[1]
    assert "照留不行" in refusal


class _HoldsAll:
    """A hold index with nothing held (the roads that count holds need one)."""
    def __getattr__(self, name):
        from core import _holds
        return getattr(_holds.hold_index([]), name)


def test_a_crash_halfway_is_finished_by_resending_the_same_change(library, monkeypatch):
    store, e, _d, root = library
    calls = {"n": 0}
    real = SC._RUN["embeddings"]

    def flaky(s, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("database is locked")
        return real(s, ctx)
    monkeypatch.setitem(SC._RUN, "embeddings", flaky)
    _s, first = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert first["status"] == "applied" and first["cleanup"]["embeddings"] == "pending"
    assert first["cleanup"]["bm25"] == "pending" and first["cleanup"]["body"] == "done"
    _s, second = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert second["status"] == "applied", "carried on, not the old result"
    assert all(v == "done" for v in second["cleanup"].values()), second["cleanup"]
    assert second["applied_seq"] == first["applied_seq"]
    _s, third = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert third["status"] == "duplicate" and third["cleanup"] == second["cleanup"]
    assert _files_holding(root, PHRASE) == []
    kinds = [x["event_type"] for x in store.ledger_mirror.iter_events()]
    assert kinds.count("SourceChanged") == 1


def test_restore_lifts_what_was_derived_but_not_a_cleared_body(library):
    store, e, d, _root = library
    run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    no_restore = Host("bot", scope_mode="open")
    _s, out = run(SC.handle(store, change("c-2", "restored", 2), no_restore))
    assert out["status"] == "forbidden" and out["note"] == "may_restore_required"
    _s, out = run(SC.handle(store, change("c-3", "restored", 2), OPEN_HOST))
    assert out["status"] == "applied" and out["state"] == "active" and not out["blocked"]
    assert out["note"] == "redeliver", "the cleared body does not come back with the state"
    assert not V.source_gone(_meta(store, d))
    assert V.source_gone(_meta(store, e))
    assert run(store.get(e))["content"].strip() == store.CLEARED_BODY


def test_deleted_then_unreadable_stays_deleted_and_a_revision_keeps_it(library):
    store, *_ = library
    run(SC.handle(store, change("c-1", "deleted", 1), OPEN_HOST))
    _s, out = run(SC.handle(store, change("c-2", "unreadable", 2), OPEN_HOST))
    assert out["state"] == "deleted" and out["note"] == "unreadable_cannot_mask"
    _s, out = run(SC.handle(store, change("c-3", "revised", 3, revision="2"), OPEN_HOST))
    assert out["state"] == "deleted" and out["blocked"]
    assert store.sources.revisions_of(M_STR)[-1]["revision"] == "2"


def test_the_other_outcomes(library):
    store, *_ = library
    run(SC.handle(store, change("c-5", "unreadable", 5), OPEN_HOST))
    _s, out = run(SC.handle(store, change("c-3", "restored", 3), OPEN_HOST))
    assert out["status"] == "stale" and out["applied_seq"] is None
    _s, out = run(SC.handle(store, change("c-5", "withdrawn", 5), OPEN_HOST))
    assert out["status"] == "conflict" and out["conflict"]["recorded"]["change"] == "unreadable"
    _s, out = run(SC.handle(store, change("c-9", "withdrawn", 1,
                                          source="lento:home/private:U#m_9999"), OPEN_HOST))
    assert out["status"] == "unknown_source" and out["applied_seq"] and out["entries"] == []
    assert store.sources.state_of("lento:home/private:U#m_9999") == "withdrawn"
    ceiling = Host("bot", max_grant=(S.Place("telegram", "bot-a"),))
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), ceiling))
    assert out["status"] == "forbidden" and out["exceeds"] == "lento/home/private:U#m_0003"
    assert store.sources.state_of(M_STR) == "unreadable", "refused whole"
    assert run(SC.handle(store, {"change_id": "x"}, OPEN_HOST))[0] == 400
    assert run(SC.handle(store, change("c-1", "gone", 1), OPEN_HOST))[0] == 400
    assert run(SC.handle(store, change("c-1", "withdrawn", 1), "panel"))[0] == 403


def test_a_line_inside_a_run_reaches_the_memory_carrying_the_run(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    store.sources.record_order({"system": "lento", "instance": "home", "container": "c"},
                               [f"m_{i}" for i in range(1, 10)])
    run_rec = {"system": "lento", "instance": "home", "container": "c", "id": "m_2",
               "through": "m_6"}
    e = run(store.create(f"一段话，{PHRASE}。", sources=[run_rec]))
    other = run(store.create("后面的话。", sources=[{**run_rec, "id": "m_7", "through": "m_9"}]))
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1,
                                          source="lento:home/c#m_4"), OPEN_HOST))
    assert out["entries"] == [e] and out["cleanup"]["body"] == "done"
    assert run(store.get(e))["content"].strip() == store.CLEARED_BODY
    assert not V.source_gone(_meta(store, other))
    assert store.sources.state_of("lento:home/c#m_2..m_6") == "withdrawn"


def test_the_route_answers_through_the_hook_guard(library, monkeypatch):
    from starlette.requests import Request
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    store, e, _d, _root = library
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setenv("T_BOT", "bot-key")
    monkeypatch.setenv("T_LIFE", "life-key")
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "dehydrator", None)
    monkeypatch.setattr(sh, "config", {"hosts": {
        "life": {"token_env": "T_LIFE", "scope_mode": "open", "may_restore": True},
        "bot": {"token_env": "T_BOT", "max_grant": [{"system": "telegram"}]}}})
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    monkeypatch.setattr(PA, "has_session", lambda r: False)

    def call(method, path, payload=None, key=None, query=b""):
        raw = json.dumps(payload).encode() if payload is not None else b""
        headers = [(b"content-type", b"application/json")]
        if key:
            headers.append((b"x-loci-hook-token", key.encode()))

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "headers": headers,
                       "query_string": query}, receive)
        resp = asyncio.run(routes[(method, path)](req))
        return resp.status_code, json.loads(resp.body)

    assert call("POST", "/api/v2/source/change", change("c-1", "withdrawn", 1))[0] == 401
    status, out = call("POST", "/api/v2/source/change", change("c-1", "withdrawn", 1),
                       key="bot-key")
    assert status == 200 and out["status"] == "forbidden"
    status, out = call("POST", "/api/v2/source/change", change("c-1", "withdrawn", 1),
                       key="life-key")
    assert status == 200 and out["status"] == "applied" and out["entries"] == [e]
    status, seen = call("GET", "/api/v2/changes", key="life-key", query=b"since=0")
    assert status == 200 and any(r["type"] == "SourceChanged" for r in seen["changes"])
    assert PHRASE not in json.dumps(seen, ensure_ascii=False)
    status, seen = call("GET", "/api/v2/changes", key="bot-key", query=b"since=0")
    assert status == 200 and seen["changes"] == [], "nothing of lento's reaches the bot"
    assert call("GET", "/api/v2/changes", key="life-key", query=b"since=x")[0] == 400
