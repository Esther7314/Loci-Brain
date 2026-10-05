# -*- coding: utf-8 -*-
"""
tests/test_source_hold_per_line.py — a hold on a run is settled line by line, never whole
by one line, and a change for a run never undoes a line's own change.

A host serving an original says a run is gone; the run is held. The authority's ordered
word for one line settles the hold for that line only: the line reads its own state, the
other lines, the runs over them and the run itself stay held, and only the memories resting
on that line alone (and what is derived only from them) are freed. The run is free once its
own change arrives or every line it holds has been settled. A settling change applied
before the hold settles nothing; a run whose lines are unknown is settled only by its own
change. Restoring a run leaves a line's own withdrawal and a line's own hold in place.
"""

import asyncio

import frontmatter
import pytest

from core import _invalidation as I
from core import _source_change as SC
from core import _sources as S
from core import visibility as V
from core.bucket_manager import BucketManager
from core.scope import Host
from utils import WAS_DERIVED_FROM

WHERE = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
PFX = "telegram:bot-a/group:G#"
RUN = PFX + "m_1..m_4"
HOST = Host("bot", scope_mode="open")
RESTORER = Host("bot", scope_mode="open", may_restore=True)


def run(coro):
    return asyncio.run(coro)


def _registry(tmp_path, lines=True):
    reg = S.SourceRegistry(tmp_path)
    if lines:
        reg.record_order(WHERE, ["m_1", "m_2", "m_3", "m_4", "m_5"])
    return reg


def _apply(reg, cid, source, kind, seq=1, may_restore=False):
    out = run(reg.apply_change({"change_id": cid, "source": source, "kind": kind,
                                "host_seq": seq}, may_restore=may_restore, host="bot"))
    assert out["outcome"] in (S.APPLIED, S.UNKNOWN_SOURCE), out
    return out


# ---------- the registry ----------

def test_one_lines_restore_settles_the_run_hold_for_that_line_only(tmp_path):
    reg = _registry(tmp_path)
    reg.hold(RUN, "withdrawn", "bot")
    _apply(reg, "r2", PFX + "m_2", "restored")
    assert reg.state_of(PFX + "m_2") == S.ACTIVE, "the settled line reads its own state"
    assert reg.held_over(PFX + "m_2") == []
    for other in ("m_1", "m_3", "m_4", "m_3..m_4", "m_2..m_3", "m_1..m_4"):
        assert reg.state_of(PFX + other) == S.HELD, other
    assert reg.held_of(RUN) is not None, "the run's hold stays open"
    assert reg.held_over(PFX + "m_3") == [RUN]
    assert reg.state_of(PFX + "m_5") == S.ACTIVE, "outside the run"


def test_a_run_hold_closes_once_every_line_is_settled_or_the_run_itself(tmp_path):
    reg = _registry(tmp_path)
    reg.hold(RUN, "deleted", "bot")
    for line in ("m_1", "m_2", "m_3"):
        _apply(reg, f"r-{line}", PFX + line, "restored")
        assert reg.held_of(RUN) is not None, line
        assert reg.state_of(RUN) == S.HELD
    _apply(reg, "r-m_4", PFX + "m_4", "restored")
    assert reg.held_of(RUN) is None and reg.state_of(RUN) == S.ACTIVE

    other = tmp_path / "other"
    reg2 = _registry(other)
    reg2.hold(RUN, "withdrawn", "bot")
    _apply(reg2, "r-run", RUN, "restored")
    assert reg2.held_of(RUN) is None
    assert all(reg2.state_of(PFX + x) == S.ACTIVE for x in ("m_1", "m_2", "m_3", "m_4"))


def test_a_restored_sub_run_of_registered_lines_settles_the_lines_it_covers(tmp_path):
    reg = _registry(tmp_path)
    reg.hold(RUN, "withdrawn", "bot")
    _apply(reg, "r23", PFX + "m_2..m_3", "restored")
    assert reg.state_of(PFX + "m_2") == S.ACTIVE and reg.state_of(PFX + "m_3") == S.ACTIVE
    assert reg.held_over(PFX + "m_2") == [] and reg.held_over(PFX + "m_2..m_3") == []
    assert reg.state_of(PFX + "m_1") == S.HELD and reg.state_of(PFX + "m_4") == S.HELD
    assert reg.state_of(RUN) == S.HELD and reg.held_of(RUN) is not None
    # The rest of the lines covered by another registered run: the hold is settled.
    _apply(reg, "r14", PFX + "m_4..m_5", "restored")
    assert reg.held_of(RUN) is not None, "m_1 is not covered yet"
    _apply(reg, "r1", PFX + "m_1", "restored")
    assert reg.held_of(RUN) is None and reg.state_of(RUN) == S.ACTIVE


def test_a_restored_run_with_unknown_lines_settles_nothing_of_a_held_run(tmp_path):
    reg = _registry(tmp_path)
    reg.hold(RUN, "withdrawn", "bot")
    # m_9 is in no registration: the run's lines are unknown, it covers nothing but itself.
    assert reg.members_of(PFX + "m_2..m_9") is None
    _apply(reg, "r29", PFX + "m_2..m_9", "restored")
    assert reg.state_of(PFX + "m_2") == S.HELD
    assert reg.held_over(PFX + "m_2") == [RUN]
    assert reg.held_of(RUN) is not None


def test_a_line_withdrawn_under_a_held_run_reads_withdrawn_and_the_rest_stays_held(tmp_path):
    reg = _registry(tmp_path)
    reg.hold(RUN, "withdrawn", "bot")
    _apply(reg, "w2", PFX + "m_2", "withdrawn")
    assert reg.state_of(PFX + "m_2") == S.WITHDRAWN
    assert reg.state_of(RUN) == S.WITHDRAWN, "a run is as bad as its worst line"
    assert reg.state_of(PFX + "m_3") == S.HELD
    assert reg.state_of(PFX + "m_3..m_4") == S.HELD


def test_a_settling_change_applied_before_the_hold_settles_nothing(tmp_path):
    reg = _registry(tmp_path)
    _apply(reg, "r2", PFX + "m_2", "restored")
    _apply(reg, "r-run", RUN, "restored")
    reg.hold(RUN, "withdrawn", "bot")
    assert reg.held_of(RUN) is not None
    assert reg.state_of(PFX + "m_2") == S.HELD
    assert reg.held_over(PFX + "m_2") == [RUN]


def test_a_held_run_with_unknown_lines_is_settled_only_by_its_own_change(tmp_path):
    reg = _registry(tmp_path, lines=False)
    reg.hold(RUN, "withdrawn", "bot")
    _apply(reg, "r1", PFX + "m_1", "restored")
    _apply(reg, "r4", PFX + "m_4", "restored")
    assert reg.held_of(RUN) is not None, "its first and last line settle nothing of it"
    assert reg.state_of(RUN) == S.HELD
    assert reg.state_of(PFX + "m_1") == S.ACTIVE, "a run with unknown lines reaches no line"
    _apply(reg, "r-run", RUN, "restored")
    assert reg.held_of(RUN) is None and reg.state_of(RUN) == S.ACTIVE


def test_restoring_a_run_leaves_a_lines_own_withdrawal(tmp_path):
    reg = _registry(tmp_path)
    _apply(reg, "w2", PFX + "m_2", "withdrawn")
    _apply(reg, "w-run", RUN, "withdrawn")
    assert all(reg.state_of(PFX + x) == S.WITHDRAWN for x in ("m_1", "m_2", "m_3", "m_4"))
    _apply(reg, "r-run", RUN, "restored", seq=2, may_restore=True)
    assert reg.state_of(PFX + "m_2") == S.WITHDRAWN, "its own block stays"
    assert [reg.state_of(PFX + x) for x in ("m_1", "m_3", "m_4")] == [S.ACTIVE] * 3
    assert reg.state_of(RUN) == S.WITHDRAWN, "the run holds a withdrawn line"
    assert reg.state_of(PFX + "m_3..m_4") == S.ACTIVE


def test_restoring_a_run_does_not_settle_a_hold_on_one_of_its_lines(tmp_path):
    reg = _registry(tmp_path)
    reg.hold(PFX + "m_2", "withdrawn", "bot")
    _apply(reg, "r-run", RUN, "restored")
    assert reg.held_of(PFX + "m_2") is not None
    assert reg.state_of(PFX + "m_2") == S.HELD
    assert reg.state_of(RUN) == S.HELD, "the run holds a held line"
    assert reg.state_of(PFX + "m_1") == S.ACTIVE
    _apply(reg, "r2", PFX + "m_2", "restored")
    assert reg.state_of(PFX + "m_2") == S.ACTIVE and reg.state_of(RUN) == S.ACTIVE


# ---------- the memories ----------

@pytest.fixture
def library(tmp_path, monkeypatch):
    from tools import _runtime as rt
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    store.sources.record_order(WHERE, ["m_1", "m_2", "m_3", "m_4", "m_5"])
    ids = {
        "on_m2": run(store.create("第二行说的事。", sources=[{**WHERE, "id": "m_2"}])),
        "on_m3": run(store.create("第三行说的事。", sources=[{**WHERE, "id": "m_3"}])),
        "on_run": run(store.create("整段说的事。",
                                   sources=[{**WHERE, "id": "m_1", "through": "m_4"}])),
    }
    ids["from_m2"] = run(store.create("从第二行想到的。",
                                      prov=[{"rel": WAS_DERIVED_FROM, "target": ids["on_m2"]}]))
    ids["from_run"] = run(store.create("从整段想到的。",
                                       prov=[{"rel": WAS_DERIVED_FROM, "target": ids["on_run"]}]))
    return store, ids


def _meta(store, bid) -> dict:
    return dict(frontmatter.load(store._find_bucket_file(bid)).metadata)


def _held(store, bid) -> bool:
    return bool(I.open_records(_meta(store, bid), I.SOURCE_HELD))


def _change(cid, source, kind, seq=1):
    return {"change_id": cid, "source": source, "host_seq": seq, "change": kind}


def test_one_lines_ordered_word_frees_only_what_rests_on_that_line(library):
    store, ids = library
    held = run(SC.hold(store, RUN, "withdrawn", "bot"))
    assert set(held) == set(ids.values())
    _s, out = run(SC.handle(store, _change("r2", PFX + "m_2", "restored"), HOST))
    assert out["status"] == "applied", out
    assert not _held(store, ids["on_m2"]) and not _held(store, ids["from_m2"])
    for still in ("on_m3", "on_run", "from_run"):
        assert _held(store, ids[still]), still
    assert V.visible_for(_meta(store, ids["on_m2"]), road=V.READ)
    assert not V.visible_for(_meta(store, ids["on_run"]), road=V.READ)

    # The host says the run is gone again while its hold is still open: the line settled
    # on its own stays free.
    again = run(SC.hold(store, RUN, "withdrawn", "bot"))
    assert ids["on_m2"] not in again and ids["from_m2"] not in again
    assert not _held(store, ids["on_m2"])

    for line in ("m_1", "m_3", "m_4"):
        run(SC.handle(store, _change(f"r-{line}", PFX + line, "restored"), HOST))
        if line != "m_4":
            assert _held(store, ids["on_run"]), line
    assert store.sources.held_of(RUN) is None
    assert not any(_held(store, b) for b in ids.values())


def test_the_runs_own_word_frees_everything_held_by_it(library):
    store, ids = library
    run(SC.hold(store, RUN, "deleted", "bot"))
    _s, out = run(SC.handle(store, _change("r", RUN, "restored"), HOST))
    assert out["status"] == "applied" and out["reach"] == SC.REACH_LINES
    assert not any(_held(store, b) for b in ids.values())


def test_restoring_a_run_leaves_a_lines_own_block_on_what_rests_on_it(library):
    store, ids = library
    run(SC.handle(store, _change("w2", PFX + "m_2", "withdrawn"), RESTORER))
    run(SC.handle(store, _change("w-run", RUN, "withdrawn"), RESTORER))
    _s, out = run(SC.handle(store, _change("r-run", RUN, "restored", seq=2), RESTORER))
    assert out["status"] == "applied" and store.sources.state_of(PFX + "m_2") == S.WITHDRAWN
    meta = _meta(store, ids["from_m2"])
    own = [r for r in I.open_records(meta, I.SOURCE_GONE) if r.get("of") == PFX + "m_2"]
    assert own, "the line's own block on what is derived from it stays"
    assert not V.visible_for(meta, road=V.READ)
