# -*- coding: utf-8 -*-
"""
tests/test_sources_tools.py — what the write tools do with `sources` and write keys.

Through the tools' dispatch against a real BucketManager, read back from the files:
a record lands with its quoted prov line; a bare host id in `from` is linked to the
record with that id; the registry's withdrawn and deleted refuse the write and its
unreadable is noted; the same delivery on another memory is a hint, never a block;
regrow carries records and leaves a withdrawn one behind; trace appends. A write key
answers a resend with the first reply, also after a restart; no key, no dedup by key.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _sources as S
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.fold import dispatch as fold
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.recall import core as R
from tools.regrow import dispatch as regrow
from tools.trace import dispatch as trace

REC = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0142",
       "fingerprint": "sha256:aa", "fingerprint_by": "adapter"}
SRC = "lento:home/private:U#m_0142"
VIEW = "Quiet mornings are when the week gets sorted out."


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


def _install(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    return mgr


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = _install(tmp_path, monkeypatch)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def run(coro):
    return asyncio.run(coro)


def _files(tmp_path):
    return sorted(p for p in tmp_path.rglob("*.md"))


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _quoted(meta) -> list[str]:
    return [ln["target"] for ln in meta.get("prov") or [] if ln["rel"] == "wasQuotedFrom"]


async def _event(text="We talked it over at breakfast.", **kw):
    out = await grow(kind="event", items=[{"room": "EVENT/WORLD", "text": text,
                                           "v": 0.6, "a": 0.3}], **kw)
    return out, (out.split("📝", 1)[1].split()[0] if "📝" in out else "")


def _withdraw(store, kind="withdrawn", seq=1, source=SRC):
    return run(store.sources.apply_change(
        {"change_id": f"{kind}-{seq}", "source": source, "kind": kind, "host_seq": seq}))


# ───────────────────────── a record and its quoted line ─────────────────────────

def test_an_event_stores_the_record_and_quotes_it(store, tmp_path):
    out, bid = run(_event(sources=[REC]))
    meta = _disk(tmp_path, bid)
    assert meta["sources"][0]["id"] == "m_0142" and meta["sources"][0]["fingerprint"] == "sha256:aa"
    assert _quoted(meta) == [SRC]


def test_a_bare_line_id_in_from_is_linked_to_its_record(store, tmp_path):
    _out, bid = run(_event(from_=["m_0142"], sources=[{**REC, "revision": "2"}]))
    assert _quoted(_disk(tmp_path, bid)) == [SRC + "@2"]


def test_a_bare_line_id_with_no_record_stays_as_it_is(store, tmp_path):
    _out, bid = run(_event(from_=["m_0931"]))
    meta = _disk(tmp_path, bid)
    assert _quoted(meta) == ["m_0931"] and "sources" not in meta


def test_a_full_string_form_in_from_gets_the_record_it_spells(store, tmp_path):
    _out, bid = run(_event(from_=[SRC + "@5"]))
    [rec] = _disk(tmp_path, bid)["sources"]
    assert S.record_string(rec) == SRC + "@5"


def test_a_bare_id_naming_two_records_is_refused(store, tmp_path):
    out, bid = run(_event(from_=["m_0142"],
                          sources=[REC, {**REC, "container": "group:G"}]))
    assert not bid and "好几条来源" in out and _files(tmp_path) == []


def test_a_malformed_record_is_refused_in_words(store, tmp_path):
    out, bid = run(_event(sources=[{"system": "lento", "id": "m_1"}]))
    assert not bid and out.startswith("sources 不对") and _files(tmp_path) == []


def test_a_json_string_is_taken_and_a_mind_may_stand_on_sources_alone(store, tmp_path):
    out = run(grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                   sources='[{"system": "lento", "instance": "home", '
                           '"container": "private:U", "id": "m_7"}]'))
    assert out.startswith("🧠mind→"), out
    bid = out.split("🧠mind→", 1)[1].split()[0]
    assert _quoted(_disk(tmp_path, bid)) == ["lento:home/private:U#m_7"]


# ───────────────────────── the registry at the door ─────────────────────────

@pytest.mark.parametrize("kind, word", [("withdrawn", "撤回"), ("deleted", "删除")])
def test_a_withdrawn_or_deleted_source_refuses_the_write(store, tmp_path, kind, word):
    _withdraw(store, kind)
    out, bid = run(_event(sources=[REC]))
    assert not bid and SRC in out and word in out and _files(tmp_path) == []
    out, bid = run(_event(from_=[SRC]))
    assert not bid and word in out and _files(tmp_path) == []


def test_an_unreadable_source_is_taken_with_a_note(store, tmp_path):
    _withdraw(store, "unreadable")
    out, bid = run(_event(sources=[REC]))
    assert bid and "读不到" in out


def test_a_grant_limits_the_turn_to_its_own_sources(store, tmp_path):
    with S.grant_scope(["lento:home/private:U#m_other"]):
        out, bid = run(_event(sources=[REC]))
    assert not bid and "不在这一轮" in out
    with S.grant_scope([SRC]):
        out, bid = run(_event(sources=[REC]))
    assert bid


def test_the_same_delivery_on_another_memory_is_a_hint_not_a_block(store, tmp_path):
    _out, first = run(_event(sources=[REC]))
    out, second = run(_event("The second thing said at breakfast.", sources=[REC]))
    assert second and second != first
    assert f"这条来源已经记过 {first}" in out
    # Another fingerprint is another delivery: no hint.
    out, third = run(_event("A third thing.", sources=[{**REC, "fingerprint": "sha256:bb"}]))
    assert third and "已经记过" not in out


def test_identical_text_is_still_deduplicated(store, tmp_path):
    run(_event(sources=[REC]))
    out, _bid = run(_event(sources=[REC]))
    assert "♻️" in out and len(_files(tmp_path)) == 1


# ───────────────────────── fold, regrow, trace ─────────────────────────

def test_fold_stores_sources(store, tmp_path):
    async def go():
        a = await store.create("First view.", room="MIND/VIEWS")
        b = await store.create("Second view.", room="MIND/VIEWS")
        out = await fold(text="Both views.", room="MIND/VIEWS", v=0.5, a=0.3,
                         cover=[a, b], sources=[REC])
        return out.split("▣gist→", 1)[1].split()[0]
    gist = run(go())
    meta = _disk(tmp_path, gist)
    assert _quoted(meta) == [SRC] and meta["sources"][0]["id"] == "m_0142"


def test_regrow_carries_records_adds_new_ones_and_leaves_a_withdrawn_one(store, tmp_path):
    other = {**REC, "id": "m_0143", "fingerprint": None}
    out = run(grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                   sources=[REC, other]))
    old = out.split("🧠mind→", 1)[1].split()[0]
    _withdraw(store, source="lento:home/private:U#m_0143")
    third = {**REC, "id": "m_0144", "fingerprint": None}
    out = run(regrow(bucket_id=old, text=VIEW + " Rainy ones too.", v=0.6, a=0.3,
                     mode="supplement", sources=[third]))
    new = _disk(tmp_path, old)["superseded_by"]
    meta = _disk(tmp_path, new)
    assert [r["id"] for r in meta["sources"]] == ["m_0142", "m_0144"]
    assert _quoted(meta) == [SRC, "lento:home/private:U#m_0144"]
    assert "m_0143" in out and "撤回或删除" in out
    # The old version keeps what it was formed on.
    assert [r["id"] for r in _disk(tmp_path, old)["sources"]] == ["m_0142", "m_0143"]


def test_regrow_without_sources_carries_them(store, tmp_path):
    out = run(grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3, sources=[REC]))
    old = out.split("🧠mind→", 1)[1].split()[0]
    run(regrow(bucket_id=old, text=VIEW + " Again.", v=0.6, a=0.3, mode="supplement"))
    new = _disk(tmp_path, old)["superseded_by"]
    assert _disk(tmp_path, new)["sources"] == _disk(tmp_path, old)["sources"]


def test_trace_appends_sources_and_their_quoted_lines(store, tmp_path):
    _out, bid = run(_event(sources=[REC]))
    more = {**REC, "id": "m_0150", "fingerprint": None}
    out = run(trace(bucket_id=bid, sources_append=[more]))
    assert out.startswith("已修改记忆桶"), out
    meta = _disk(tmp_path, bid)
    assert [r["id"] for r in meta["sources"]] == ["m_0142", "m_0150"]
    assert _quoted(meta) == [SRC, "lento:home/private:U#m_0150"]
    _withdraw(store, source="lento:home/private:U#m_0151")
    out = run(trace(bucket_id=bid, sources_append=[{**more, "id": "m_0151"}]))
    assert "撤回" in out and len(_disk(tmp_path, bid)["sources"]) == 2


def test_trace_links_a_bare_line_the_entry_already_had(store, tmp_path):
    _out, bid = run(_event(from_=["m_0142"]))
    run(trace(bucket_id=bid, sources_append=[REC]))
    assert _quoted(_disk(tmp_path, bid)) == [SRC]


# ───────────────────────── reading by id ─────────────────────────

def test_recall_by_id_marks_a_withdrawn_or_revised_source(store, tmp_path):
    _out, bid = run(_event(sources=[{**REC, "revision": "1"}]))
    shown = run(R.recall_core(when="", room="", tag="", query=bid))
    assert SRC + "@1" in shown and "⚠️" not in shown.split("来源:", 1)[1]
    run(store.sources.apply_change({"change_id": "r1", "source": SRC, "kind": "revised",
                                    "host_seq": 1, "revision": "2"}))
    _withdraw(store, seq=2)
    block = run(R.recall_core(when="", room="", tag="", query=bid)).split("来源:", 1)[1]
    assert "⚠️撤回" in block and "@2" in block


# ───────────────────────── write keys ─────────────────────────

def test_the_same_write_key_returns_the_first_reply_and_writes_once(store, tmp_path):
    with S.write_key_scope(S.write_key("turn-1", 1)):
        first, bid = run(_event(sources=[REC]))
        again, _ = run(_event(sources=[REC]))
    assert bid and again == first and len(_files(tmp_path)) == 1
    claim = store.sources.claimed("turn-1#1")
    assert claim["ids"] == [bid] and claim["op"] == "grow"


def test_a_resend_after_a_restart_gets_the_same_answer(store, tmp_path, monkeypatch):
    with S.write_key_scope("turn-2#1"):
        first, _bid = run(_event("Something said once.", sources=[REC]))
    _install(tmp_path, monkeypatch)     # a fresh store and registry on the same library
    with S.write_key_scope("turn-2#1"):
        again, _ = run(_event("Something said once.", sources=[REC]))
    assert again == first and len(_files(tmp_path)) == 1


def test_another_ordinal_is_another_write(store, tmp_path):
    with S.write_key_scope("turn-3#1"):
        run(_event("One thing."))
    with S.write_key_scope("turn-3#2"):
        out, bid = run(_event("Another thing."))
    assert bid and len(_files(tmp_path)) == 2


def test_a_refusal_claims_nothing(store, tmp_path):
    with S.write_key_scope("turn-4#1"):
        out, bid = run(_event(sources=[{"system": "lento"}]))
        assert not bid
        out, bid = run(_event(sources=[REC]))
    assert bid, "the corrected call under the same key still writes"


def test_without_a_key_nothing_is_deduplicated_by_key(store, tmp_path):
    assert S.current_write_key() is None
    run(_event("First wording."))
    run(_event("Second wording."))
    assert len(_files(tmp_path)) == 2
    assert not store.sources.keys_path.exists()


def test_every_write_tool_goes_through_the_key(store, tmp_path):
    async def go():
        a = await store.create("First view.", room="MIND/VIEWS")
        b = await store.create("Second view.", room="MIND/VIEWS")
        with S.write_key_scope("turn-5#1"):
            f1 = await fold(text="Both.", room="MIND/VIEWS", v=0.5, a=0.3, cover=[a, b])
            f2 = await fold(text="Both.", room="MIND/VIEWS", v=0.5, a=0.3, cover=[a, b])
        gist = f1.split("▣gist→", 1)[1].split()[0]
        with S.write_key_scope("turn-5#2"):
            r1 = await regrow(bucket_id=gist, text="Both, said better.", v=0.5, a=0.3,
                              mode="supplement")
            r2 = await regrow(bucket_id=gist, text="Both, said better.", v=0.5, a=0.3,
                              mode="supplement")
        with S.write_key_scope("turn-5#3"):
            m1 = await grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                            from_=[a])
            m2 = await grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                            from_=[a])
        with S.write_key_scope("turn-5#4"):
            t1 = await trace(bucket_id=a, sources_append=[REC])
            t2 = await trace(bucket_id=a, sources_append=[{**REC, "id": "m_9"}])
        return (f1, f2), (r1, r2), (m1, m2), (t1, t2)
    for first, second in run(go()):
        assert first == second
    assert [r["id"] for r in _disk(tmp_path, run(_first_view(store)))["sources"]] == ["m_0142"]
    # two seeded + gist + its new version + the mind
    assert len(_files(tmp_path)) == 5


async def _first_view(store):
    for b in await store.list_all(include_archive=True):
        if b["content"].strip() == "First view.":
            return b["id"]
