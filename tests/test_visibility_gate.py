# -*- coding: utf-8 -*-
"""
tests/test_visibility_gate.py — every road that shows a memory asks one gate.

WHY THIS FILE EXISTS
    The rules for "may this memory be put in front of the model" used to live wherever a
    road needed them, and a road that forgot one leaked without a sound: 「忽然想起」 and
    the undigested dream pool both showed an entry its owner had marked `dont_surface`,
    and a read by id handed back an archived or deleted entry word for word, as if it were
    current. core/visibility.py gathers the rules; this file feeds each of the eight places
    an archived, a deleted and a dont_surface entry and says what each must do with it.

THE TABLE
    hidden   the place leaves it out
    marked   a lookup shows it, with its state said out loud before anything else
    shown    it comes up: a lookup hides nothing for dont_surface, and some surfacing
             roads do not count dont_surface (the rules by the door, 依据变了的, muse)
             — a choice, written down here so it is visible

    Two rows have no place to feed yet and are skipped with the stage that adds them:
    write tools' returns (stage 6) and strong-reminder / name cards (5.5).

    Hole 4 — what a read by id does with an entry that is not live, and with the entries
    it links to — has tests of its own below the table.
"""

import asyncio
import json
import re
from datetime import datetime, timedelta

import pytest

from core import _dream as D
from core import _muse as M
from core import _when as W
from core import visibility as V
from core.bucket_manager import BucketManager
from core._invalidation import block as invalidation_block
from core.profile import door_note, edited_by_user, event_pool, involuntary, prospective
from tools import _runtime as rt
from tools.recall import core as R

NOW = datetime(2026, 8, 22, 21, 0, 0, tzinfo=W.LOCAL_TZ)

# The three entries every place is fed, as metadata laid over an ordinary entry.
STATE_META = {
    "archived": {"type": "archived"},
    "deleted": {"deleted_at": "2026-08-21T10:00:00", "tombstone": True},
    "dont_surface": {"dont_surface": True},
}
STATES = tuple(STATE_META)


class SilentLogger:
    def _noop(self, *a, **k):
        return None
    warning = info = debug = error = _noop


def run(coro):
    return asyncio.run(coro)


def bucket(bid, content="She left the window open all night.", *, tags=None,
           room="EVENT/SELF", created=None, **meta):
    m = {"id": bid, "room": room, "tags": list(tags or []),
         "created": (created or (NOW - timedelta(days=1))).isoformat(timespec="seconds")}
    m.update(meta)
    return {"id": bid, "content": content, "metadata": m}


def fed(state, *a, **kw):
    """An ordinary entry carrying one of the three states."""
    return bucket(*a, **{**kw, **STATE_META[state]})


def ids(rows):
    return [r["id"] for r in rows]


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", SilentLogger())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return mgr


async def put_in_state(store, bid, state):
    """The real operation behind each state, on the store."""
    if state == "archived":
        assert await store.archive(bid)
    elif state == "deleted":
        assert await store.delete(bid)
    else:
        assert await store.update(bid, dont_surface=True)


# ── the eight places ────────────────────────────────────────────────────────

def breath_reminders(state, store):
    out = door_note([fed(state, "aaa", when=(NOW + timedelta(days=3)).strftime("%Y-%m-%d"),
                         direction_of_fit="telic")], NOW)
    return "shown" if ids(out["reminders"]) + ids(out["heavy"]) else "hidden"


def breath_prospective(state, store):
    out = prospective([fed(state, "aaa", when=(NOW + timedelta(days=3)).strftime("%Y-%m-%d"),
                           direction_of_fit="telic", bound=["AI"])], NOW)
    return "shown" if out["items"] else "hidden"


def breath_sudden(state, store):
    return "shown" if event_pool([fed(state, "aaa")], NOW) else "hidden"


def breath_involuntary(state, store):
    rows = [fed(state, "aaa", created=NOW - timedelta(days=30))]
    return "shown" if involuntary(rows, NOW) else "hidden"


def breath_invalidation(state, store):
    rec = {"kind": "overturn", "of": "b" * 12, "by": "c" * 12, "at": "2026-08-20"}
    return "shown" if invalidation_block([fed(state, "aaa", invalidation=[rec])], None) else "hidden"


def breath_rules(state, store):
    return "shown" if door_note([fed(state, "aaa", pinned=True)], NOW)["rules"] else "hidden"


def breath_edited(state, store):
    return "shown" if edited_by_user([fed(state, "aaa", tags=["人改的"])]) else "hidden"


def recall_browse(state, store):
    entries, err, _ledger = run(R._collect("", "", "", "", all_buckets=[fed(state, "aaa")]))
    assert not err
    return "shown" if entries else "hidden"


def recall_search(state, store):
    async def go():
        bid = await store.create("The lighthouse keeper's ledger, water-stained.", tags=["t"])
        await put_in_state(store, bid, state)
        return await R.recall_core(when="", room="", tag="", query="lighthouse")
    return "shown" if "keeper's ledger" in run(go()) else "hidden"


def recall_read_by_id(state, store):
    async def go():
        bid = await store.create("The lighthouse keeper's ledger, water-stained.", tags=["t"])
        await put_in_state(store, bid, state)
        return await R.recall_core(when="", room="", tag="", query=bid)
    out = run(go())
    if "water-stained" not in out:
        return "hidden"
    return "marked" if out.splitlines()[1].startswith("⚠️在归档区") else "shown"


def muse_pool(state, store):
    rec = fed(state, "aaa", room="MIND/VIEWS", created=NOW - timedelta(days=60))
    pool = M.pool_of([(rec["metadata"], rec["content"])], "muse", M.muse_config({}), NOW)
    return "shown" if pool else "hidden"


def dream_wants(state, store):
    rec = fed(state, "aaa", direction_of_fit="telic", weight=0.9)
    return "shown" if D.want_pool([(rec["metadata"], rec["content"])], NOW) else "hidden"


def dream_undigested(state, store):
    rec = fed(state, "aaa", valence=0.1, arousal=0.9)
    pool = D.unclear_pool([(rec["metadata"], rec["content"])], set(), {}, NOW)
    return "shown" if pool else "hidden"


def dream_handed_out(state, store):
    async def go():
        bid = await store.create("A staircase that went down into the sea.", tags=["t"])
        D.save_record(a_dream(bid))
        await put_in_state(store, bid, state)
        return await D.handable_dreams(D.load_dreams())
    return "shown" if run(go()) else "hidden"


def a_dream(*ingredients) -> dict:
    stamp = W.now().isoformat(timespec="seconds")
    return {"id": "d0000000feed", "织于": stamp, "起算点": stamp, "回想次数": 0, "轮次": 0,
            "碎片": "Stairs. Salt water. A door left open.", "完整": "A staircase, the sea...",
            "v": 0.4, "a": 0.6, "nightmare": False,
            "素材": {"压在心头": list(ingredients), "想不明白": [], "几个词": []}}


NOT_YET = pytest.mark.skip


# (place, how to feed it, what it must do with: archived, deleted, dont_surface)
PLACES = [
    ("1 profile page · ⏰ reminders / 🫀 weighing", breath_reminders, ("hidden", "hidden", "hidden")),
    ("1 breath · 惦记的事", breath_prospective, ("hidden", "hidden", "hidden")),
    ("1 breath · 忽然想起's pool (leak closed)", breath_sudden, ("hidden", "hidden", "hidden")),
    ("1 breath · 忽然想起", breath_involuntary, ("hidden", "hidden", "hidden")),
    ("1 breath · rules by the door", breath_rules, ("hidden", "hidden", "shown")),
    ("1 breath · 依据变了的: panel edits", breath_edited, ("hidden", "hidden", "shown")),
    ("1 breath · 依据变了的: changed bases", breath_invalidation, ("hidden", "hidden", "shown")),
    ("2 recall · time browsing", recall_browse, ("hidden", "hidden", "shown")),
    ("2 recall · search", recall_search, ("hidden", "hidden", "shown")),
    ("3 recall · read by id (hole 4)", recall_read_by_id, ("marked", "marked", "shown")),
    ("4 muse · the pool", muse_pool, ("hidden", "hidden", "shown")),
    ("5 dream · wants", dream_wants, ("hidden", "hidden", "hidden")),
    ("5 dream · undigested (leak closed)", dream_undigested, ("hidden", "hidden", "hidden")),
    ("6 dream · handed out", dream_handed_out, ("hidden", "hidden", "hidden")),
    pytest.param("7 write tools' returns", None, None,
                 marks=NOT_YET(reason="stage 6 adds 回望 / 场景常来; they ask the gate")),
    pytest.param("8 strong-reminder and name cards", None, None,
                 marks=NOT_YET(reason="5.5 adds them; they ask the gate")),
]


@pytest.mark.parametrize("place, feed, expected", PLACES, ids=lambda x: x if isinstance(x, str) else "")
def test_each_place_does_what_the_table_says(store, place, feed, expected):
    got = tuple(feed(state, store) for state in STATES)
    assert got == expected, f"{place}: archived/deleted/dont_surface gave {got}"


# ── the gate itself ─────────────────────────────────────────────────────────

def test_a_scope_that_is_not_a_scope_view_is_refused():
    # Criterion: a caller passing a scope must not believe it was applied. The gate takes
    # a core.scope.ScopeView (tests/test_read_scope.py); anything else is an error, not
    # the whole library.
    with pytest.raises(ValueError):
        V.visible_for({}, scope={"use": "chat"}, road=V.READ)
    with pytest.raises(ValueError):
        V.visible_for({}, scope="scoped", road=V.READ)


def test_a_road_that_counts_holds_needs_the_index():
    # Criterion: forgetting the index must fail loudly, not quietly count no holds.
    with pytest.raises(ValueError):
        V.visible_for({}, road=V.SUDDEN)


def test_dont_surface_on_an_old_version_is_the_chain_not_a_choice():
    # regrow writes both fields on the old version. A road that lets old versions through
    # (a dream handed out) must not be closed by the chain's own dont_surface.
    old = {"superseded_by": "bbbbbbbbbbbb", "dont_surface": True}
    assert V.visible_for(old, road=V.DREAM_HANDOUT, holds=V._H.hold_index([]))
    reasons = V.visible_for(old, road=V.SUDDEN, holds=V._H.hold_index([])).reasons
    assert V.SUPERSEDED in reasons and V.DONT_SURFACE not in reasons


def test_an_avoid_hold_withholds_a_handed_out_dream_and_a_defer_does_not():
    target = bucket("aaa")
    for level, shown in (("avoid", False), ("defer", True)):
        hold = bucket("hhh", exception_of="aaa", hold=level, direction_of_fit="telic")
        idx = V._H.hold_index([target, hold])
        assert bool(V.visible_for(target, road=V.DREAM_HANDOUT, now=NOW, holds=idx)) is shown


# ── hole 4: a read by id of an entry that is not live ───────────────────────

@pytest.mark.parametrize("state, word", [("archived", "沉下去"), ("deleted", "已删除")])
def test_a_read_by_id_says_the_state_first_and_still_gives_the_body(store, state, word):
    async def go():
        bid = await store.create("She sold the piano in March.", tags=["t"])
        await put_in_state(store, bid, state)
        return bid, await R.recall_core(when="", room="", tag="", query=bid)
    bid, out = run(go())
    lines = out.splitlines()
    # Criterion: the state is the line right under the header — before the facts, the
    # sources, the body — so nothing above it reads as a current memory.
    assert lines[0].startswith(f"═ {bid}")
    assert lines[1].startswith("⚠️在归档区") and word in lines[1]
    # The body still comes out whole: a lookup hides nothing. Its rule says where it is from.
    rule = next(i for i, ln in enumerate(lines) if ln.startswith("─"))
    assert "归档区里的原文" in lines[rule]
    assert lines[rule + 1] == "She sold the piano in March."


def test_a_live_read_by_id_is_unchanged(store):
    async def go():
        bid = await store.create("She sold the piano in March.", tags=["t"])
        return await R.recall_core(when="", room="", tag="", query=bid)
    lines = run(go()).splitlines()
    assert "⚠️在归档区" not in "\n".join(lines)
    assert "─" * 30 in lines


def test_linked_entries_carry_their_state_and_stay_one_line(store):
    async def go():
        sank = await store.create("The old flat on Wenhua Road.", tags=["t"])
        gone = await store.create("A quarrel about the keys.", tags=["t"])
        kept = await store.create("The new flat has a balcony.", tags=["t"])
        gist = await store.create("Moving taught her what to keep.", tags=["t"],
                                  room="MIND/VIEWS")
        assert await store.update(gist, prov=[{"rel": "wasDerivedFrom", "target": sank},
                                              {"rel": "wasDerivedFrom", "target": gone},
                                              {"rel": "wasDerivedFrom", "target": kept}],
                                  cover=[sank, gone, kept])
        assert await store.archive(sank)
        assert await store.delete(gone)
        return sank, gone, kept, await R.recall_core(when="", room="", tag="", query=gist)
    sank, gone, kept, out = run(go())
    by_id: dict[str, list[str]] = {}
    for ln in out.splitlines():
        m = re.match(r"\s+[←▣◈→] (\S+)", ln)
        if m:
            by_id.setdefault(m.group(1), []).append(ln)
    # Criterion: every line naming a linked entry says its state — sources and what it
    # covers alike — and none of them is expanded into its body.
    for ln in by_id[sank]:
        assert "⚠️在归档区" in ln and "已删除" not in ln
    for ln in by_id[gone]:
        assert "⚠️在归档区（已删除）" in ln
    for ln in by_id[kept]:
        assert "在归档区" not in ln
    assert len(by_id[sank]) == len(by_id[gone]) == len(by_id[kept]) == 2
    assert "A quarrel about the keys." not in out.split("─" * 30)[0]


# ── a dream handed out, through both doors ──────────────────────────────────

def test_a_withheld_dream_is_absent_from_both_doors_and_is_not_recalled(store, monkeypatch):
    from web import loci as L

    async def no_muse():
        return {"worth_poking": False}
    monkeypatch.setattr(L, "build_muse_pending", no_muse)

    async def go():
        bid = await store.create("A staircase that went down into the sea.", tags=["t"])
        D.save_record(a_dream(bid))
        before = (await L.build_poke())["dreams"]
        await put_in_state(store, bid, "dont_surface")
        after = (await L.build_poke())["dreams"]
        current = await D.current_dream()
        return before, after, current
    before, after, current = run(go())
    assert [d["id"] for d in before] == ["d0000000feed"]
    # Criterion: absent is the shape the bridge already reads — no new key, no stub entry.
    assert after == []
    assert current is None
    # Withholding writes nothing: the dream was not counted as recalled.
    assert json.load(open(D.load_dreams()[0]["_路径"], encoding="utf-8"))["回想次数"] == 0
