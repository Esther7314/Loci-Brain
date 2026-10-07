# -*- coding: utf-8 -*-
"""
tests/test_sources_tools.py — what the write tools do with `sources` and write keys.

Through the tools' dispatch against a real BucketManager, read back from the files:
a record lands with its quoted prov line; a bare host id in `from` is linked to the
record with that id, completed from Loci's registered line orders or the host's one
container (the record says which), or refused naming it — never naming a container the
caller may not reach (an entry that already holds one says on read that it cannot be
traced); the registry's withdrawn and deleted refuse the write and its
unreadable is noted; the same delivery on another memory is a hint, never a block;
regrow carries records and leaves a withdrawn one behind; trace appends. A write key
answers a resend with the first reply, also after a restart; no key, no dedup by key.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _sources as S
from core import scope as SC
from core.bucket_manager import BucketManager
from core import runtime as rt
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


def test_a_bare_line_id_with_no_record_is_refused_naming_it(store, tmp_path):
    # Criterion: a quoted line naming no container would be one reads cannot fetch and a
    # withdrawal cannot reach; the owner's call has no host container to complete it with.
    out, bid = run(_event(from_=["m_0931", "m_0932"]))
    assert not bid and "只写了编号 m_0931、m_0932" in out and "Loci 不猜" in out, out
    assert _files(tmp_path) == []


def test_regrow_refuses_a_new_bare_line_id_and_leaves_the_old_version(store, tmp_path):
    old = run(grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3, sources=[REC]))
    old = old.split("🧠mind→", 1)[1].split()[0]
    out = run(regrow(bucket_id=old, text=VIEW + " Rainy ones too.", v=0.6, a=0.3,
                     mode="supplement", from_=["m_0009"]))
    assert "只写了编号 m_0009" in out, out
    assert not _disk(tmp_path, old).get("superseded_by")


GROUP = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
GROUP_SCOPE = ('{"v": 1, "entry": {"system": "telegram", "instance": "bot-a", '
               '"container": "group:G"}, "venue": "group", "audience": ["user:U"], '
               '"grant": [{"system": "telegram", "instance": "bot-a", "container": "group:G"}]}')
ONE_ROOM = SC.Host("group-bot", max_grant=(S.Place(**GROUP),), token="group-key")


async def _as_host(host, key, **kw):
    with SC.request_scope(SC.RequestScope.resolve(host, GROUP_SCOPE)), S.write_key_scope(key):
        return await _event(**kw)


def test_a_bare_line_id_is_completed_from_the_hosts_one_container(store, tmp_path):
    out, bid = run(_as_host(ONE_ROOM, S.write_key("t-1", 1, ONE_ROOM.name), from_=["m_0003"]))
    assert bid, out
    meta = _disk(tmp_path, bid)
    text = "telegram:bot-a/group:G#m_0003"
    assert _quoted(meta) == [text]
    [rec] = meta["sources"]
    assert (rec["system"], rec["instance"], rec["container"], rec["id"]) == (
        "telegram", "bot-a", "group:G", "m_0003")
    assert rec["completed_from"] == "host_scope"
    assert f"m_0003 只写了编号，按这个宿主唯一的容器补全成 {text}" in out


PRIVATE = {"system": "lento", "instance": "home", "container": "private:U"}


def _register(store, where, *ids):
    store.sources.record_order(where, list(ids))


def test_the_owner_citing_a_registered_line_gets_it_completed_from_the_registry(store, tmp_path):
    _register(store, PRIVATE, "m_0002", "m_0003", "m_0004")
    out, bid = run(_event(from_=["m_0003"]))
    assert bid, out
    meta = _disk(tmp_path, bid)
    text = "lento:home/private:U#m_0003"
    assert _quoted(meta) == [text]
    [rec] = meta["sources"]
    assert S.record_id(rec) == S.SourceId("lento", "home", "private:U", "m_0003")
    assert rec["completed_from"] == "registry"
    assert f"m_0003 只写了编号，按 Loci 登记过的那一处补全成 {text}" in out


def test_a_line_registered_in_two_containers_is_refused_naming_them(store, tmp_path):
    _register(store, PRIVATE, "m_0003")
    _register(store, GROUP, "m_0003")
    out, bid = run(_event(from_=["m_0003"]))
    assert not bid and "编号 m_0003 在 2 个容器里都登记过" in out, out
    assert "lento:home/private:U" in out and "telegram:bot-a/group:G" in out
    assert _files(tmp_path) == []


def test_a_ceilinged_host_never_learns_of_a_container_past_its_ceiling(store, tmp_path):
    # Criterion: a line registered only outside the ceiling gets the generic refusal, the
    # same words as an id registered nowhere — no container, no count.
    _register(store, PRIVATE, "m_0003")
    key = S.write_key("t-1", 1, "instance-bot")
    wide = SC.Host("instance-bot", max_grant=(S.Place("telegram", "bot-a"),), token="k")
    out, bid = run(_as_host(wide, key, from_=["m_0003"]))
    assert not bid and "只写了编号 m_0003" in out, out
    assert "lento" not in out and "private:U" not in out and "登记过" not in out
    assert _files(tmp_path) == []


def test_under_a_ceiling_only_reachable_registrations_count(store, tmp_path):
    _register(store, PRIVATE, "m_0003")
    _register(store, GROUP, "m_0003")
    key = S.write_key("t-1", 1, "instance-bot")
    wide = SC.Host("instance-bot", max_grant=(S.Place("telegram", "bot-a"),), token="k")
    out, bid = run(_as_host(wide, key, from_=["m_0003"]))
    assert bid, out
    assert "private:U" not in out and "2 个容器" not in out
    [rec] = _disk(tmp_path, bid)["sources"]
    assert (rec["container"], rec["completed_from"]) == ("group:G", "registry")


def test_a_writer_cannot_claim_completed_from(store, tmp_path):
    out, bid = run(_event(sources=[{**REC, "completed_from": "registry"}]))
    assert not bid and "completed_from" in out and _files(tmp_path) == []


def test_completed_from_is_not_identity():
    plain = {**REC, "fingerprint": None, "fingerprint_by": None}
    marked = {**plain, "completed_from": "registry"}
    assert S.record_id(plain) == S.record_id(marked)
    assert S.record_string(plain) == S.record_string(marked)
    assert S.same_reference(plain, marked)
    assert len(S.normalize_sources([plain, marked])) == 1
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources([{**plain, "completed_from": "the model"}])


@pytest.mark.parametrize("host, key", [
    (ONE_ROOM, None),                                            # no write key
    (ONE_ROOM, S.write_key("t-1", 1, "another-host")),           # a key of another host
    (SC.Host("instance-bot", max_grant=(S.Place("telegram", "bot-a"),), token="k"),
     S.write_key("t-1", 1, "instance-bot")),                     # a ceiling wider than a room
    (SC.Host(SC.LEGACY, scope_mode=SC.OPEN), S.write_key("t-1", 1, SC.LEGACY)),   # open
])
def test_a_bare_line_id_is_not_completed_without_one_container_of_this_host(
        store, tmp_path, host, key):
    out, bid = run(_as_host(host, key, from_=["m_0003"]))
    assert not bid and "只写了编号 m_0003" in out, out
    assert _files(tmp_path) == []


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


def _old_bare_entry(store) -> str:
    """An entry written before a bare quoted line was refused."""
    return run(store.create("We talked it over at breakfast.", room="EVENT/WORLD",
                            prov=[{"rel": "wasQuotedFrom", "target": "m_0142"}]))


def test_trace_links_a_bare_line_the_entry_already_had(store, tmp_path):
    bid = _old_bare_entry(store)
    run(trace(bucket_id=bid, sources_append=[REC]))
    assert _quoted(_disk(tmp_path, bid)) == [SRC]


def test_an_old_bare_line_says_on_read_that_it_cannot_be_traced(store, tmp_path):
    bid = _old_bare_entry(store)
    shown = run(R.recall_core(when="", room="", tag="", query=bid))
    assert "有一根引原话的线只写了编号（m_0142），追不到来源" in shown, shown
    run(trace(bucket_id=bid, sources_append=[REC]))
    assert "只写了编号" not in run(R.recall_core(when="", room="", tag="", query=bid))


# ───────────────────────── a run of lines ─────────────────────────

RUN = {**REC, "through": "m_0160"}
RUN_SRC = SRC + "..m_0160"


def _order(store):
    """The host registers the lines of the chat these runs are cut from."""
    store.sources.record_order({"system": "lento", "instance": "home", "container": "private:U"},
                               [f"m_{i:04d}" for i in range(140, 176)])


def test_a_run_whose_lines_were_never_registered_is_refused_as_a_basis(store, tmp_path):
    out, bid = run(_event(sources=[RUN]))
    assert not bid and RUN_SRC in out and "POST /api/v2/source/lines" in out
    out, bid = run(_event(from_=[RUN_SRC]))
    assert not bid and _files(tmp_path) == []
    _order(store)
    _out, bid = run(_event(sources=[RUN]))
    assert bid


def test_a_run_is_stored_and_quoted_by_its_range_form(store, tmp_path):
    _order(store)
    _out, bid = run(_event(sources=[RUN]))
    meta = _disk(tmp_path, bid)
    assert meta["sources"][0]["through"] == "m_0160" and _quoted(meta) == [RUN_SRC]
    out, other = run(_event("Something else said that morning.", from_=[SRC + "..m_0170"]))
    assert other and _disk(tmp_path, other)["sources"][0]["through"] == "m_0170", out


def test_a_run_whose_first_line_is_withdrawn_refuses_the_write(store, tmp_path):
    _order(store)
    _withdraw(store)
    out, bid = run(_event(sources=[RUN]))
    assert not bid and RUN_SRC in out and "撤回" in out
    out, bid = run(_event(from_=[RUN_SRC]))
    assert not bid and "撤回" in out and _files(tmp_path) == []


def test_the_same_run_is_a_hint_and_another_run_from_its_first_line_is_not(store, tmp_path):
    _order(store)
    _out, first = run(_event(sources=[RUN]))
    out, second = run(_event("The second thing said in that talk.", sources=[RUN]))
    assert second and f"这条来源已经记过 {first}" in out
    out, third = run(_event("A third thing.", sources=[{**RUN, "through": "m_0150"}]))
    assert third and "已经记过" not in out


# ───────────────────────── reading by id ─────────────────────────

def test_recall_by_id_marks_a_withdrawn_or_revised_source(store, tmp_path):
    _out, bid = run(_event(sources=[{**REC, "revision": "1"}]))
    shown = run(R.recall_core(when="", room="", tag="", query=bid))
    assert SRC + "@1" in shown and "⚠️" not in shown.split("来源:", 1)[1]
    run(store.sources.apply_change({"change_id": "r1", "source": SRC, "kind": "revised",
                                    "host_seq": 1, "revision": "2"}))
    block = run(R.recall_core(when="", room="", tag="", query=bid)).split("来源:", 1)[1]
    assert "已改到 @2" in block
    # Withdrawn in the registry: the read gate keeps the memory off from that moment,
    # before any record is written on it — the body is not shown, its sources neither.
    _withdraw(store, seq=2)
    shown = run(R.recall_core(when="", room="", tag="", query=bid))
    assert "依据的来源被撤回或删除了" in shown and "来源:" not in shown


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
