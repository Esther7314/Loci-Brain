# -*- coding: utf-8 -*-
"""CONTRACT: a "these are not the same thing" rejection is remembered, and it survives a restart.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    An external review ran this system on synthetic data and made one observation worth
    acting on: the parts of it that are most worth having are exactly the parts a
    refactor destroys without anyone noticing, because they are *semantics*, not
    behaviour anyone thought to assert.

    Rejection is one of those. `muse()` proposes groups of realizations that look like
    they are about one thing. Saying "no, those are three different thoughts" is a
    judgement, and judgements are expensive: if the same group comes back tomorrow the
    tool is not helping, it is nagging, and the only way to make it stop is to stop
    looking at it. So the rejection has to persist, and it has to persist across a
    restart, or it may as well not exist.

    Nothing here asserts that clustering is *good*. That is a quality question and it
    moves. This asserts only that a decision, once made, is still made afterwards.

THE ONE DESIGN DECISION UNDER TEST
    A rejection is about the *group*, not about any member of it. It is therefore stored
    outside the memories themselves, keyed by the sorted id set. Two consequences follow,
    and both are deliberate:

      · the same ids in a different order are the same rejection
      · a group with one more (or one fewer) member is a DIFFERENT rejection, and gets
        proposed again — correctly, because at that point it really is a new group

    If someone ever "tidies" this by writing the rejection into each member's metadata,
    every one of these tests should go red, and that is the entire point of the file.
"""
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from core import _muse as M

LIMIT = 1  # one "no" is enough to silence a group; the tests state the limit explicitly

_SRC = str(Path(__file__).resolve().parent.parent / "src")


def _ids(*n: str) -> list[str]:
    return list(n)


def _key_in_a_fresh_process(ids: list[str], hash_seed: str) -> str:
    """Compute the key in a *different interpreter*, with a chosen string-hash seed.

    Why go to this trouble: see `test_the_key_is_the_same_in_a_different_process`. Set
    iteration order is stable inside one process and unstable between processes, so an
    in-process assertion cannot tell a sorted key from an unsorted one. This can.
    """
    code = (
        "import sys; sys.path.insert(0, %r)\n"
        "from core import _muse as M\n"
        "print(M.rejection_key(%r))\n" % (_SRC, ids)
    )
    r = subprocess.run([sys.executable, "-c", code], capture_output=True,
                       env={**os.environ, "PYTHONHASHSEED": hash_seed,
                            "PYTHONIOENCODING": "utf-8"})
    assert r.returncode == 0, r.stderr.decode("utf-8", "replace")
    return r.stdout.decode("utf-8").strip()


# ───────────────────────── the key is the group, not the order ─────────────────────────

def test_key_is_order_independent():
    # Criterion: the same three thoughts shuffled are the same three thoughts. If the key
    # ever became order-sensitive, a rejection would silently stop matching the moment
    # muse returned the group in a different order — which it does, since ordering comes
    # out of scoring.
    #
    # ⚠️ This assertion alone is NOT enough, and the mutation check is what proved it:
    #    delete the `sorted()` from `rejection_key` and this test stays green, because a
    #    Python set iterates in the same order for the same strings *within one process*.
    #    The test below is the one that actually holds the sort in place.
    assert M.rejection_key(_ids("a", "b", "c")) == M.rejection_key(_ids("c", "a", "b"))


def test_the_key_is_the_same_in_a_different_process():
    # Criterion: THE assertion that makes "survives a restart" mean anything. The key is
    # built from a set, and CPython randomizes string hashing per process, so an unsorted
    # key comes out in a different order in tomorrow's process than in today's — and every
    # rejection ever recorded quietly stops matching. Nothing raises; the groups just come
    # back, and it reads as "muse is nagging again" rather than as a bug.
    #
    # Two different hash seeds, two fresh interpreters, one required answer.
    ids = _ids("m9", "zz", "c", "a", "b")
    first = _key_in_a_fresh_process(ids, "1")
    second = _key_in_a_fresh_process(list(reversed(ids)), "12345")
    assert first == second == "a,b,c,m9,zz"


def test_key_ignores_blank_and_duplicate_ids():
    # Criterion: whitespace and repeats are noise from the caller, not part of the group.
    assert M.rejection_key(["a", " a ", "", "  ", "b"]) == M.rejection_key(["a", "b"])


def test_a_different_sized_group_is_a_different_key():
    # Criterion: adding a fourth thought makes it a new question, and a new question is
    # allowed to be asked. This is the behaviour that keeps a rejection from hardening
    # into "never mention any of these again".
    assert M.rejection_key(_ids("a", "b", "c")) != M.rejection_key(_ids("a", "b", "c", "d"))


# ───────────────────────── the rejection is written down ─────────────────────────

def test_recording_a_rejection_makes_it_stick(tmp_path):
    # Criterion: record once, and asking again says yes-this-was-rejected.
    d = str(tmp_path)
    assert M.is_rejected(M.load_rejected(d), _ids("a", "b"), LIMIT) is False

    key, count = M.record_rejection(d, _ids("a", "b"))
    assert count == 1
    assert M.is_rejected(M.load_rejected(d), _ids("a", "b"), LIMIT) is True


def test_rejection_survives_a_restart(tmp_path):
    # Criterion: THE one the reviewer asked for. `load_rejected` reads from disk with no
    # in-process state of any kind, so calling it fresh is exactly what a restarted
    # process does. A rejection held only in memory would pass every other test in this
    # file and still nag her tomorrow morning.
    d = str(tmp_path)
    M.record_rejection(d, _ids("x", "y", "z"))

    on_disk = tmp_path / "_state" / M.REJECT_FILE
    assert on_disk.exists(), "a rejection that never reached the disk cannot survive anything"

    # Nothing is carried over here but the directory path — the same thing a new process has.
    assert M.is_rejected(M.load_rejected(d), _ids("z", "y", "x"), LIMIT) is True


def test_recording_twice_counts_up_and_keeps_the_first_time(tmp_path, monkeypatch):
    # Criterion: the count is what lets a limit above 1 exist at all ("ask me twice before
    # you give up"). `first` must not move, or "how long have I been saying no to this"
    # becomes unanswerable.
    #
    # ⚠️ The clock is driven on purpose. Timestamps are written at one-second resolution,
    #    so two real calls land on the same value and the assertion passes whether `first`
    #    is preserved or overwritten — the mutation check caught exactly that false green.
    #    Two hours apart, there is nothing left to hide behind.
    d = str(tmp_path)
    clock = [datetime(2026, 8, 20, 9, 0, 0)]
    monkeypatch.setattr(M._w, "now", lambda *a, **k: clock[0])

    _, first_count = M.record_rejection(d, _ids("a", "b"))
    first_stamp = M.load_rejected(d)["rejected"][M.rejection_key(_ids("a", "b"))]["first"]

    clock[0] = datetime(2026, 8, 20, 11, 0, 0)
    _, second_count = M.record_rejection(d, _ids("b", "a"))
    entry = M.load_rejected(d)["rejected"][M.rejection_key(_ids("a", "b"))]

    assert (first_count, second_count) == (1, 2)
    assert first_stamp == "2026-08-20T09:00:00"
    assert entry["first"] == first_stamp, "the first refusal is when it started, not when it was last repeated"
    assert entry["last"] == "2026-08-20T11:00:00", "`last` is the one that moves"
    assert entry["ids"] == ["a", "b"], "stored ids are normalized, so the record reads the same as the key"


def test_a_grown_group_is_proposed_again(tmp_path):
    # Criterion: rejecting {a,b} must not silently reject {a,b,c}. The new member may be
    # the very thing that makes them one thought — refusing to ask again would bury that
    # permanently, and nothing would ever surface the loss.
    d = str(tmp_path)
    M.record_rejection(d, _ids("a", "b"))
    assert M.is_rejected(M.load_rejected(d), _ids("a", "b", "c"), LIMIT) is False


def test_limit_is_honoured(tmp_path):
    # Criterion: below the limit is not rejected. Without this the limit parameter would
    # be decorative, and a config change from 1 to 2 would do nothing.
    d = str(tmp_path)
    M.record_rejection(d, _ids("a", "b"))
    assert M.is_rejected(M.load_rejected(d), _ids("a", "b"), 2) is False
    M.record_rejection(d, _ids("a", "b"))
    assert M.is_rejected(M.load_rejected(d), _ids("a", "b"), 2) is True


# ───────────────────────── a damaged file degrades, it does not explode ─────────────────────────

def test_missing_file_reads_as_no_rejections(tmp_path):
    # Criterion: a fresh install has rejected nothing. Reading must not require the file
    # to exist, or the first ever `muse()` call raises.
    assert M.load_rejected(str(tmp_path)) == {"version": 1, "rejected": {}}


def test_corrupt_file_reads_as_no_rejections_instead_of_raising(tmp_path):
    # Criterion: the failure mode of a broken state file is "you get asked about a group
    # you already refused" — mildly annoying. The failure mode of raising here is that
    # `muse()` stops working entirely. Choose the annoying one.
    state = tmp_path / "_state"
    state.mkdir()
    (state / M.REJECT_FILE).write_text("{not json at all", encoding="utf-8")
    assert M.load_rejected(str(tmp_path)) == {"version": 1, "rejected": {}}


def test_wrong_shaped_file_reads_as_no_rejections(tmp_path):
    # Criterion: valid JSON of the wrong shape is the case a plain try/except around
    # json.load would miss — `is_rejected` would then index into a list and raise.
    state = tmp_path / "_state"
    state.mkdir()
    (state / M.REJECT_FILE).write_text(json.dumps(["a", "b"]), encoding="utf-8")
    assert M.load_rejected(str(tmp_path)) == {"version": 1, "rejected": {}}


def test_rejections_live_outside_the_memories(tmp_path):
    # Criterion: the file is under `_state/`, not inside any bucket. This is the structural
    # half of the design decision in the module docstring — a rejection is a fact about a
    # combination, and combinations have no home inside a single member.
    d = str(tmp_path)
    M.record_rejection(d, _ids("a", "b"))
    assert os.path.isfile(os.path.join(d, "_state", M.REJECT_FILE))
    assert sorted(p.name for p in tmp_path.iterdir()) == ["_state"], \
        "recording a rejection must not touch, create or rewrite any memory file"
