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
resend; a restore gives the source back but D waits for review until it is confirmed
or rewritten; a line inside a run reaches the run; the route answers
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


def test_restore_gives_back_the_source_but_what_was_derived_waits_for_review(library):
    from tools.breath.awaken import _invalidation_lines
    store, e, d, _root = library
    run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    no_restore = Host("bot", scope_mode="open")
    _s, out = run(SC.handle(store, change("c-2", "restored", 2), no_restore))
    assert out["status"] == "forbidden" and out["note"] == "may_restore_required"
    _s, out = run(SC.handle(store, change("c-3", "restored", 2), OPEN_HOST))
    assert out["status"] == "applied" and out["state"] == "active" and not out["blocked"]
    assert out["note"] == "redeliver", "the cleared body does not come back with the state"
    assert out["derived_pending"] == [d], "the restore names what still waits"
    # The entry: its body is gone for good, and so its record stays.
    assert V.source_gone(_meta(store, e))
    assert run(store.get(e))["content"].strip() == store.CLEARED_BODY
    # What was derived: not gone any more, not back either — off every road but the review
    # and a read by id, where the review reads it.
    meta = _meta(store, d)
    assert not V.source_gone(meta) and V.source_restored(meta)
    for road in V.ROADS:
        verdict = V.visible_for(meta, road=road, holds=_HoldsAll())
        assert bool(verdict) == (road in (V.INVALIDATION, V.READ)), road
    items = {it["id"]: it for it in I.block(run(store.list_all()), store.sources)}
    it = items[d]
    assert [r["source"] for r in it["restored"]] == [M_STR] and it["failed"] == []
    assert it["text"], "the source may be used again: the review shows the text"
    [line, *_hints] = _invalidation_lines({"items": [it], "more": 0})
    assert "现在恢复了" in line and "等你看过" in line
    # Resent, the same answer.
    _s, again = run(SC.handle(store, change("c-3", "restored", 2), OPEN_HOST))
    assert again["status"] == "duplicate" and again["derived_pending"] == [d]


def test_what_waits_after_a_restore_comes_back_once_confirmed_or_rewritten(library):
    from tools.recall import core as R
    from tools.regrow import dispatch as regrow
    from tools.trace import dispatch as trace
    store, e, d, _root = library
    d2 = run(store.create("小周周末总往外跑。", room="MIND/TRAITS",
                          prov=[{"rel": WAS_DERIVED_FROM, "target": e}]))
    run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    _s, out = run(SC.handle(store, change("c-2", "restored", 2), OPEN_HOST))
    assert set(out["derived_pending"]) == {d, d2}
    # Read by id: the whole entry, under a line saying it waits and how to settle it.
    read = run(R.recall_core(when="", room="", tag="", query=d))
    lines = read.splitlines()
    assert lines[0].startswith(f"═ {d}") and "现在恢复了" in lines[1] and "还没复核" in lines[1]
    assert f'trace(bucket_id="{d}", invalidation="confirmed")' in lines[1] and "regrow" in lines[1]
    assert "小周喜欢夏天出门" in read
    # Nowhere else: not listed, not in the three days, not carded.
    meta = _meta(store, d)
    assert not any(V.visible_for(meta, road=road, holds=_HoldsAll())
                   for road in (V.LIST, V.RECENT, V.CUE))
    # Kept as it is: the gesture the line offers closes the record, and it is back.
    said = run(trace(bucket_id=d, invalidation="confirmed"))
    assert "确认照留" in said, said
    meta = _meta(store, d)
    assert not V.source_restored(meta) and not I.open_records(meta)
    assert all(V.visible_for(meta, road=road, holds=_HoldsAll())
               for road in (V.READ, V.LIST, V.RECENT, V.CUE))
    read = run(R.recall_core(when="", room="", tag="", query=d))
    assert "小周喜欢夏天出门" in read and "还没复核" not in read
    # Rewritten: the new version carries no mark, the old one leaves the review.
    run(regrow(bucket_id=d2, mode="supplement", text="小周周末总往外跑，夏天更是。", v=0.5, a=0.3))
    newer = _meta(store, d2)["superseded_by"]
    assert "invalidation" not in _meta(store, newer)
    assert V.visible_for(_meta(store, newer), road=V.READ)
    carded = [it["id"] for it in I.block(run(store.list_all()), store.sources)]
    assert d not in carded and d2 not in carded and newer not in carded


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
    monkeypatch.setenv("T_RELAY", "relay-key")
    monkeypatch.setattr(sh, "config", {"hosts": {
        "life": {"token_env": "T_LIFE", "scope_mode": "open", "may_restore": True,
                 "authority": [{"system": "lento"}]},
        "bot": {"token_env": "T_BOT", "max_grant": [{"system": "telegram"}]},
        "relay": {"token_env": "T_RELAY", "max_grant": [{"system": "lento"}]}}})
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
    # Within its ceiling, but not the source's change authority: refused, and nothing moves.
    status, out = call("POST", "/api/v2/source/change", change("c-1", "withdrawn", 1),
                       key="relay-key")
    assert (status, out["status"], out["note"]) == (200, "forbidden", "not_change_authority")
    assert store.sources.state_of(M_STR) == "active"
    status, out = call("POST", "/api/v2/source/change", change("c-1", "withdrawn", 1),
                       key="life-key")
    assert status == 200 and out["status"] == "applied" and out["entries"] == [e]
    assert isinstance(out["applied_seq"], int), "an open host sees the ledger's numbers"
    status, seen = call("GET", "/api/v2/changes", key="life-key", query=b"since=0")
    assert status == 200 and any(r["type"] == "SourceChanged" for r in seen["changes"])
    assert PHRASE not in json.dumps(seen, ensure_ascii=False)
    status, seen = call("GET", "/api/v2/changes", key="bot-key", query=b"since=0")
    assert status == 200 and seen["changes"] == [], "nothing of lento's reaches the bot"
    assert "since" not in seen and isinstance(seen["next"], str), "a cursor, not a seq"
    assert call("GET", "/api/v2/changes", key="bot-key", query=b"since=3")[0] == 400
    assert call("GET", "/api/v2/changes", key="bot-key", query=b"cursor=c1nope")[0] == 400
    assert call("GET", "/api/v2/changes", key="life-key", query=b"cursor=x")[0] == 400
    assert call("GET", "/api/v2/changes", key="life-key", query=b"since=x")[0] == 400
    # A run's lines, registered by a host directly.
    lines = {"source": "lento:home/private:U#m_0010..m_0012", "revision": "w-1",
             "lines": ["m_0010", {"id": "m_0011", "revision": "e1"}, "m_0012"]}
    assert call("POST", "/api/v2/source/lines", lines)[0] == 401
    status, out = call("POST", "/api/v2/source/lines", lines, key="bot-key")
    assert (status, out["status"]) == (200, "forbidden")
    # Within its ceiling, but the lines are life's: only their authority puts them in a run.
    status, out = call("POST", "/api/v2/source/lines", lines, key="relay-key")
    assert (status, out["status"], out["note"]) == (200, "forbidden", "not_change_authority")
    status, out = call("POST", "/api/v2/source/lines", lines, key="life-key")
    assert (status, out["status"], out["lines"]) == (200, "recorded", 3)
    assert store.sources.members_of("lento:home/private:U#m_0010..m_0012") == [
        "m_0010", "m_0011", "m_0012"]
    assert call("POST", "/api/v2/source/lines", {**lines, "lines": ["m_0010"]},
                key="relay-key")[0] == 400


# ───────────────────── the other team's 10-02 rulings ─────────────────────

def _hosts(**spec):
    from core import scope as SCOPE
    env = {f"T_{name.upper()}": f"{name}-key" for name in spec}
    table = {name: {"token_env": f"T_{name.upper()}", **body} for name, body in spec.items()}
    return SCOPE.load_hosts({"hosts": table}, env)


def test_only_the_declared_authority_sends_and_its_order_is_the_one_order(library):
    # Their counter-example: B's newer withdrawal arrives first, A's older restore later.
    store, e, _d, _root = library
    hosts = _hosts(a={"max_grant": [{"system": "lento"}], "may_restore": True},
                   b={"max_grant": [{"system": "lento"}], "may_restore": True,
                      "authority": [{"system": "lento", "instance": "home"}]})
    a, b = hosts.get("a"), hosts.get("b")
    _s, out = run(SC.handle(store, change("b-7", "withdrawn", 7), b, hosts=hosts))
    assert out["status"] == "applied" and out["state"] == "withdrawn"
    _s, out = run(SC.handle(store, change("a-5", "restored", 5), a, hosts=hosts))
    assert (out["status"], out["note"]) == ("forbidden", SC.NOT_AUTHORITY)
    # Relayed through the authority, the older restore is still older.
    _s, out = run(SC.handle(store, change("b-5", "restored", 5), b, hosts=hosts))
    assert out["status"] == "stale"
    assert store.sources.state_of(M_STR) == "withdrawn"
    # A source nobody is declared the authority of: nobody may change it.
    _s, out = run(SC.handle(store, change("x", "withdrawn", 1,
                                          source="lento:work/c#m_1"), b, hosts=hosts))
    assert (out["status"], out["note"]) == ("forbidden", SC.NO_AUTHORITY)


def test_two_hosts_declaring_the_same_place_name_neither_and_authority_stays_in_the_ceiling():
    hosts = _hosts(a={"max_grant": [{"system": "lento"}], "authority": [{"system": "lento"}]},
                   b={"max_grant": [{"system": "lento"}], "authority": [{"system": "lento"}]})
    assert hosts.authority_for("lento:home/c#m_1") is None
    deeper = _hosts(a={"max_grant": [{"system": "lento"}], "authority": [{"system": "lento"}]},
                    b={"max_grant": [{"system": "lento"}],
                       "authority": [{"system": "lento", "instance": "home"}]})
    assert deeper.authority_for("lento:home/c#m_1").name == "b"
    assert deeper.authority_for("lento:work/c#m_1").name == "a"
    past = _hosts(a={"max_grant": [{"system": "telegram"}], "authority": [{"system": "lento"}]})
    assert past.get("a") is None and past.errors


def test_without_a_hosts_table_the_legacy_host_is_the_authority_for_everything(library):
    from core import scope as SCOPE
    store, e, _d, _root = library
    hosts = SCOPE.load_hosts({}, {}, legacy_token="life-key")
    legacy = hosts.default
    assert hosts.authority_for(M_STR) is legacy and hosts.provider_for(M_STR) is None
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), legacy, hosts=hosts))
    assert out["status"] == "applied" and out["entries"] == [e]
    assert isinstance(out["applied_seq"], int) and "applied_cursor" not in out


def test_a_host_with_a_ceiling_gets_a_cursor_not_the_ledgers_number(library):
    from core import _ledger as L
    store, e, _d, _root = library
    bridge = Host("bridge", max_grant=(S.Place("lento"),))
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), bridge))
    assert "applied_seq" not in out and out["applied_cursor"].startswith("c1")
    seen = run(L.changes_since(store, bridge))
    assert out["applied_cursor"] in [r["cursor"] for r in seen["changes"]]
    assert all("seq" not in r for r in seen["changes"])


def test_a_memory_only_quoting_the_source_is_reached_by_its_withdrawal(library):
    # Found by our own dream worker: a wasQuotedFrom line is a tie to the source too.
    store, e, _d, _root = library
    q = run(store.create(f"小周说过一句：{PHRASE}。", room="EVENT/SELF",
                         prov=[{"rel": "wasQuotedFrom", "target": M_STR}]))
    bare = run(store.create("只记了宿主的编号。", room="EVENT/SELF",
                            prov=[{"rel": "wasQuotedFrom", "target": "m_0003"}]))
    assert set(run(S.memories_of(store, M_STR))) == {e, q}
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert set(out["entries"]) == {e, q}
    assert run(store.get(q))["content"].strip() == store.CLEARED_BODY
    assert bare not in out["entries"], "a bare host id names no container: not matched"


def test_a_cleared_body_keeps_what_carries_the_next_withdrawal(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    where = {"system": "lento", "instance": "home", "container": "c"}
    store.sources.record_order(where, [f"m_{i}" for i in range(1, 10)])
    run_rec = {**where, "id": "m_2", "through": "m_6"}
    e = run(store.create(f"一段话，{PHRASE}。", sources=[run_rec]))
    run(SC.handle(store, change("c-1", "withdrawn", 1, source="lento:home/c#m_3"), OPEN_HOST))
    meta = _meta(store, e)
    assert meta["sources"][0]["through"] == "m_6", "the run's identity stays on the md"
    assert store.sources.members_of("lento:home/c#m_2..m_6") == [f"m_{i}" for i in range(2, 7)]
    assert run(S.memories_of(store, "lento:home/c#m_5")) == [e], "another line still finds it"
    _s, out = run(SC.handle(store, change("c-2", "deleted", 1, source="lento:home/c#m_5"),
                            OPEN_HOST))
    assert out["entries"] == [e] and store.sources.state_of("lento:home/c#m_2..m_6") == "deleted"


def test_a_hosts_unordered_word_holds_until_the_ordered_change_settles_it(library):
    store, e, d, _root = library
    held = run(SC.hold(store, M_STR, "withdrawn", "lento"))
    assert set(held) == {e, d}
    assert store.sources.state_of(M_STR) == S.HELD
    assert store.sources.describe(M_STR) is None, "no state was written"
    for bid in (e, d):
        meta = _meta(store, bid)
        assert V.source_gone(meta)
        assert not V.visible_for(meta, road=V.READ) and not V.visible_for(meta, road=V.RECENT)
    assert run(store.get_including_archive(e))["content"].strip() != store.CLEARED_BODY
    [rec] = S.normalize_sources([M])
    assert "正等宿主的变化通知确认" in store.sources.check_writable([rec])[0]
    # The ordered change: a restore of a source that was never withdrawn settles it.
    _s, out = run(SC.handle(store, change("c-1", "restored", 1), Host("x", scope_mode="open")))
    assert out["status"] == "applied" and store.sources.state_of(M_STR) == "active"
    for bid in (e, d):
        assert not V.source_gone(_meta(store, bid))


def test_a_held_source_settled_by_its_withdrawal_is_cleared_then(library):
    store, e, d, _root = library
    run(SC.hold(store, M_STR, "deleted", "lento"))
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert out["status"] == "applied" and out["cleanup"]["body"] == "done"
    assert store.sources.held_of(M_STR) is None
    gone = I.gone_records(_meta(store, d))
    assert [r["kind"] for r in gone] == [I.SOURCE_GONE], "the hold closed, the gone stays"


def test_registering_a_runs_lines_directly(library):
    store, *_ = library
    host = Host("bridge", max_grant=(S.Place("lento", "home"),))
    body = {"source": {"system": "lento", "instance": "home", "container": "private:U",
                       "id": "m_0020", "through": "m_0023"},
            "revision": "w-9",
            "lines": [{"id": "m_0020", "revision": "e1"}, {"id": "m_0021", "revision": None},
                      "m_0022", {"id": "m_0023", "revision": "e4"}]}
    status, out = run(SC.handle_lines(store, body, host))
    assert (status, out["status"], out["lines"]) == (200, "recorded", 4)
    assert run(SC.handle_lines(store, body, host))[1]["status"] == "known"
    run_rec = {"system": "lento", "instance": "home", "container": "private:U",
               "id": "m_0020", "through": "m_0023", "revision": "w-9"}
    assert store.sources.adopted_revisions(run_rec) == {"m_0020": "e1", "m_0021": None,
                                                        "m_0023": "e4"}
    clash = {**body, "lines": [{"id": "m_0020", "revision": "e2"}, "m_0021", "m_0022",
                               "m_0023"]}
    status, out = run(SC.handle_lines(store, clash, host))
    assert out["status"] == "conflict" and "m_0020" in out["note"]
    for bad in ({**body, "lines": ["m_0021", "m_0023"]},          # does not start at id
                {**body, "source": "lento:home/private:U#m_0020"},  # not a run
                {**body, "lines": ["m_0020", "m_0020", "m_0023"]},  # a line twice
                {**body, "extra": 1}):
        assert run(SC.handle_lines(store, bad, host))[0] == 400, bad
    assert run(SC.handle_lines(store, body, "panel"))[0] == 403
    far = Host("bot", max_grant=(S.Place("telegram"),))
    assert run(SC.handle_lines(store, body, far))[1]["status"] == "forbidden"
