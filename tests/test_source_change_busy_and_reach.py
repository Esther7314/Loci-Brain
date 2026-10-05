# -*- coding: utf-8 -*-
"""
tests/test_source_change_busy_and_reach.py — a second send of a running change answers
`in_progress` after a short wait; a change for a run says what it reached.

While one send of a change holds its lease, another send of the same change_id waits a
few seconds, then answers 200 `in_progress` (with the progress saved so far, when there is
any) and does nothing; resent after the lease is free it is `applied` or `duplicate` as
always. A timeout inside the work itself is not mistaken for a busy lease. A change for a
run whose lines were never registered reaches only what names the run, and its receipt
and ledger line say so (`reach: run_only`, `note: members_unknown`); one whose lines are
registered says `reach: lines`; a change for a single piece has no `reach`.
"""

import asyncio
import time

import pytest

from core import _ledger as L
from core import _source_change as SC
from core import _sources as S
from core.bucket_manager import BucketManager, _filesystem_turn
from core.scope import Host

M = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003"}
M_STR = "lento:home/private:U#m_0003"
HOST = Host("life", scope_mode="open", may_restore=True)


def run(coro):
    return asyncio.run(coro)


def change(cid, kind, seq, source=M_STR):
    return {"change_id": cid, "source": source, "host_seq": seq, "change": kind}


@pytest.fixture
def store(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return store


async def _while_leased(store, cid, body):
    """Send `body` while this test holds the lease of (HOST, cid), as a first send still
    running would. Returns (seconds it took, status, reply)."""
    key = S.SourceRegistry.change_key(HOST.name, cid)
    async with _filesystem_turn(store.base_dir, f"source-change-{key}"):
        t0 = time.monotonic()
        status, out = await SC.handle(store, body, HOST)
        return time.monotonic() - t0, status, out


def test_the_wait_is_short_and_named():
    assert 1.0 <= SC._CHANGE_WAIT_SECONDS <= 30.0


def test_a_second_send_answers_in_progress_and_does_nothing(store, monkeypatch):
    e = run(store.create("小周说周六去海边。", sources=[M]))
    monkeypatch.setattr(SC, "_CHANGE_WAIT_SECONDS", 0.5)
    took, status, out = run(_while_leased(store, "c-1", change("c-1", "withdrawn", 1)))
    assert took < 5.0
    assert status == 200 and out == {"change_id": "c-1", "status": "in_progress",
                                     "source": M_STR, "note": "retry_same_change_id"}
    assert store.sources.prior_change(HOST.name, "c-1") is None, "nothing was applied"
    assert store.sources.state_of(M_STR) == S.ACTIVE
    assert run(store.get(e))["content"].strip() != store.CLEARED_BODY
    # The lease is free: the resend does the work, the next one is a duplicate.
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), HOST))
    assert out["status"] == "applied" and out["entries"] == [e]
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), HOST))
    assert out["status"] == "duplicate"


def test_in_progress_shows_the_progress_saved_so_far(store, monkeypatch):
    e = run(store.create("小周说周六去海边。", sources=[M]))
    first = run(SC.handle(store, change("c-1", "withdrawn", 1), HOST))[1]
    monkeypatch.setattr(SC, "_CHANGE_WAIT_SECONDS", 0.5)
    _t, status, out = run(_while_leased(store, "c-1", change("c-1", "withdrawn", 1)))
    assert status == 200 and out["status"] == "in_progress"
    assert out["note"] == "retry_same_change_id" and out["source"] == M_STR
    assert out["state"] == "withdrawn" and out["entries"] == [e]
    assert out["applied_seq"] == first["applied_seq"]
    assert out["cleanup"] == first["cleanup"]
    assert "reach" not in out, "a piece has no reach"


def test_in_progress_hides_the_ledger_number_from_a_host_with_a_ceiling(store, monkeypatch):
    bridge = Host("bridge", max_grant=(S.Place("lento"),))
    run(store.create("小周说周六去海边。", sources=[M]))
    run(SC.handle(store, change("c-1", "withdrawn", 1), bridge))
    monkeypatch.setattr(SC, "_CHANGE_WAIT_SECONDS", 0.5)
    key = S.SourceRegistry.change_key(bridge.name, "c-1")

    async def go():
        async with _filesystem_turn(store.base_dir, f"source-change-{key}"):
            return await SC.handle(store, change("c-1", "withdrawn", 1), bridge)
    _s, out = run(go())
    assert out["status"] == "in_progress"
    assert "applied_seq" not in out and out["applied_cursor"].startswith("c1")


def test_a_timeout_inside_the_work_is_not_taken_for_a_busy_lease(store, monkeypatch):
    async def stuck(*_a, **_k):
        raise TimeoutError("the registry's own lease")
    monkeypatch.setattr(store.sources, "apply_change", stuck)
    with pytest.raises(TimeoutError):
        run(SC.handle(store, change("c-1", "withdrawn", 1), HOST))


def _changed_line(store, cid) -> dict:
    lines = [x for x in store.ledger_mirror.iter_since(0)
             if x["event_type"] == L.SOURCE_CHANGED and x["payload"].get("change_id") == cid]
    assert len(lines) == 1
    return lines[0]["payload"]


def test_a_change_for_a_run_with_unknown_lines_says_it_reached_the_run_only(store):
    line_memory = run(store.create("第一行说的事。", sources=[M]))
    run_str = "lento:home/private:U#m_0003..m_0009"
    _s, out = run(SC.handle(store, change("c-run", "withdrawn", 1, source=run_str), HOST))
    assert out["reach"] == SC.REACH_RUN_ONLY and out["note"] == SC.MEMBERS_UNKNOWN
    assert line_memory not in out["entries"], "nothing resting on a line was reached"
    assert _changed_line(store, "c-run")["note"] == SC.MEMBERS_UNKNOWN
    public = L.public_row({"event_type": L.SOURCE_CHANGED, "seq": 1,
                           "payload": _changed_line(store, "c-run")})
    assert public["note"] == SC.MEMBERS_UNKNOWN, "/changes shows what the change reached"
    assert L.redact_line({"event_type": L.SOURCE_CHANGED,
                          "payload": _changed_line(store, "c-run")}) is None, \
        "the ledger line holds only what a source line may hold"
    # Registering the lines afterwards does not change what that change reached.
    store.sources.record_order({k: M[k] for k in ("system", "instance", "container")},
                               [f"m_000{i}" for i in range(3, 10)])
    _s, again = run(SC.handle(store, change("c-run", "withdrawn", 1, source=run_str), HOST))
    assert again["status"] == "duplicate" and again["reach"] == SC.REACH_RUN_ONLY
    assert again["note"] == SC.MEMBERS_UNKNOWN


def test_a_change_for_a_run_with_registered_lines_says_it_reached_them(store):
    where = {k: M[k] for k in ("system", "instance", "container")}
    store.sources.record_order(where, [f"m_000{i}" for i in range(3, 10)])
    line_memory = run(store.create("第一行说的事。", sources=[M]))
    run_str = "lento:home/private:U#m_0003..m_0009"
    _s, out = run(SC.handle(store, change("c-run", "withdrawn", 1, source=run_str), HOST))
    assert out["reach"] == SC.REACH_LINES and "note" not in out
    assert out["entries"] == [line_memory]
    assert "note" not in _changed_line(store, "c-run")


def test_a_change_for_a_piece_has_no_reach(store):
    run(store.create("小周说周六去海边。", sources=[M]))
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), HOST))
    assert out["status"] == "applied" and "reach" not in out and "note" not in out
    _s, out = run(SC.handle(store, change("c-1", "withdrawn", 1), HOST))
    assert out["status"] == "duplicate" and "reach" not in out
