# -*- coding: utf-8 -*-
"""
tests/test_backfill_fills_blanks.py — the backfill's one call, and what it may write.

Who fills which slot: the main model writes what cannot be read off the sentence; the
side model reads what can; code works out what has a known origin (a date from the copied
phrase). So the backfill only ever fills blanks, lists every slot it filled in
`backfilled`, and writes the names table in exactly two ways. A bad answer — not JSON, or
a names part in the wrong shape — writes nothing of its own, and the body is never touched.

The side model is a stub: no test here calls a real model.
"""

import asyncio
import json

import pytest

from core import dehydrator as D
from tools import _runtime as rt
from tools import _subjects as S
from tools.grow import rooms_path as R

BODY = "答应小王下周一两点一起去看《沙丘》，在老地方见。"
# 18:30 UTC on the 6th is Wednesday the 7th on the local calendar.
CREATED = "2026-10-06T18:30:00"


class StubModel:
    """The side model: one canned answer per call (a dict is sent as JSON), or a raise."""

    def __init__(self, *answers, raises=False):
        self.answers = list(answers)
        self.raises = raises
        self.requests: list[tuple[str, str]] = []

    async def _chat(self, system, user, max_tokens=0, temperature=0.0):
        self.requests.append((system, user))
        if self.raises:
            raise RuntimeError("side model is down")
        if not self.answers:
            return ""
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


class DictStore:
    """One entry held in memory; update() applies like the real store (None removes)."""

    embedding_engine = None

    def __init__(self, meta: dict, body: str = BODY):
        self.meta = {"id": "b1", "tags": [], **meta}
        self.body = body
        self.updates: list[dict] = []

    async def get(self, bucket_id):
        return {"id": bucket_id, "content": self.body, "metadata": dict(self.meta)}

    async def update(self, bucket_id, revise=None, **kwargs):
        if revise is not None:
            kwargs = {**revise(dict(self.meta)), **kwargs}
            if not kwargs:
                return True
        self.updates.append(kwargs)
        for k, v in kwargs.items():
            if v is None:
                self.meta.pop(k, None)
            else:
                self.meta[k] = v
        return True

    def written(self) -> dict:
        out: dict = {}
        for u in self.updates:
            out.update(u)
        return out


class Lines:
    def __init__(self):
        self.lines: list[str] = []

    def _record(self, msg, *a):
        self.lines.append(str(msg) % a if a else str(msg))

    warning = info = debug = error = _record


TABLE = """\
# a hand-written table
小李:
  instance_of: 人
小张:
  - 张哥
"""


@pytest.fixture
def table(tmp_path, monkeypatch):
    path = tmp_path / "aliases.yaml"
    path.write_text(TABLE, encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(path))
    monkeypatch.setattr(S, "_cache", None)
    return path


@pytest.fixture
def backfill(monkeypatch, table):
    """backfill(meta, *answers, raises=False, body=BODY) -> (store, model, log lines)."""
    def go(meta, *answers, raises=False, body=BODY):
        store = DictStore(meta, body)
        model = StubModel(*answers, raises=raises)
        log = Lines()
        monkeypatch.setattr(rt, "dehydrator", model)
        monkeypatch.setattr(rt, "bucket_mgr", store)
        monkeypatch.setattr(rt, "logger", log)
        asyncio.run(R._backfill_one("b1", body, "mind" if "MIND" in meta.get("room", "") else "event"))
        return store, model, log.lines
    return go


def event(**fields) -> dict:
    return {"room": "EVENT/SELF", "created": CREATED, "name": "2026-10-07 02-30-00",
            "domain": ["未分类"], **fields}


def mind(**fields) -> dict:
    return {**event(**fields), "room": "MIND/VIEWS"}


# ───────────────────────── fill-only ─────────────────────────

def test_the_main_models_name_and_summary_survive(backfill):
    store, _, _ = backfill(
        event(name="主模型起的名字", summary="主模型写的摘要"),
        {"name": "侧模型的名字", "summary": "侧模型的摘要", "tags": ["沙丘"]})
    written = store.written()
    assert "name" not in written and "summary" not in written
    assert store.meta["name"] == "主模型起的名字"
    assert store.meta["summary"] == "主模型写的摘要"


def test_blanks_are_filled_and_backfilled_lists_exactly_those(backfill):
    store, _, _ = backfill(
        event(subjects=["小李"]),
        {"name": "约看沙丘", "summary": "约好下周一看沙丘", "tags": ["沙丘", "老地方"],
         "aliases": ["电影", "约会"], "domain": ["影视"],
         "subjects": [{"name": "小王", "kind": "人"}]})
    meta = store.meta
    assert meta["name"] == "约看沙丘" and "name_source" not in meta
    assert meta["summary"] == "约好下周一看沙丘"
    assert meta["aliases"] == ["电影", "约会"]
    assert meta["domain"] == ["影视"]
    assert meta["subjects"] == ["小李", "小王"], "the main model's subjects stay, new names join"
    assert meta["tags"] == ["沙丘", "老地方"]
    assert sorted(meta["backfilled"]) == sorted(
        ["name", "summary", "aliases", "domain", "subjects", "tags"])


def test_a_domain_the_main_model_set_is_kept(backfill):
    store, _, _ = backfill(event(domain=["工作"]), {"summary": "s", "domain": ["影视"]})
    assert store.meta["domain"] == ["工作"]
    assert "domain" not in store.meta["backfilled"]


def test_backfilled_adds_to_what_was_listed_before(backfill):
    store, _, _ = backfill(event(backfilled=["bound"]), {"summary": "s"})
    assert store.meta["backfilled"][0] == "bound"
    assert "summary" in store.meta["backfilled"]


def test_nothing_new_to_fill_writes_no_backfilled(backfill):
    store, _, _ = backfill(event(name="名字", summary="摘要", aliases=["a"]),
                           {"name": "别的", "summary": "别的", "aliases": ["b"]})
    assert store.updates == []


# ───────────────────────── the slots read off the sentence ─────────────────────────

def test_bound_is_filled_only_on_a_telic_entry(backfill):
    store, _, _ = backfill(event(), {"summary": "s", "bound": ["小王"]})
    assert "bound" not in store.meta
    store, _, _ = backfill(event(direction_of_fit="telic"), {"summary": "s", "bound": ["小王"]})
    assert store.meta["bound"] == ["小王"]
    assert "bound" in store.meta["backfilled"]


def test_bound_the_main_model_set_is_kept(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic", bound=["小李"]),
                           {"summary": "s", "bound": ["小王"]})
    assert store.meta["bound"] == ["小李"]


def test_evidential_is_never_written_on_an_event(backfill):
    store, _, _ = backfill(event(), {"summary": "s", "evidential": "assumption"})
    assert "evidential" not in store.meta
    store, _, _ = backfill(mind(), {"summary": "s", "evidential": "assumption"})
    assert store.meta["evidential"] == "assumption"


def test_internally_generated_only_on_an_event(backfill):
    store, _, _ = backfill(mind(), {"summary": "s", "internally_generated": True})
    assert "internally_generated" not in store.meta
    store, _, _ = backfill(event(), {"summary": "s", "internally_generated": True})
    assert store.meta["internally_generated"] is True


def test_looks_like_a_promise_leaves_a_mark_and_telic_alone(backfill):
    store, _, _ = backfill(event(), {"summary": "s", "looks_like_promise": True})
    assert store.meta["looks_like_promise"] is True
    assert "looks_like_promise" in store.meta["backfilled"]
    assert all("direction_of_fit" not in u for u in store.updates), "telic is the main model's"


def test_a_telic_entry_gets_no_promise_mark(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic"),
                           {"summary": "s", "looks_like_promise": True})
    assert "looks_like_promise" not in store.meta


def test_cue_phrasings_fill_only_an_empty_list(backfill):
    store, _, _ = backfill(event(cue={"condition": "考完试", "phrasings": []}),
                           {"summary": "s", "cue_phrasings": ["考完了", "终于考完"]})
    assert store.meta["cue"] == {"condition": "考完试", "phrasings": ["考完了", "终于考完"]}
    assert "cue.phrasings" in store.meta["backfilled"]
    store, _, _ = backfill(event(cue={"condition": "考完试", "phrasings": ["出成绩"]}),
                           {"summary": "s", "cue_phrasings": ["考完了"]})
    assert store.meta["cue"]["phrasings"] == ["出成绩"]
    store, _, _ = backfill(event(), {"summary": "s", "cue_phrasings": ["考完了"]})
    assert "cue" not in store.meta, "no condition, no cue: a cue is the main model's to declare"


# ───────────────────────── dates: the model copies, code converts ─────────────────────────

def test_a_relative_date_is_read_against_the_day_the_memory_was_written(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic"),
                           {"summary": "s", "time": {"phrase": "下周一两点", "yearly": False}})
    assert store.meta["when"] == "2026-10-12T14:00+08:00"
    assert "when" in store.meta["backfilled"]


def test_a_when_already_there_is_kept(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic", when="2026-12-01"),
                           {"summary": "s", "time": {"phrase": "后天"}})
    assert store.meta["when"] == "2026-12-01"


def test_a_lived_event_is_not_dated_in_the_future(backfill):
    store, _, _ = backfill(event(), {"summary": "s", "time": {"phrase": "明天"}})
    assert "when" not in store.meta
    store, _, _ = backfill(event(), {"summary": "s", "time": {"phrase": "昨天"}})
    assert store.meta["when"] == "2026-10-06"


def test_a_promise_is_not_dated_in_the_past(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic"),
                           {"summary": "s", "time": {"phrase": "昨天"}})
    assert "when" not in store.meta


def test_a_yearly_date_sets_recurrence(backfill):
    store, _, _ = backfill(event(), {"summary": "s",
                                     "time": {"phrase": "每年8月7号", "yearly": True}})
    assert store.meta["when"] == "2026-08-07"
    assert store.meta["recurrence"] == "FREQ=YEARLY"
    assert {"when", "recurrence"} <= set(store.meta["backfilled"])


def test_an_unreadable_phrase_is_not_guessed(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic"),
                           {"summary": "s", "time": {"phrase": "过几天"}})
    assert "when" not in store.meta


def test_the_side_models_own_date_is_used_only_when_well_formed(backfill):
    store, _, _ = backfill(event(direction_of_fit="telic"),
                           {"summary": "s", "time": {"phrase": "过几天", "absolute": "2026-10-20"}})
    assert store.meta["when"] == "2026-10-20"
    assert "when" in store.meta["backfilled"]
    store, _, log = backfill(event(direction_of_fit="telic"),
                             {"summary": "s", "time": {"phrase": "过几天", "absolute": "2026-13-40"}})
    assert "when" not in store.meta
    assert store.meta["summary"] == "s", "a bad time part does not cost the rest"


def test_no_created_day_means_no_date(backfill):
    meta = event(direction_of_fit="telic")
    del meta["created"]
    store, _, _ = backfill(meta, {"summary": "s", "time": {"phrase": "后天"}})
    assert "when" not in store.meta


def test_the_request_carries_the_local_day_and_what_is_already_there(backfill):
    _, model, _ = backfill(event(direction_of_fit="telic", name="主模型起的名字"), {"summary": "s"})
    system, user = model.requests[0]
    assert "2026-10-07（星期三）" in user
    assert "想让它发生：是" in user
    assert "name=主模型起的名字" in user
    assert BODY in user
    assert "{kinds}" not in system and "游戏" in system


# ───────────────────────── the names table ─────────────────────────

def _table(path) -> str:
    return path.read_text(encoding="utf-8")


def test_the_table_gains_new_names_and_missing_kinds_only(backfill, table):
    backfill(event(), {"summary": "s", "subjects": [
        {"name": "小王", "kind": "人"},        # not in the table: added with its kind
        {"name": "张哥", "kind": "人"},        # an alias of a kindless entry: that entry gets it
        {"name": "小李", "kind": "游戏"},      # already a person: never changed
        {"name": "沙丘", "kind": "影视"},
        {"name": "老周", "kind": ""},          # no kind: nothing to write
    ]})
    S._cache = None
    assert S.kind_of("小王") == "人"
    assert S.kind_of("小张") == "人"
    assert S.kind_of("小李") == "人"
    assert S.kind_of("沙丘") == "影视"
    assert S.record_of("老周") is None
    assert _table(table).startswith("# a hand-written table\n"), "the owner's comments stay"


def test_a_malformed_kind_leaves_the_table_and_the_subjects_alone(backfill, table):
    before = _table(table)
    store, _, log = backfill(event(), {"summary": "s", "subjects": [
        {"name": "小王", "kind": "人"},
        {"name": "沙丘", "kind": "电影院里放的那种东西"}]})
    assert _table(table) == before
    assert "subjects" not in store.meta
    assert store.meta["summary"] == "s", "the rest of the answer still counts"
    assert sum("subjects" in line for line in log) == 1, "said once"


@pytest.mark.parametrize("subjects", [
    [{"name": "小王", "kind": 1}],
    ["小王"],
    [{"name": "小王", "kind": "人", "role": "朋友"}],
    [{"name": "", "kind": "人"}],
    "小王",
])
def test_any_bad_shape_in_the_names_part_writes_nothing_to_the_table(backfill, table, subjects):
    before = _table(table)
    store, _, _ = backfill(event(), {"summary": "s", "subjects": subjects})
    assert _table(table) == before
    assert "subjects" not in store.meta


# ───────────────────────── failure keeps the body ─────────────────────────

def test_malformed_json_writes_nothing_of_the_models(backfill, table):
    before = _table(table)
    store, model, _ = backfill(event(direction_of_fit="telic"),
                               "这不是 JSON {", '{"summary": ')
    assert len(model.requests) == 2, "asked once more, then given up"
    written = store.written()
    for field in ("tags", "aliases", "subjects", "domain", "bound", "when",
                  "looks_like_promise", "evidential", "internally_generated", "cue"):
        assert field not in written
    # what is written is the stamped quotes of the body, nothing the model said
    assert written["name_source"] == "fallback" and written["summary_source"] == "fallback"
    assert BODY.startswith(written["summary"])
    assert all("content" not in u for u in store.updates)
    assert _table(table) == before


def test_a_raising_side_model_keeps_the_body_and_the_table(backfill, table):
    before = _table(table)
    store, _, log = backfill(event(direction_of_fit="telic"), raises=True)
    assert all("content" not in u for u in store.updates)
    assert "summary" not in store.meta, "absent: the sweep comes back for it"
    assert _table(table) == before
    assert any("b1" in line for line in log)


def test_the_real_store_keeps_the_body_and_takes_every_fill(tmp_path, monkeypatch, table):
    from core.bucket_manager import BucketManager
    import frontmatter

    store = BucketManager({"buckets_dir": str(tmp_path / "lib")})
    bid = asyncio.run(store.create(BODY, tags=[], room="EVENT/SELF",
                                   cue={"condition": "小王有空"}))
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "logger", Lines())
    monkeypatch.setattr(rt, "dehydrator", StubModel({
        "name": "约看沙丘", "summary": "约好看沙丘", "tags": ["沙丘"],
        "looks_like_promise": True, "cue_phrasings": ["小王说他有空了"]}))
    asyncio.run(R._backfill_one(bid, BODY, "event"))
    post = frontmatter.load(store._find_bucket_file(bid))
    assert post.content == BODY
    meta = post.metadata
    assert meta["looks_like_promise"] is True
    assert meta["cue"]["phrasings"] == ["小王说他有空了"]
    assert sorted(meta["backfilled"]) == sorted(
        ["name", "summary", "tags", "looks_like_promise", "cue.phrasings"])


# ───────────────────────── the parser on its own ─────────────────────────

def test_unknown_keys_are_dropped_and_feelings_are_never_read():
    ans = D.parse_backfill('{"summary": "s", "valence": 0.9, "mood": "happy"}', BODY)
    assert ans.summary == "s"
    assert not hasattr(ans, "valence") and not hasattr(ans, "mood")


def test_a_scene_tag_must_be_in_the_body_and_a_subject_is_not_also_a_tag():
    ans = D.parse_backfill(json.dumps({
        "tags": ["沙丘", "电影院", "小王"], "aliases": ["小王", "电影"],
        "subjects": [{"name": "小王", "kind": "人"}]}, ensure_ascii=False), BODY)
    assert ans.tags == ["沙丘"]
    assert ans.aliases == ["电影"]


@pytest.mark.parametrize("raw", ["", "[]", "{}", "not json", '{"summary": 3}'])
def test_an_answer_with_nothing_usable_is_none(raw):
    assert D.parse_backfill(raw, BODY) is None


def test_a_bad_part_costs_only_itself():
    ans = D.parse_backfill(json.dumps({
        "summary": "s", "time": "明天", "evidential": "certain", "looks_like_promise": "yes",
        "cue_phrasings": ["好" * 201, "考完了"]}, ensure_ascii=False), BODY)
    assert ans.summary == "s"
    assert ans.time_phrase == "" and ans.evidential == "" and ans.looks_like_promise is False
    assert ans.cue_phrasings == ["考完了"], "an over-long phrasing is dropped, not cut"
    assert set(ans.problems) == {"time", "evidential", "looks_like_promise"}


def test_a_kind_the_table_already_uses_is_accepted(table):
    table.write_text("Detroit:\n  instance_of: 角色扮演\n", encoding="utf-8")
    S._cache = None
    ans = D.parse_backfill(json.dumps({"subjects": [{"name": "Connor", "kind": "角色扮演"}]},
                                      ensure_ascii=False), BODY)
    assert ans.subjects == [("Connor", "角色扮演")]
