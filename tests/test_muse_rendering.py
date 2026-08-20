# -*- coding: utf-8 -*-
"""Unit tests for the two functions that turn muse's findings into something readable.

WHY THIS FILE EXISTS — it closes a hole that was proved to be open
    During the English rename, every field of `Cluster` and `Finger` was renamed. There
    was a window in which `_muse.py` had the new names and the two files that READ those
    fields still had the old ones. In that window the whole fast lane was green: pyflakes
    clean, every unit test passing, gateway passing, the English ratchet passing — while
    `muse()` and `/api/muse/pending` would have raised `AttributeError` on the first call.

    Two separate blind spots lined up to produce that green:

      · pyflakes cannot see a misspelled attribute. `x.架v` on an object that no longer
        has it is perfectly valid Python until it runs.
      · nothing in `tests/` had ever constructed a `Cluster` or a `Finger`, so no test
        ever touched a single one of those fields.

    A test that builds one of each and renders it would have caught it in about a second.
    This is that test. Its value is not the assertions about wording — it is that every
    field of both dataclasses gets READ by something that runs.

WHAT IS DELIBERATELY NOT TESTED
    Whether the clustering is any good, and whether the thresholds are set right. Those
    are judgements, they move, and freezing them here would freeze an opinion. What is
    frozen is the shape: every piece of evidence that was gathered is shown, and the one
    case where a finger must stay silent stays silent.
"""
from datetime import datetime

import pytest

from core import _muse as M
from tools import muse as R


def an_item(bid="a1b2c3d4e5f6", room="MIND/VIEWS", text="a thought", **over):
    kw = dict(id=bid, room=room, ts=datetime(2026, 8, 20), created=datetime(2026, 8, 20),
              v=0.7, a=0.4, tags=["tag"], text=text)
    kw.update(over)
    return M.Item(**kw)


def a_cluster(**over):
    kw = dict(ids=["a1", "b2"], items=[an_item("a1"), an_item("b2")],
              shelf_v=0.72, shelf_a=0.41)
    kw.update(over)
    return M.Cluster(**kw)


def a_finger(**over):
    kw = dict(name="词爆发", ids=["e1", "e2"],
              items=[an_item("e1", room="EVENT/SELF", text="something happened"),
                     an_item("e2", room="EVENT/SELF", text="then this")],
              start=datetime(2026, 8, 1), end=datetime(2026, 8, 5),
              evidence="the word appeared 9 times inside this stretch and twice outside it",
              next_step='fold(when="2026-08-01..2026-08-05")')
    kw.update(over)
    return M.Finger(**kw)


CFG = {"max_clusters": 5, "max_fingers": 3}
STATS = {"mind": {"团": 2}, "event": {"主线": 1}}


# ───────────────── every field is read by something that runs ─────────────────

def test_the_evidence_line_reads_the_shelf_coordinates():
    # Criterion: the shelf coordinate is the evidence that these were grouped by feeling
    # rather than by a vector's opinion. Dropping it would leave the reader unable to tell
    # a deliberate grouping from a lucky one.
    line = R._mind_evidence(a_cluster(shelf_v=0.72, shelf_a=0.41))
    assert "0.72" in line and "0.41" in line


def test_a_from_chain_is_reported_with_its_shared_ancestor():
    # Criterion: "these grew out of the same events" is the hardest evidence this tool
    # has — it is a link a person made by hand, not a similarity score. It has to be
    # distinguishable from the soft kind at a glance.
    line = R._mind_evidence(a_cluster(from_core=["a1", "b2"], shared_from=["src1", "src2"]))
    assert "from" in line
    assert "src1"[:6] in line


def test_semantic_additions_are_reported_separately_with_their_floor():
    # Criterion: vector similarity is a first pass, and anything it brought in is marked
    # as such. Reporting the floor as well means the reader can see how weak the weakest
    # member is instead of taking the grouping on trust.
    line = R._mind_evidence(a_cluster(semantic_add=["c3"], min_sim=0.63))
    assert "0.63" in line


def test_a_cluster_with_no_hand_made_link_says_so_out_loud():
    # Criterion: THE assertion of this section. A group held together only by a vector is
    # the weakest kind, and it must announce itself — otherwise it reads exactly like a
    # group held together by links someone actually made.
    line = R._mind_evidence(a_cluster())
    assert "from" in line or "语义" in line
    assert R._mind_evidence(a_cluster(from_core=["a1"])) != line


# ───────────────── the first screen ─────────────────

def test_step_one_renders_clusters_and_fingers_together():
    clusters, shown, extra, everything = R.layout([a_cluster()], {"词爆发": [a_finger()]},
                                                  STATS, CFG)
    text = R.step_one(clusters, scattered=3, default_coords=0, extra_clusters=extra,
                      shown_fingers=shown, era_n=1)
    assert "[1]" in text and "[2]" in text
    assert "the word appeared 9 times" in text
    assert "3" in text


def test_the_blank_ledger_finger_stays_silent_when_no_period_exists_yet():
    # Criterion: this one is a rule, not a preference. "Which stretches have no name yet"
    # is a question about a map — and with no periods recorded at all there is no map, so
    # the honest answer is not "the whole year is blank". A tool that answered that way
    # would be inventing an absence out of a system that has simply not been used yet.
    clusters, shown, extra, _ = R.layout([], {"空白记账": [a_finger(name="空白记账")]},
                                         {"mind": {"团": 0}, "event": {"主线": 0}}, CFG)
    text = R.step_one(clusters, 0, 0, extra, shown, era_n=0)
    assert "the word appeared 9 times" not in text, \
        "with no period on record, the blank-ledger finger must not report a blank"


def test_the_blank_ledger_finger_speaks_once_a_period_exists():
    # Criterion: the pair to the test above. The silence is conditional, not permanent —
    # asserting only the silence would let a bug that silences it forever pass.
    clusters, shown, extra, _ = R.layout([], {"空白记账": [a_finger(name="空白记账")]},
                                         {"mind": {"团": 0}, "event": {"主线": 1}}, CFG)
    text = R.step_one(clusters, 0, 0, extra, shown, era_n=1)
    assert "the word appeared 9 times" in text


def test_buckets_still_on_the_old_default_coordinates_are_called_out():
    # Criterion: (0.5, 0.3) is the factory placeholder, not a feeling anyone recorded.
    # Shelving by it would be grouping memories by the fact that nobody has judged them —
    # so they are held out, and the count is shown rather than hidden.
    text = R.step_one([], 0, default_coords=7, extra_clusters=0,
                      shown_fingers=[], era_n=1)
    assert "7" in text


def test_clusters_beyond_the_cap_are_counted_not_dropped_silently():
    # Criterion: a truncated list that does not say it was truncated is a lie of omission,
    # and this whole screen exists to be trusted at a glance.
    #
    # ⚠️ The first version of this asserted `"1" in text`, which is true of almost any
    #    render — the mutation check caught it staying green with the truncation notice
    #    deleted outright. Comparing against the un-truncated render is what makes it bite.
    cfg = {"max_clusters": 1, "max_fingers": 3}
    clusters, shown, extra, _ = R.layout([a_cluster(), a_cluster()], {}, STATS, cfg)
    assert extra == 1

    truncated = R.step_one(clusters, 0, 0, extra, shown, era_n=1)
    complete = R.step_one(clusters, 0, 0, 0, shown, era_n=1)
    assert truncated != complete, "a truncated screen must not read identically to a complete one"
    assert "还有 1" in truncated


# ───────────────── the second screen ─────────────────

def test_step_two_on_a_cluster_shows_every_member_verbatim():
    # Criterion: the second screen exists to be read word for word before deciding to
    # fold. Summarising here would defeat the only purpose it has.
    x = a_cluster(items=[an_item("a1", text="the first thought, in full"),
                         an_item("b2", text="the second thought, in full")],
                  from_core=["a1"], shared_from=["src1"])
    text = R._step_two(1, x)
    assert "the first thought, in full" in text
    assert "the second thought, in full" in text
    assert "src1" in text, "the shared ancestor is printed in full, not shortened"


def test_step_two_marks_which_members_came_from_a_link_and_which_from_a_vector():
    # Criterion: the two kinds of membership must stay distinguishable all the way to the
    # last screen — that difference is what the decision to fold rests on.
    x = a_cluster(ids=["a1", "b2"], from_core=["a1"], semantic_add=["b2"], min_sim=0.61)
    text = R._step_two(1, x)
    first, second = text.split("a1")[1], text.split("b2")[1]
    assert "from" in first
    assert first[:40] != second[:40]


def test_step_two_on_a_cluster_gives_no_dates():
    # Criterion: realizations do not go by the calendar — that is why they are shelved by
    # feeling rather than laid out in time. Printing dates here would quietly re-introduce
    # the ordering the whole design refuses.
    #
    # ⚠️ Assert against whatever `_date_label` actually produces, not against a guessed
    #    format. The first version looked for "2026-08-20" while the renderer writes
    #    "08-20" for the current year, so a mutation that added dates back stayed green.
    stamp = datetime(2026, 8, 20)
    text = R._step_two(1, a_cluster(items=[an_item("a1", ts=stamp, created=stamp)]))
    assert R._date_label(stamp) not in text
    assert str(stamp.year) not in text


def test_step_two_on_a_finger_shows_the_span_and_the_evidence():
    text = R._step_two(4, a_finger())
    assert "词爆发" in text
    assert "the word appeared 9 times" in text
    assert "something happened" in text
    assert 'fold(when="2026-08-01..2026-08-05")' in text


def test_a_drift_boundary_is_drawn_between_the_two_sides():
    # Criterion: the composition-drift finger points at a boundary rather than claiming a
    # stretch. Showing the members without the line would present a claim it never made.
    x = a_finger(name="成分漂移", boundary=datetime(2026, 8, 3),
                 items=[an_item("e1", room="EVENT/SELF", ts=datetime(2026, 8, 1),
                                text="before"),
                        an_item("e2", room="EVENT/SELF", ts=datetime(2026, 8, 4),
                                text="after")])
    text = R._step_two(4, x)
    assert text.index("before") < text.index("┈") < text.index("after")


def test_members_that_already_have_a_name_or_a_cover_are_marked():
    # Criterion: "already inside a named period" and "already folded under a gist" are two
    # different facts, and both change whether this finger is worth acting on. They are
    # computed differently and must not be collapsed into one mark.
    x = a_finger(items=[an_item("e1", room="EVENT/SELF", text="plain"),
                        an_item("e2", room="EVENT/SELF", text="named", named=True),
                        an_item("e3", room="EVENT/SELF", text="covered", covered=True)])
    text = R._step_two(4, x)
    named_line = [l for l in text.splitlines() if l.startswith("· e2")][0]
    covered_line = [l for l in text.splitlines() if l.startswith("· e3")][0]
    plain_line = [l for l in text.splitlines() if l.startswith("· e1")][0]
    assert named_line != covered_line
    assert "←" in named_line and "←" in covered_line and "←" not in plain_line


# ───────────────── the numbering the two screens share ─────────────────

def test_both_screens_number_from_the_same_list():
    # Criterion: `[N]` on the first screen has to point at the same thing on the second,
    # or someone reads one group and folds another. This is why the truncation lives in
    # `layout()` and not in either renderer.
    clusters, shown, extra, everything = R.layout(
        [a_cluster(), a_cluster()],
        {"词爆发": [a_finger(name="词爆发")], "成分漂移": [a_finger(name="成分漂移")]},
        {"mind": {"团": 2}, "event": {"主线": 1}}, CFG)

    assert len(everything) == 4
    assert isinstance(everything[0], M.Cluster) and isinstance(everything[2], M.Finger)

    text = R.step_one(clusters, 0, 0, extra, shown, era_n=1)
    for n in range(1, len(everything) + 1):
        assert f"[{n}]" in text
        R._step_two(n, everything[n - 1])   # every index must render without raising


@pytest.mark.parametrize("bad", [M.Cluster(ids=[], items=[], shelf_v=0.0, shelf_a=0.0)])
def test_an_empty_cluster_still_renders(bad):
    # Criterion: these run over live data. A shape that should not occur must degrade to a
    # readable line rather than take the whole screen down with it.
    assert R._mind_evidence(bad)
    assert R._step_two(1, bad)
