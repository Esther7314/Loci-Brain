# -*- coding: utf-8 -*-
"""
tests/test_source_change_repairs.py — a source change carried on, raced, or sent for a run.

A resend finishes the places its first run left pending on the memories that first run
found, never on memories written after a later change gave the source back; a restore's
review is written however the first run ended; two sends of one change at once run once;
a change for a run, and a registration of a run's lines, come only from the host that is
the change authority for every line of it.
"""

import asyncio
import threading

import pytest

from core import _invalidation as I
from core import _slicer as SL
from core import _source_change as SC
from core import _sources as S
from core import visibility as V
from core.scope import Host

from test_source_change import (M, M_STR, OPEN_HOST, PHRASE, _hosts, _meta, change,  # noqa: F401
                                library, run)


def _flaky(monkeypatch, place: str):
    """Make one place fail on its first run only."""
    real = SC._RUN[place]
    calls = {"n": 0}

    def once(s, ctx):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("database is locked")
        return real(s, ctx)
    monkeypatch.setitem(SC._RUN, place, once)


def test_a_resend_after_a_restore_never_clears_what_was_written_since(library, monkeypatch):
    store, e, d, _root = library
    _flaky(monkeypatch, "embeddings")
    _s, first = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert first["cleanup"]["embeddings"] == "pending" and first["entries"] == [e]
    _s, back = run(SC.handle(store, change("c-2", "restored", 2), OPEN_HOST))
    assert back["status"] == "applied" and back["state"] == "active"
    # The source may be used again: the host delivers it once more.
    fresh = run(store.create("小周又说了一遍：周六去海边。", sources=[M], room="EVENT/WORLD"))
    _s, again = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert again["status"] == "applied" and again["entries"] == [e], again
    assert all(v in ("done", "none") for v in again["cleanup"].values()), again["cleanup"]
    meta = _meta(store, fresh)
    assert not V.source_gone(meta) and not I.open_records(meta)
    assert run(store.get(fresh))["content"].startswith("小周又说了一遍")
    assert store.sources.state_of(M_STR) == "active"


def test_a_resend_after_a_restore_does_not_start_clearing_what_it_never_blocked(
        library, monkeypatch):
    store, e, d, _root = library
    real = store.add_invalidation_record
    calls = {"n": 0}

    async def crash_once(bid, rec):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk went away")
        return await real(bid, rec)
    monkeypatch.setattr(store, "add_invalidation_record", crash_once)
    with pytest.raises(OSError):
        run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    _s, back = run(SC.handle(store, change("c-2", "restored", 2), OPEN_HOST))
    assert back["status"] == "applied" and store.sources.state_of(M_STR) == "active"
    _s, again = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert again["entries"] == [] and again["derived_pending"] == [], again
    for bid in (e, d):
        assert not V.source_gone(_meta(store, bid)), bid
    assert PHRASE in run(store.get_including_archive(e))["content"]


def test_a_restore_whose_review_broke_off_writes_it_on_the_resend(library, monkeypatch):
    store, e, d, _root = library
    run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    real = SC._await_review
    calls = {"n": 0}

    async def crash_once(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("disk went away")
        return await real(*a, **kw)
    monkeypatch.setattr(SC, "_await_review", crash_once)
    with pytest.raises(OSError):
        run(SC.handle(store, change("c-2", "restored", 2), OPEN_HOST))
    _s, again = run(SC.handle(store, change("c-2", "restored", 2), OPEN_HOST))
    assert again["status"] == "applied" and again["derived_pending"] == [d], again
    meta = _meta(store, d)
    assert V.source_restored(meta) and not V.source_gone(meta)
    _s, third = run(SC.handle(store, change("c-2", "restored", 2), OPEN_HOST))
    assert third["status"] == "duplicate" and third["derived_pending"] == [d]


def test_two_sends_of_one_change_at_once_run_it_once(library, monkeypatch):
    store, e, _d, _root = library
    real = SC._derived

    async def slow(*a, **kw):
        await asyncio.sleep(0.4)
        return await real(*a, **kw)
    monkeypatch.setattr(SC, "_derived", slow)
    outs = []

    def send():
        outs.append(run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))[1])
    threads = [threading.Thread(target=send) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    kinds = [x["event_type"] for x in store.ledger_mirror.iter_events()]
    assert kinds.count("SourceChanged") == 1, kinds
    assert sorted(o["status"] for o in outs) == ["applied", "duplicate"], outs
    assert outs[0]["applied_seq"] == outs[1]["applied_seq"]


def test_a_generic_tag_is_not_the_entrys_words(tmp_path, monkeypatch):
    from core import _dream
    from core.bucket_manager import BucketManager
    from core import runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    generic = "日常生活记录"
    e = run(store.create(f"小周说{PHRASE}。", sources=[M], room="EVENT/WORLD",
                         tags=[generic], subjects=["每天都在发生的事"]))
    other = run(store.create("别的一天。", room="EVENT/WORLD"))
    _dream.save_record({"id": "d00000000003", "完整": f"梦里也是{generic}",
                        "碎片": "x", "素材": {"压在心头": [other], "想不明白": []}},
                       str(tmp_path))
    store.usage.record("found", [other], "recall.search", query=generic)
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), OPEN_HOST))
    assert out["entries"] == [e]
    assert [r["id"] for r in _dream.load_dreams(str(tmp_path))] == ["d00000000003"]
    assert [r.get("query") for r in store.usage.read()] == [generic]


# ───────────────────── a run's change and its lines come from its authority ─────────────────────

def _run_library(store):
    where = {"system": "lento", "instance": "home", "container": "c"}
    store.sources.record_order(where, [f"m_{i}" for i in range(1, 6)])
    return run(store.create("一段话。", sources=[{**where, "id": "m_1", "through": "m_5"}]))


def test_a_run_change_needs_the_authority_of_every_line_in_it(library):
    store, *_ = library
    _run_library(store)
    hosts = _hosts(a={"max_grant": [{"system": "lento"}], "may_restore": True,
                      "authority": [{"system": "lento", "instance": "home", "container": "c"}]},
                   b={"max_grant": [{"system": "lento"}], "may_restore": True,
                      "authority": [{"system": "lento", "instance": "home", "container": "c",
                                     "id": "m_3"}]})
    a, b = hosts.get("a"), hosts.get("b")
    _s, out = run(SC.handle(store, change("a-1", "withdrawn", 1,
                                          source="lento:home/c#m_1..m_5"), a, hosts=hosts))
    assert (out["status"], out["note"]) == ("forbidden", SC.NOT_AUTHORITY), out
    assert store.sources.state_of("lento:home/c#m_3") == "active", "b's line is untouched"
    # Its own lines one by one, and a run holding only its own lines, are a's.
    _s, out = run(SC.handle(store, change("a-2", "withdrawn", 2,
                                          source="lento:home/c#m_1"), a, hosts=hosts))
    assert out["status"] == "applied"
    store.sources.record_order({"system": "lento", "instance": "home", "container": "c"},
                               ["m_4", "m_5"])
    _s, out = run(SC.handle(store, change("a-3", "withdrawn", 1,
                                          source="lento:home/c#m_4..m_5"), a, hosts=hosts))
    assert out["status"] == "applied", out
    # b's own line is b's.
    _s, out = run(SC.handle(store, change("b-1", "withdrawn", 1,
                                          source="lento:home/c#m_3"), b, hosts=hosts))
    assert out["status"] == "applied"


def test_registering_lines_is_refused_past_another_hosts_authority(library):
    store, *_ = library
    hosts = _hosts(a={"max_grant": [{"system": "lento"}],
                      "authority": [{"system": "lento", "instance": "home"}]},
                   c={"max_grant": [{"system": "lento"}]})
    a, c = hosts.get("a"), hosts.get("c")
    body = {"source": "lento:home/c#m_1..m_3", "revision": None,
            "lines": ["m_1", "m_2", "m_3"]}
    status, out = run(SC.handle_lines(store, body, c, hosts=hosts))
    assert (status, out["status"], out["note"]) == (200, "forbidden", SC.NOT_AUTHORITY), out
    assert store.sources.members_of("lento:home/c#m_1..m_3") is None
    status, out = run(SC.handle_lines(store, body, a, hosts=hosts))
    assert out["status"] == "recorded"
    # A slicing batch registers lines too: the same rule, refused before the side model.
    called = []

    async def side_model(system, user):
        called.append(1)
        return '{"slices": []}'
    batch = {"source": {"system": "lento", "instance": "home", "container": "d"},
             "day": "2026-10-01", "lines": [{"id": "m_1", "text": "x"}, {"id": "m_2", "text": "y"}]}
    with pytest.raises(SL.BatchForbidden) as got:
        run(SL.take_batch(store, batch, model=side_model, host=c, hosts=hosts))
    assert SC.NOT_AUTHORITY in str(got.value) and called == []
    # Nobody declared: a host within its ceiling is still not the one a line list comes from.
    free = {"source": "lento:work/c#m_1..m_2", "revision": None, "lines": ["m_1", "m_2"]}
    _s, out = run(SC.handle_lines(store, free, c, hosts=hosts))
    assert (out["status"], out["note"]) == ("forbidden", SC.NOT_REGISTRAR), out


def test_a_slicing_batch_refuses_lines_that_are_withdrawn_or_held(library):
    store, *_ = library
    where = {"system": "lento", "instance": "home", "container": "g"}
    run(store.sources.apply_change({"change_id": "w", "source": "lento:home/g#m_2",
                                    "kind": "withdrawn", "host_seq": 1}))
    store.sources.hold("lento:home/g#m_5", "deleted", "x")
    before = store.slices.pending_count()
    called = []

    async def side_model(system, user):
        called.append(user)
        return '{"slices": [{"from": 1, "to": 2, "gist": "g"}]}'
    for bad in ("m_2", "m_5"):
        batch = {"source": where, "day": "2026-10-01",
                 "lines": [{"id": "m_1", "text": "x"}, {"id": bad, "text": "secret"}]}
        with pytest.raises(SL.BatchError) as got:
            run(SL.take_batch(store, batch, model=side_model))
        assert bad in str(got.value)
    assert called == [], "the text never reached the side model"
    assert store.slices.pending_count() == before
