# -*- coding: utf-8 -*-
"""
tests/test_gist_merge.py — gists are thoughts muse can see, and two that say the same
thing can merge.

A gist is a sentence written over what it folds. While nothing covers it, it is a thought
like any other: it belongs in muse's pool, where two gists saying the same thing can be
laid side by side. Once a higher layer covers it, the one on top speaks for it and it
stays out. When fold writes a new gist that says what an existing one already says, the
return asks whether to merge — and a merge is just one more fold over both.
"""

import asyncio
import json
import pytest

from core import _fold as F
from core import _when as W
from core import _muse as M
from core import _sources as S
from core import scope as SC
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.fold import _merge_question
from tools.fold import dispatch as fold
from tools.grow import rooms_path

NOW = W.parse_stamp("2026-10-01T12:00:00")
OLD = "2026-08-01T10:00:00"


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── the muse pool ─────────────────────────

def gist_meta(bid, *, room="MIND/TRAITS", v=0.7, a=0.4, **extra):
    return {"id": bid, "room": room, "tags": [F.GIST_TAG], "created": OLD,
            "valence": v, "arousal": a, "cover": ["x1", "x2"], **extra}


def thought_meta(bid, *, v=0.7, a=0.4, **extra):
    return {"id": bid, "room": "MIND/TRAITS", "tags": [], "created": OLD,
            "valence": v, "arousal": a, **extra}


@pytest.fixture
def no_store(monkeypatch):
    # Without a store every recorded cover counts as live (`_fold._is_live`).
    monkeypatch.setattr(rt, "bucket_mgr", None)


def _pool(recs, kind="muse", cfg=None):
    return [it.id for it in M.pool_of(recs, kind, M.muse_config(cfg or {}), NOW)]


def test_an_uncovered_gist_is_in_the_muse_pool(no_store):
    # Criterion: the gist tag used to throw every gist out of muse, so a gist could never
    # meet another gist saying the same thing, and the merge had nowhere to come from.
    assert _pool([(gist_meta("g1"), "they recover by going out")]) == ["g1"]


def test_a_covered_gist_is_out_of_the_muse_pool(no_store):
    # Criterion: a covered gist is spoken for by the one on top; laying both out would be
    # the same thought twice.
    recs = [(gist_meta("g1", covered_by=["g9"]), "they recover by going out"),
            (gist_meta("g9", cover=["g1", "g2"]), "rest is going out, for them")]
    assert _pool(recs) == ["g9"]


def test_a_regrown_thought_is_in_and_its_old_version_out(no_store):
    recs = [(thought_meta("t1", superseded_by="t2", dont_surface=True), "first wording"),
            (thought_meta("t2", supersedes="t1"), "second wording")]
    assert _pool(recs) == ["t2"]


def test_periods_and_the_note_at_the_door_stay_machinery(no_store):
    period = gist_meta("p1", room="EVENT/SELF", when="2026-07-01..2026-07-05",
                       tags=[F.GIST_TAG, M.BIGEVENT_TAG])
    door = thought_meta("d1", tags=["__档案事实__"])
    assert M._is_utility_record(period) and M._is_utility_record(door)
    assert not M._is_utility_record(gist_meta("g1"))
    assert M._is_utility_record(gist_meta("g1", covered_by=["g9"]))


def test_two_gists_saying_the_same_thing_surface_together(no_store):
    # Criterion (work order 6.3): two gists that mean the same thing can be put side by
    # side by muse. Same feeling on the v/a plane, near-identical vectors.
    recs = [(gist_meta("g1", v=0.70, a=0.40), "they recover by going out"),
            (gist_meta("g2", v=0.72, a=0.41), "going out is how they rest"),
            (thought_meta("t1", v=0.71, a=0.39), "a walk resets their week")]
    items = M.pool_of(recs, "muse", M.muse_config({}), NOW)
    assert sorted(it.id for it in items) == ["g1", "g2", "t1"]
    vectors = {"g1": [1.0, 0.0, 0.05], "g2": [1.0, 0.02, 0.0], "t1": [0.98, 0.05, 0.03]}
    clusters, _scattered, _default = M.daydream(items, vectors, M.muse_config({}))
    assert len(clusters) == 1
    assert {"g1", "g2"} <= set(clusters[0].ids)


# ───────────────────────── fold asks whether to merge ─────────────────────────

class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


class _Embed:
    """Scores by id prefix of the stored entry; records what it was asked to rank among."""
    enabled = True

    def __init__(self, scores=None, fail=False):
        self.scores = scores or {}
        self.fail = fail
        self.among: list[set[str]] = []

    async def search_similar(self, query, top_k=10, among=None):
        if self.fail:
            raise RuntimeError("provider down")
        among = set(among or [])
        self.among.append(among)
        hits = [(bid, s) for bid, s in self.scores.items() if bid in among]
        return sorted(hits, key=lambda x: -x[1])[:top_k]

    async def generate_and_store(self, bucket_id, content):
        return False


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


async def _existing_gist(store, text="Going out is how they rest.", **kw):
    a = await store.create("tired days, they want out", room="MIND/TRAITS")
    b = await store.create("a lie-in does not reset them", room="MIND/TRAITS")
    gid, _ = await F.save_gist(text, "MIND/TRAITS", 0.7, 0.4, [a, b], **kw)
    return gid


async def _fold_two(store, text="They recover by going out."):
    c = await store.create("the hill on Saturday fixed the week", room="MIND/TRAITS")
    d = await store.create("staying in made it worse", room="MIND/TRAITS")
    return await fold(text=text, room="MIND/TRAITS", v=0.7, a=0.4, cover=[c, d])


def _new_id(out: str) -> str:
    return out.split("▣gist→", 1)[1].split()[0]


def test_fold_asks_to_merge_with_a_gist_at_the_line(store):
    async def go():
        gid = await _existing_gist(store)
        store.embedding_engine = _Embed({gid: 0.80})
        out = await _fold_two(store)
        new = _new_id(out)
        assert "要合吗" in out and gid in out and "0.80" in out
        # The question names the merge as the fold it is.
        assert f'fold(folds=["{new}", "{gid}"]' in out
        assert "Going out is how they rest." in out
    run(go())


def test_fold_does_not_ask_below_the_line(store):
    async def go():
        gid = await _existing_gist(store)
        store.embedding_engine = _Embed({gid: 0.79})
        out = await _fold_two(store)
        assert out.startswith("▣gist→") and "要合吗" not in out
    run(go())


def test_only_uncovered_mind_gists_are_candidates(store):
    async def go():
        live = await _existing_gist(store, "Going out is how they rest.")
        covered = await _existing_gist(store, "They go out to rest.")
        top = await _existing_gist(store, "Rest, for them, is outside.")
        # `top` covers `covered`, so only `top` speaks for that layer.
        assert await store.update(top, cover=[covered])
        assert await store.update(covered, covered_by=[top])
        period, _ = await F.save_gist("The hill weeks", "EVENT/SELF", 0.6, 0.5, [],
                                      when="2026-07-01..2026-07-05")
        plain = await store.create("they like the hill", room="MIND/TRAITS")
        ee = _Embed({live: 0.9, covered: 0.95, top: 0.85, period: 0.99, plain: 0.99})
        store.embedding_engine = ee
        out = await _fold_two(store)
        new = _new_id(out)
        assert ee.among[-1] == {live, top}
        # Highest first, at most two.
        assert out.index(live) < out.index(top)
        assert covered not in out.split("要合吗")[0].split("🔀", 1)[1]
        assert f'"{new}", "{live}"' in out
    run(go())


def test_without_embeddings_fold_still_succeeds_and_asks_nothing(store):
    async def go():
        await _existing_gist(store)
        store.embedding_engine = None
        out = await _fold_two(store)
        assert out.startswith("▣gist→") and "要合吗" not in out
        store.embedding_engine = _Embed(fail=True)
        out = await _fold_two(store, "They recover outdoors.")
        assert out.startswith("▣gist→") and "要合吗" not in out
    run(go())


def test_a_merge_is_one_more_fold_over_both(store):
    async def go():
        gid = await _existing_gist(store)
        store.embedding_engine = _Embed({gid: 0.9})
        new = _new_id(await _fold_two(store))
        store.embedding_engine = None
        out = await fold(text="Going out is how they rest and recover.", room="MIND/TRAITS",
                         v=0.7, a=0.4, cover=[new, gid])
        merged = _new_id(out)
        for old in (new, gid):
            assert F.covers_of((await store.get(old))["metadata"]) == [merged]
        # In muse, the merged one stands for both.
        recs = [((b.get("metadata") or {}) | {"created": OLD}, b.get("content") or "")
                for b in await store.list_all(include_archive=False)]
        pool = {it.id for it in M.pool_of(recs, "muse", M.muse_config({}), NOW)}
        assert merged in pool and new not in pool and gid not in pool
    run(go())


GROUP = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
BOT = SC.Host("book-bot", max_grant=(S.Place("telegram", "bot-a"),), token="bot-key")


def test_candidates_pass_the_gate_under_the_callers_scope(store):
    async def go():
        in_group = await store.create("Going out is how they rest.", room="MIND/TRAITS",
                                      tags=[F.GIST_TAG],
                                      sources=[{**GROUP, "id": "m_1", "use": {"venues": ["group"]}}])
        private = await store.create("They rest by going out.", room="MIND/TRAITS",
                                     tags=[F.GIST_TAG],
                                     sources=[{**GROUP, "id": "m_2", "use": {"venues": ["private"]}}])
        store.embedding_engine = _Embed({in_group: 0.9, private: 0.95})
        header = json.dumps({"v": 1, "entry": GROUP, "venue": "group",
                             "audience": ["user:U"], "grant": [GROUP]})
        with SC.request_scope(SC.RequestScope.resolve(BOT, header)):
            scoped = await _merge_question("ffffffffffff", "They recover by going out.",
                                           "MIND/TRAITS", [])
        unscoped = await _merge_question("ffffffffffff", "They recover by going out.",
                                         "MIND/TRAITS", [])
        assert in_group in scoped and private not in scoped
        assert private in unscoped
    run(go())
