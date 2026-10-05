# -*- coding: utf-8 -*-
"""
tests/test_source_run_delivery.py — one delivery of a run holds one list of lines.

The same run (first and last line) registered twice under the same watermark with other
lines between, or the same lines in another order, is a conflict (`members_differ`) and
nothing is recorded, so the run's members stay as first registered; the same list again
is known; another watermark is a new delivery. A registration with no watermark is a
delivery of its own: two of them for one run may not disagree, and neither binds a
watermarked one. Both roads that register lines — POST /api/v2/source/lines and a slicing
batch — surface the word.
"""

import asyncio
import json

import pytest

from core import _slicer as SL
from core import _source_change as SC
from core import _sources as S
from core.bucket_manager import BucketManager
from core.scope import Host

WHERE = {"system": "lento", "instance": "home", "container": "private:U"}
RUN = "lento:home/private:U#m_1..m_3"


def run(coro):
    return asyncio.run(coro)


def test_one_delivery_of_a_run_given_other_lines_is_a_conflict(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    assert reg.record_order(WHERE, ["m_1", "m_2", "m_3"], revision="w-1") == S.RECORDED
    assert reg.record_order(WHERE, ["m_1", "m_3"], revision="w-1") == S.MEMBERS_DIFFER
    assert reg.record_order(WHERE, ["m_1", "m_2", "m_9", "m_3"], revision="w-1") \
        == S.MEMBERS_DIFFER
    assert reg.members_of(RUN) == ["m_1", "m_2", "m_3"], "nothing was recorded"
    assert len(S._read_lines(reg.orders_path)) == 1
    assert reg.record_order(WHERE, ["m_1", "m_2", "m_3"], revision="w-1") == S.KNOWN
    # Another watermark is another delivery: joined as before.
    assert reg.record_order(WHERE, ["m_1", "m_9", "m_3"], revision="w-2") == S.RECORDED
    assert reg.members_of(RUN) == ["m_1", "m_2", "m_3", "m_9"]


def test_the_same_lines_in_another_order_disagree(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    reg.record_order(WHERE, ["m_1", "m_2", "m_4", "m_3"], revision="w-1")
    assert reg.record_order(WHERE, ["m_1", "m_4", "m_2", "m_3"], revision="w-1") \
        == S.MEMBERS_DIFFER


def test_a_registration_without_a_watermark_is_a_delivery_of_its_own(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    assert reg.record_order(WHERE, ["m_1", "m_2", "m_3"]) == S.RECORDED
    assert reg.record_order(WHERE, ["m_1", "m_3"]) == S.MEMBERS_DIFFER
    assert reg.record_order(WHERE, ["m_1", "m_2", "m_3"]) == S.KNOWN
    # Independent of a watermarked delivery, either way round.
    assert reg.record_order(WHERE, ["m_1", "m_3"], revision="w-1") == S.RECORDED
    other = S.SourceRegistry(tmp_path / "other")
    other.record_order(WHERE, ["m_1", "m_3"], revision="w-1")
    assert other.record_order(WHERE, ["m_1", "m_2", "m_3"]) == S.RECORDED


def test_a_different_run_of_the_same_container_does_not_disagree(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    reg.record_order(WHERE, ["m_1", "m_2", "m_3"], revision="w-1")
    assert reg.record_order(WHERE, ["m_1", "m_2"], revision="w-1") == S.RECORDED
    assert reg.record_order(WHERE, ["m_2", "m_3", "m_4"], revision="w-1") == S.RECORDED


@pytest.fixture
def store(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return store


def test_the_lines_route_answers_conflict_members_differ(store):
    host = Host("bridge", max_grant=(S.Place("lento", "home"),))
    body = {"source": {**WHERE, "id": "m_1", "through": "m_3"}, "revision": "w-1",
            "lines": ["m_1", "m_2", "m_3"]}
    assert run(SC.handle_lines(store, body, host))[1]["status"] == "recorded"
    status, out = run(SC.handle_lines(store, {**body, "lines": ["m_1", "m_3"]}, host))
    assert (status, out["status"], out["note"]) == (200, "conflict", "members_differ")
    assert store.sources.members_of(RUN) == ["m_1", "m_2", "m_3"]
    assert run(SC.handle_lines(store, body, host))[1]["status"] == "known"


def test_a_slicing_batch_giving_one_delivery_other_lines_is_refused(store):
    async def side_model(system, user):
        return json.dumps({"slices": []})

    def batch(ids):
        return {"source": WHERE, "day": "2026-10-01", "revision": "w-1",
                "lines": [{"id": i, "text": f"line {i}"} for i in ids]}
    run(SL.take_batch(store, batch(["m_1", "m_2", "m_3"]), model=side_model))
    with pytest.raises(SL.BatchError, match="members_differ"):
        run(SL.take_batch(store, batch(["m_1", "m_3"]), model=side_model))
    assert store.sources.members_of(RUN) == ["m_1", "m_2", "m_3"]
