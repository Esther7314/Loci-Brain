# -*- coding: utf-8 -*-
"""CONTRACT: a dream goes whole → fragment → one line → gone, only ever forwards, and
both of the things that remove it leave something behind.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    A dream is the one thing in this system that is *supposed* to disappear. Everything
    else is built to keep what it was given; this is built to lose it on a schedule. That
    inverts the usual risk: a bug here does not look like data loss, it looks like the
    feature working, and the way to notice is that nothing is left where something should be.

    So the lifecycle is pinned at both ends.

    Forwards only:
        whole       survives on its own terms — no clock touches it
        fragment    after she has come back and spoken twice
        one line    after 30 minutes (or 15 turns) at fragment
        gone        after 60 minutes (or 30 turns)
      A recall can slow the slide down. It can never move it back up. "Slower" and
      "backwards" are one line of code apart and read identically in a diff.

    Two removals, two traces:
        the whole version is dropped by `degrade_on_wake()`   → leaves a `降级于` stamp on the record
        the file is deleted by `sweep_expired()`          → leaves a grown memory: there was a
                                                     dream here, and I cannot remember it
      Neither may remove its half silently, because a dream that vanishes with no trace
      is indistinguishable from a dream that was never woven.

WHAT IS DELIBERATELY NOT TESTED
    Whether the dream is any good, and whether the pressure line is set right. Both go
    through a model, both are hers to judge, and freezing either would freeze an opinion.
"""
import asyncio
import json

import pytest

from core import _dream as D
from tools import _runtime as rt

from datetime import datetime, timedelta

# ⚠️ Timezone-aware on purpose, and this is not a formality. A stamp written without an
#    offset is read back as UTC and shifted into local time, so a naive clock here would
#    put every dream hours away from where the test thinks it is — and the boundary tests
#    below would be asserting against the wrong minute while looking perfectly sensible.
WOVEN_AT = datetime(2026, 8, 20, 3, 0, 0, tzinfo=D._w.LOCAL_TZ)
CFG = D.dream_config()      # factory settings: fragment 30min/15turns, one-line 60min/30turns


class FakeLogger:
    def __init__(self):
        self.lines = []

    def _record(self, msg, *a):
        self.lines.append(str(msg) % a if a else str(msg))

    warning = info = debug = error = _record


@pytest.fixture
def dreams(tmp_path, monkeypatch):
    """A dream directory of its own, plus a clock the test drives."""
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "logger", FakeLogger())
    clock = [WOVEN_AT]
    monkeypatch.setattr(D._w, "now", lambda *a, **k: clock[0])
    return tmp_path, clock


def a_dream(**over) -> dict:
    """The shape `weave()` writes. `完整` present means the whole version is still alive."""
    rec = {
        "id": "d0000000feed",
        "织于": WOVEN_AT.isoformat(timespec="seconds"),
        "起算点": WOVEN_AT.isoformat(timespec="seconds"),
        "回想次数": 0,
        "轮次": 0,
        "碎片": "A corridor that kept going. Someone's keys. Then the sea, indoors.",
        "完整": "A long corridor with the wrong number of doors...",
    }
    rec.update(over)
    return rec


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── on disk, and only ours ─────────────────────────

def test_a_dream_round_trips_through_the_disk(dreams):
    tmp, _ = dreams
    path = D.save_record(a_dream())
    back = D.load_dreams()
    assert len(back) == 1
    assert back[0]["碎片"] == a_dream()["碎片"]
    assert back[0]["_路径"] == path


def test_the_private_path_key_is_not_written_to_the_file(dreams):
    # Criterion: `_路径` is filled in by the reader, for the reader. Writing it back would
    # bake an absolute path into the file, and the first time the data volume moves, every
    # dream would point somewhere that no longer exists.
    #
    # ⚠️ The record has to make a round trip first, and the mutation check is what showed
    #    that. A freshly built record has no `_路径` at all, so writing it out proves
    #    nothing; the key only exists on a record that came back off the disk — which is
    #    exactly the record every write after the first one is made from.
    tmp, _ = dreams
    D.save_record(a_dream())
    round_tripped = D.load_dreams()[0]
    assert "_路径" in round_tripped, "the reader does add it — otherwise this test is vacuous"

    D.save_record(round_tripped)
    raw = json.loads(next((tmp / "night_fall" / "dreams").iterdir()).read_text(encoding="utf-8"))
    assert "_路径" not in raw


def test_the_old_engines_dreams_are_left_alone(dreams):
    # Criterion: the retired engine left `dream_*.md` files in the same folder. They are
    # history, not garbage — this sweep must neither read them nor delete them. A prefix
    # check is the whole guard, and it is one edit away from being dropped as redundant.
    tmp, clock = dreams
    folder = tmp / "night_fall" / "dreams"
    folder.mkdir(parents=True, exist_ok=True)

    prose = folder / "dream_2026-08-01.md"
    prose.write_text("a dream from the engine that was retired", encoding="utf-8")
    # ⚠️ The second stranger is the one that matters, and the mutation check is why it is
    #    here. A prose file is turned away by the JSON parser whatever the prefix check
    #    does, so on its own it proves nothing. This one is well-formed JSON with an
    #    expired-looking timestamp: the ONLY thing standing between it and deletion is
    #    the check that the name starts with our own prefix.
    lookalike = folder / "dream_2026-08-01.json"
    lookalike.write_text(json.dumps(
        {"id": "old0000engine", "碎片": "from the retired engine",
         "织于": "2026-01-01T00:00:00+08:00"}), encoding="utf-8")

    D.save_record(a_dream())

    assert len(D.load_dreams()) == 1, "only our own files are read"
    clock[0] = WOVEN_AT + timedelta(days=200)
    run(D.sweep_expired(CFG))
    assert prose.exists(), "a file we do not own is never deleted by our sweep"
    assert lookalike.exists(), "not even one that parses and looks long expired"


def test_an_unreadable_dream_file_is_skipped_not_fatal(dreams):
    # Criterion: one corrupt file must not stop the others from being read or swept.
    tmp, _ = dreams
    D.save_record(a_dream())
    folder = tmp / "night_fall" / "dreams"
    (folder / f"{D.FILE_PREFIX}broken.json").write_text("{ not json", encoding="utf-8")
    assert len(D.load_dreams()) == 1


# ───────────────────────── whole: outside time ─────────────────────────

def test_the_whole_version_does_not_decay_with_the_clock(dreams):
    # Criterion: the whole version's only death is her coming back and speaking again.
    # If it aged like the rest, a dream woven at 3am would already be a fragment by the
    # time she woke up — and the one moment it exists for is the first thing she says.
    _, clock = dreams
    rec = a_dream()
    clock[0] = WOVEN_AT + timedelta(hours=9)
    assert D.layer_of(rec, clock[0], CFG) == "完整"


def test_the_whole_version_ignores_turns_too(dreams):
    # Criterion: same rule, other clock. Turn count is the gateway's measure; it must not
    # sneak past the short circuit either.
    _, clock = dreams
    rec = a_dream(轮次=99)
    assert D.layer_of(rec, clock[0], CFG) == "完整"


def test_an_empty_whole_field_is_not_a_whole_version(dreams):
    # Criterion: the field's *content* is the marker, not its presence. A record carrying
    # `完整: ""` has already been degraded, and reading presence alone would freeze it at
    # the top layer forever — it would never sink, never expire, never leave a trace.
    _, clock = dreams
    assert D.layer_of(a_dream(完整=""), clock[0], CFG) == "碎片"


# ───────────────────────── the slide, forwards only ─────────────────────────

@pytest.mark.parametrize("minutes,expected", [
    (0, "碎片"),
    (29, "碎片"),
    (30, "一句"),     # boundary is inclusive: at the mark, it has already slipped
    (59, "一句"),
    (60, "没了"),
    (600, "没了"),
])
def test_the_layers_follow_the_clock(dreams, minutes, expected):
    # Criterion: the boundaries are stated here so a config change is a visible decision
    # rather than a silent one. Written as a table because the failure worth catching is
    # an off-by-one at a boundary, and a table is where that shows.
    _, clock = dreams
    rec = a_dream(完整="")
    now = WOVEN_AT + timedelta(minutes=minutes)
    assert D.layer_of(rec, now, CFG) == expected


@pytest.mark.parametrize("turns,expected", [(0, "碎片"), (15, "一句"), (30, "没了")])
def test_turns_can_get_there_first(dreams, turns, expected):
    # Criterion: whichever clock arrives first wins. A long conversation in ten minutes
    # ages a dream as surely as an hour of silence does.
    _, clock = dreams
    assert D.layer_of(a_dream(完整="", 轮次=turns), WOVEN_AT, CFG) == expected


def test_a_recall_can_slow_the_slide_but_never_reverse_it(dreams):
    # Criterion: THE assertion of this section. Recall pushes the start point later, which
    # is how "thinking about it keeps it around" works. Left at that, the arithmetic alone
    # would let a one-line remnant read as a fragment again — a dream growing back. The
    # floor is what forbids it, and nothing else does.
    _, clock = dreams
    rec = a_dream(完整="", 到过的最低层="一句")
    # The start point has been pushed forward so the arithmetic says "fragment"...
    rec["起算点"] = (WOVEN_AT + timedelta(minutes=55)).isoformat(timespec="seconds")
    assert D.layer_of(rec, WOVEN_AT + timedelta(minutes=60), CFG) == "一句"


def test_the_floor_does_not_hold_it_above_where_it_has_got_to(dreams):
    # Criterion: the floor only ever stops it going back up. Time may still carry it
    # further down — otherwise recording a floor would freeze the dream at that layer.
    _, clock = dreams
    rec = a_dream(完整="", 到过的最低层="碎片")
    assert D.layer_of(rec, WOVEN_AT + timedelta(minutes=90), CFG) == "没了"


def test_a_record_with_no_timestamps_at_all_is_treated_as_expired(dreams):
    # Criterion: an unparseable start point must fail towards "gone", not towards
    # "immortal". A dream that can never expire is one that never leaves a trace either,
    # and it would sit in the folder forever.
    _, clock = dreams
    assert D.layer_of({"id": "x", "碎片": "..."}, WOVEN_AT, CFG) == "没了"


# ───────────────────────── removal #1: the whole version is dropped ─────────────────────────

def test_waking_drops_the_whole_version_and_stamps_when(dreams):
    # Criterion: the first of the two removals. The whole version goes, and `降级于`
    # records that it went — this is its trace. Without the stamp, "the whole version is
    # missing" and "there never was one" read the same on disk.
    _, clock = dreams
    D.save_record(a_dream())
    clock[0] = WOVEN_AT + timedelta(hours=5)

    assert D.degrade_on_wake() == ["d0000000feed"]

    rec = D.load_dreams()[0]
    assert not rec.get("完整"), "the whole version is gone"
    assert rec["降级于"] == clock[0].isoformat(timespec="seconds")
    assert rec["碎片"], "the fragment is what survives it"


def test_waking_restarts_the_clock_from_that_moment(dreams):
    # Criterion: the fragment's 30 minutes run from when she came back, not from when it
    # was woven. Measured from weaving, a dream from 3am would already be half rotted
    # before she read it — which contradicts the one rule the whole layer exists for.
    _, clock = dreams
    D.save_record(a_dream())
    clock[0] = WOVEN_AT + timedelta(hours=5)
    D.degrade_on_wake()

    rec = D.load_dreams()[0]
    assert D.layer_of(rec, clock[0], CFG) == "碎片"
    assert D.layer_of(rec, clock[0] + timedelta(minutes=31), CFG) == "一句"


def test_waking_clears_the_recall_history_from_the_previous_life(dreams):
    # Criterion: degrading starts a fresh lifecycle. Carrying over recalls and the floor
    # from while the whole version was alive would age the fragment on the strength of
    # attention paid to something that no longer exists.
    _, clock = dreams
    D.save_record(a_dream(回想次数=4, 到过的最低层="一句"))
    D.degrade_on_wake()
    rec = D.load_dreams()[0]
    assert rec["回想次数"] == 0
    assert rec["到过的最低层"] == "碎片"


def test_waking_twice_is_harmless(dreams):
    # Criterion: the gateway decides when to call this, from state it keeps separately.
    # The two can disagree after a restart, so a second call has to be a quiet no-op —
    # not an error, and above all not a second degradation that resets the clock again
    # and keeps the fragment alive indefinitely.
    _, clock = dreams
    D.save_record(a_dream())
    D.degrade_on_wake()
    stamped = D.load_dreams()[0]["降级于"]

    clock[0] = WOVEN_AT + timedelta(hours=1)
    assert D.degrade_on_wake() == []
    assert D.load_dreams()[0]["降级于"] == stamped, "the second call must not restamp anything"


# ───────────────────────── removal #2: the file goes, the fact stays ─────────────────────────

def test_an_expired_dream_is_deleted_and_leaves_a_memory(dreams, monkeypatch):
    # Criterion: the second removal, and the one that matters most. The file goes; what is
    # grown in its place is the true thing — I know I dreamt, I cannot tell you what.
    # A sweep that only deleted would be correct about the file and wrong about the person.
    import tools.grow as grow_mod
    grown: list[dict] = []

    async def fake_dispatch(**kw):
        grown.append(kw)
        return "trace-id"
    monkeypatch.setattr(grow_mod, "dispatch", fake_dispatch)

    _, clock = dreams
    path = D.save_record(a_dream(完整=""))
    clock[0] = WOVEN_AT + timedelta(minutes=90)

    out = run(D.sweep_expired(CFG))

    assert out["删了"] == ["d0000000feed"]
    import os
    assert not os.path.exists(path), "the dream file itself is gone"
    assert len(grown) == 1, "and exactly one memory was grown in its place"


def test_the_trace_is_an_ordinary_memory_not_something_pressing(dreams, monkeypatch):
    # Criterion: the trace is a thing that happened, full stop. Growing it as a `want`
    # would put "I had a dream" onto the list of things weighing on her — and there is
    # nothing to resolve, so it would sit there getting louder forever.
    import tools.grow as grow_mod
    grown: list[dict] = []
    monkeypatch.setattr(grow_mod, "dispatch",
                        lambda **kw: _record_and_return(grown, kw))

    _, clock = dreams
    D.save_record(a_dream(完整=""))
    clock[0] = WOVEN_AT + timedelta(minutes=90)
    run(D.sweep_expired(CFG))

    call = grown[0]
    assert call["kind"] == "event"
    assert "tense" not in call, "a dream is not something to be carried"
    item = call["items"][0]
    assert item["room"] == "EVENT/SELF"


def test_the_trace_does_not_borrow_the_dreams_own_feeling(dreams, monkeypatch):
    # Criterion: the dream's valence and arousal were assigned by a model. Storing them as
    # hers would be outsourcing the one field this system never outsources — and a trace
    # carrying a strong feeling falls straight back into the pool that feeds dream weaving,
    # so dreams would start feeding on dreams.
    import tools.grow as grow_mod
    grown: list[dict] = []
    monkeypatch.setattr(grow_mod, "dispatch",
                        lambda **kw: _record_and_return(grown, kw))

    _, clock = dreams
    D.save_record(a_dream(完整="", v=0.05, a=0.95))
    clock[0] = WOVEN_AT + timedelta(minutes=90)
    run(D.sweep_expired(CFG))

    item = grown[0]["items"][0]
    assert (item["v"], item["a"]) == (0.5, 0.3), "neutral, not the dream's own numbers"


def test_a_dream_that_has_not_expired_is_left_alone(dreams, monkeypatch):
    # Criterion: the sweep is lazy and runs on whatever call happens to come along, so it
    # runs often and mostly finds nothing. Sweeping something that still has time left
    # would delete dreams she has not been shown yet.
    import tools.grow as grow_mod
    grown: list[dict] = []
    monkeypatch.setattr(grow_mod, "dispatch",
                        lambda **kw: _record_and_return(grown, kw))

    _, clock = dreams
    path = D.save_record(a_dream(完整=""))
    clock[0] = WOVEN_AT + timedelta(minutes=10)

    out = run(D.sweep_expired(CFG))
    import os
    assert out["删了"] == [] and grown == []
    assert os.path.exists(path)


def test_a_failed_trace_does_not_stop_the_sweep(dreams, monkeypatch):
    # Criterion: the file is already gone by the time the trace is written, so an
    # exception there must not abort the loop — the remaining expired dreams would stay
    # on disk forever, and the next sweep would find them still expired and fail again.
    import tools.grow as grow_mod

    async def boom(**kw):
        raise RuntimeError("grow is unavailable")
    monkeypatch.setattr(grow_mod, "dispatch", boom)

    _, clock = dreams
    D.save_record(a_dream(完整=""))
    clock[0] = WOVEN_AT + timedelta(minutes=90)

    out = run(D.sweep_expired(CFG))
    assert out["删了"] == ["d0000000feed"]
    assert out["留痕"] == []


# ───────────────────────── the remnant is quoted, not rewritten ─────────────────────────

def test_the_last_line_is_cut_out_of_the_fragment_mechanically(dreams):
    # Criterion: "only one line left" takes the first sentence of what was already there.
    # It does not go back to a model. A remnant is what survives of the thing; a freshly
    # generated sentence would be a new thing wearing its clothes.
    assert D.first_sentence("门一直开着。后来是海。") == "门一直开着。"
    assert D.first_sentence("走廊没有尽头\n然后是海") == "走廊没有尽头"   # the cut is kept, the whitespace around it is not

    # ⚠️ Recorded, not asserted as desirable: the sentence splitter only knows 。！？…
    #    and a newline. An English fragment with no newline comes back whole, because
    #    there is nothing in the pattern that matches a full stop. That is fine while
    #    the dreams are written in Chinese, and it is a real gap the day they are not.
    assert D.first_sentence("A corridor. Then the sea.") == "A corridor. Then the sea."



def test_a_fragment_with_no_sentence_end_still_yields_something(dreams):
    # Criterion: models do not always punctuate. Returning empty here would show her a
    # blank where a remnant should be, which reads as "the dream is gone" one layer early.
    out = D.first_sentence("a corridor that kept going and going with no end in sight at all")
    assert out.strip()


def _record_and_return(bucket: list, kw: dict):
    """Helper for the monkeypatched `dispatch` — records the call, returns an awaitable."""
    bucket.append(kw)

    async def done():
        return "trace-id"
    return done()
