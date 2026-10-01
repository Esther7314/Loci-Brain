# -*- coding: utf-8 -*-
"""CONTRACT: when the tagging model fails, the body is already safe and nothing is invented.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    Writing a memory happens in two moves. The body goes to disk immediately, by itself,
    with neutral placeholder metadata. Then a background pass asks a side model, in one
    call, for tags, subjects, a domain, a name and a summary, and writes those on top.

    The second move can fail — an expired key, a rate limit, a model that returns nothing,
    a network that is not there. The contract is about what the failure is allowed to cost:

        the body                 never at risk; it was on disk before the model was asked
        tags / subjects          left ABSENT, never filled with a guess
        summary                  falls back to the opening of the body — the caller's own
                                 words, quoted, not a model's paraphrase of them
        name                     same, since 2026-08-20 — and both fallbacks are STAMPED

    🔴 THE 2026-08-20 AMENDMENT (name moved lines in that table)
        `name` used to sit on the "left absent" row. It was moved on her decision, and
        the reasoning is worth keeping because the two rows are not the same kind of
        thing at all. "Absent" is honest when the alternative would be a guess. But a
        nameless bucket is not absent — it is named after the second it was born in, a
        row of digits, and that is what shows up in every list read by eye.

        Her words: 「可以兜底 但是能不能做个记号 比如说谁没打标是兜底的 然后不是正好
        也要做一个一键打标的按钮嘛」. The second half is the load-bearing half. A
        fallback is invisible **by construction**: it fills the field in, so every
        "this one is unfinished" check stops matching, and nothing ever comes back. So
        the price of falling back is a stamp — `name_source: fallback`,
        `summary_source: fallback` — present exactly while a stand-in is standing in,
        and cleared the moment the model supplies the real thing.

        The line that did NOT move: nothing is invented. A fallback quotes the caller's
        own body verbatim. The distinction this whole file is about is guess vs. quote,
        and a stamped quote is on the right side of it.

    The dangerous version of this code is not one that crashes. It is one that helpfully
    writes `tags: []` and `subjects: []` when the model fails, because an empty list is
    indistinguishable from "the model looked and found nothing" — and the memory is then
    permanently invisible to every search that goes through tags, with no sign anything
    went wrong and nothing left to retry against.

WHY THE FAKE RUNTIME
    `_backfill_one` reaches for the global runtime for its model, its store and its logger.
    So the runtime is faked rather than the function reshaped. The model is one `_chat`:
    tagging and the summary are a single call, and the fake answers it with JSON.
"""
import asyncio
import json

import pytest

from tools import _runtime as rt
from tools.grow import rooms_path as R

BODY = "She said the panel stays in Chinese and the comments go. That is the whole rule."


class FakeDehydrator:
    """Stands in for the side model. Each knob is one way the real thing fails; an answer
    is a dict (sent back as JSON) or a raw string, one per call."""

    def __init__(self, *, answers=None, raises=False, has_chat=True):
        self.answers = list(answers or [])
        self.raises = raises
        self.chat_calls = 0
        if not has_chat:
            self._chat = None

    async def _chat(self, system, user, max_tokens=0, temperature=0.0):
        self.chat_calls += 1
        if self.raises:
            raise RuntimeError("upstream 401: your API key expired")
        if not self.answers:
            return ""
        answer = self.answers.pop(0)
        return answer if isinstance(answer, str) else json.dumps(answer, ensure_ascii=False)


class FakeStore:
    """Records what backfill tried to write, and can be told to fail the write."""

    def __init__(self, *, existing_tags=None, update_raises=False):
        self.existing_tags = list(existing_tags or [])
        self.update_raises = update_raises
        self.updates: list[dict] = []
        self.embedding_engine = None   # off: similarity tagging is a separate concern

    async def get(self, bucket_id):
        return {"id": bucket_id, "content": BODY,
                "metadata": {"id": bucket_id, "tags": list(self.existing_tags)}}

    async def update(self, bucket_id, **kwargs):
        if self.update_raises:
            raise OSError("disk full")
        self.updates.append(kwargs)
        return True


class FakeLogger:
    def __init__(self):
        self.lines: list[str] = []

    def _record(self, msg, *a):
        self.lines.append(str(msg) % a if a else str(msg))

    warning = info = debug = error = _record


@pytest.fixture(autouse=True)
def names_table(tmp_path, monkeypatch):
    """A throwaway names table: the backfill may add names to it."""
    from tools import _subjects as S
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(tmp_path / "aliases.yaml"))
    monkeypatch.setattr(S, "_cache", None)


@pytest.fixture
def runtime(monkeypatch):
    """Install fakes on the shared runtime and take them off again afterwards."""
    def install(dehydrator=None, store=None):
        logger = FakeLogger()
        monkeypatch.setattr(rt, "dehydrator", dehydrator or FakeDehydrator())
        monkeypatch.setattr(rt, "bucket_mgr", store or FakeStore())
        monkeypatch.setattr(rt, "logger", logger)
        return logger
    return install


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── the model falls over ─────────────────────────

def test_the_model_raising_does_not_propagate(runtime):
    # Criterion: backfill runs detached, behind the call that already returned success to
    # the caller. An exception escaping here is an unhandled task exception nobody sees,
    # and the caller has already been told the memory was saved — which it was.
    d = FakeDehydrator(raises=True)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))       # must simply return


def test_nothing_is_invented_when_the_model_fails(runtime):
    # Criterion: THE assertion of this file. With no answer from the model, none of the
    # fields it would have filled may be written — not as empty, not as a placeholder.
    # An absent field can be retried; a field written empty looks answered forever.
    # `name` is no longer on this list (see the 2026-08-20 amendment at the top): it is
    # filled from the body, which is a quote, not a guess — and it is checked below.
    d = FakeDehydrator(raises=True)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))

    assert len(store.updates) == 1
    written = store.updates[0]
    for invented in ("tags", "subjects", "domain", "aliases"):
        assert invented not in written, f"{invented!r} was fabricated out of a failed model call"


def test_a_failed_model_is_written_down_somewhere(runtime):
    # Criterion: silence is the thing that makes this class of failure expensive. It does
    # not have to interrupt anyone, but it does have to be findable afterwards.
    d = FakeDehydrator(raises=True)
    logger = runtime(d)
    run(R._backfill_one("b1", BODY, "event"))
    assert any("b1" in line for line in logger.lines), \
        "a bucket whose tagging failed must be identifiable from the log"


def test_the_body_is_never_written_by_backfill(runtime):
    # Criterion: backfill only ever adds metadata. The body reached disk before the model
    # was asked, so any write of content here would be backfill overwriting the one thing
    # that was already safe — with a value derived from a model that just failed.
    d = FakeDehydrator(raises=True)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert all("content" not in u for u in store.updates)


def test_a_failed_write_does_not_propagate_either(runtime):
    # Criterion: the store can fail too (disk full, file locked). Same reasoning as the
    # model: the caller is long gone and the body is already safe.
    d = FakeDehydrator(answers=[{"tags": ["panel"], "summary": "s"}])
    runtime(d, FakeStore(update_raises=True))
    run(R._backfill_one("b1", BODY, "event"))


def test_the_unfinished_marker_survives_a_total_model_outage(runtime):
    # Criterion: model down AND no chat channel at all (an unconfigured deployment) —
    # nothing the model would have answered may be written, and above all the "unfinished"
    # marker `backfill_sweep` finds this bucket by, an ABSENT summary, has to survive.
    # Fill that in and the bucket drops out of the sweep and is never completed.
    #
    # 📌 This assertion used to read `store.updates == []`, which was the same criterion
    #    written in terms of the implementation of the day. Since 2026-08-20 there IS one
    #    honest thing to write with the model down — a stamped fallback name, quoted from
    #    the body — so "no write at all" would now fail while the thing it was protecting
    #    is untouched. It is spelled out as the marker itself instead.
    d = FakeDehydrator(raises=True, has_chat=False)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = store.updates[0] if store.updates else {}
    assert "summary" not in written, (
        "a summary written during a total outage clears the only marker that would have "
        "brought this bucket back for a second try")
    for invented in ("tags", "subjects", "domain", "aliases"):
        assert invented not in written


# ───────────────────────── the summary's own fallback ─────────────────────────

def test_summary_falls_back_to_the_callers_own_words(runtime):
    # Criterion: the fallback quotes the body rather than inventing a line. The summary is
    # the hook that makes a memory visible in a zoomed-out view, so an empty one hides it —
    # but a made-up one would be worse, because it would read as something she wrote.
    d = FakeDehydrator(answers=["", ""])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))

    summary = store.updates[0]["summary"]
    assert BODY.startswith(summary), "the fallback summary must be a prefix of the body, verbatim"
    assert d.chat_calls == 2, "an empty answer is retried exactly once before falling back"


def test_summary_is_absent_rather_than_empty_when_there_is_no_model_at_all(runtime):
    # Criterion: with no chat channel there is nothing to fall back *from* — the distinction
    # this file is about. Absent means `backfill_sweep` can still find this bucket and
    # finish the job once a model is configured.
    d = FakeDehydrator(answers=[{"tags": ["panel"]}], has_chat=False)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert "summary" not in store.updates[0]


def test_a_raising_chat_does_not_fall_back(runtime):
    # Criterion: an empty answer and a thrown exception are different. Empty means the model
    # answered and had nothing; a raise means it never answered, and the right record of
    # that is "not done yet", not a degraded stand-in that ends the retry.
    d = FakeDehydrator(raises=True)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert "summary" not in store.updates[0]
    assert d.chat_calls == 1, "a thrown error is not retried the way an empty answer is"


# ───────────────────────── the model works, and still does not overwrite ─────────────────────────

def test_existing_tags_are_merged_not_replaced(runtime):
    # Criterion: system tags land on a bucket between the write and the backfill. The model
    # knows nothing about them, so replacing the list drops them — and a dropped system tag
    # is a memory that stops belonging to the thing it belonged to, silently.
    d = FakeDehydrator(answers=[{"tags": ["comments", "panel"], "summary": "s"}])
    store = FakeStore(existing_tags=["__archive_fact__"])
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert store.updates[0]["tags"] == ["__archive_fact__", "comments", "panel"]


class FakeEmbeddings:
    enabled = True

    def __init__(self, hits):
        self.hits = hits

    async def search_similar(self, text, top_k=3):
        return list(self.hits)


class NeighbourStore(FakeStore):
    """One close neighbour, shaped as a thought so the mind branch accepts it too."""

    def __init__(self, **kw):
        super().__init__(**kw)
        self.embedding_engine = FakeEmbeddings([("neighbour001", 0.93)])

    async def get(self, bucket_id):
        b = await super().get(bucket_id)
        if bucket_id != "b1":
            b["metadata"]["room"] = "MIND/TRAITS"
        return b


def test_a_similarity_hint_goes_on_top_of_the_tags_already_there(runtime):
    # Criterion: the model gave nothing this round, so the similarity hint is the only tag
    # being written. A regrown entry is always close to the version it replaced, so this
    # path runs on nearly every regrow — and a hint written on its own replaces the list,
    # taking __gist__ and the profile page's tag with it, with no sign.
    d = FakeDehydrator(answers=[{"summary": "s"}])
    store = NeighbourStore(existing_tags=["__gist__", "__档案事实__"])
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert store.updates[0]["tags"] == ["__gist__", "__档案事实__", "疑似同件:neighb"]


def test_a_thought_close_to_an_old_view_gets_no_tag(runtime):
    # Criterion: a thought running into an old view is said in the write's own return
    # (core/_reconsolidation.py), not tagged in the background where nothing reads it.
    d = FakeDehydrator(answers=[{"summary": "s"}])
    store = NeighbourStore(existing_tags=["__gist__"])
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "mind"))
    assert "tags" not in store.updates[0]


class UnreadableStore(FakeStore):
    async def get(self, bucket_id):
        raise OSError("file is locked")


def test_tags_are_not_written_when_the_current_ones_cannot_be_read(runtime):
    # Criterion: without the current list, any write of tags is a replacement. Missing one
    # round of added tags can be retried; the system tags already there cannot be recovered.
    d = FakeDehydrator(answers=[{"tags": ["panel"], "summary": "s"}])
    store = UnreadableStore(existing_tags=["__gist__"])
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert store.updates, "name and summary still get written"
    assert "tags" not in store.updates[0]


def test_fields_the_model_left_out_are_not_written(runtime):
    # Criterion: a model that answers with tags but no subjects has not said "no subjects".
    # Only what came back gets written; the rest stays absent and retryable.
    d = FakeDehydrator(answers=[{"tags": ["panel"], "summary": "s"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = store.updates[0]
    assert written["tags"] == ["panel"]
    for absent in ("subjects", "domain", "aliases"):
        assert absent not in written


def test_valence_and_arousal_are_never_backfilled(runtime):
    # Criterion: how it felt is not the model's to answer. This is the one field the
    # tagging pass is forbidden to touch even when it succeeds — a model-guessed v/a turns
    # the memory into someone else's account of the moment.
    d = FakeDehydrator(
        answers=[{"tags": ["panel"], "valence": 0.9, "arousal": 0.8, "summary": "s"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = store.updates[0]
    assert "valence" not in written and "arousal" not in written


# ───────────────────── the naming fallback, and its stamp ─────────────────────
# Her decision of 2026-08-20. Two halves, and the second one is the one that gets
# forgotten: falling back is only allowed **because** the fallback leaves a mark a
# re-tagging pass can find it by.

# A model that answered, summary included, but named nothing.
NAMELESS = {"tags": ["panel"], "subjects": [{"name": "Es", "kind": ""}], "summary": "s"}


def _written(store):
    assert store.updates, "backfill wrote nothing at all"
    return store.updates[0]


def test_a_bucket_the_model_did_not_name_still_gets_a_name(runtime):
    # Criterion: the first half. Without this the bucket keeps the name it was born with
    # — `2026-08-20 01-05-33`, the second it happened to be written in — forever, because
    # nothing ever revisits it. That is what shows in every list a human reads.
    d = FakeDehydrator(answers=[NAMELESS])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert _written(store)["name"].strip(), "no name was written at all"


def test_the_fallback_name_is_quoted_from_the_body_not_invented(runtime):
    # Criterion: the line that did not move in the 2026-08-20 amendment. The fallback is
    # allowed only because it is the caller's own words — a model-flavoured guess at a
    # title would be exactly the invention this whole file forbids.
    d = FakeDehydrator(answers=[NAMELESS])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert BODY.startswith(_written(store)["name"])


def test_the_fallback_name_is_stamped(runtime):
    # Criterion: THE assertion of this section. The stamp is not decoration — it is the
    # only handle by which a bucket named this way can ever be found again, because the
    # act of naming it is precisely what stops it looking unfinished.
    d = FakeDehydrator(answers=[NAMELESS])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert _written(store)["name_source"] == "fallback"


def test_the_fallback_name_fires_when_the_model_never_answered_at_all(runtime):
    # Criterion: the commonest shape of "no name" is not a model that replied without one
    # — it is a model that did not reply. If the fallback is nested inside the
    # "we got an answer" branch it stops firing in exactly the case it was written for,
    # and it does so silently, because a bucket named after its birth-second looks like a
    # bucket rather than like a failure.
    d = FakeDehydrator(raises=True)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert BODY.startswith(written["name"])
    assert written["name_source"] == "fallback"


def test_a_name_from_the_model_is_not_stamped(runtime):
    # Criterion: the stamp has to mean something. Stamp everything and it distinguishes
    # nothing, and the re-tagging pass it exists for would come back for buckets that are
    # already properly named — forever, on every run.
    d = FakeDehydrator(answers=[{"tags": ["panel"], "name": "面板留中文", "summary": "s"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert written["name"] == "面板留中文"
    assert written.get("name_source") is None, (
        "a name the model actually supplied must not carry the fallback stamp")


def test_a_real_name_takes_the_earlier_stamp_back_off(runtime):
    # Criterion: the stamp is a two-state thing — standing in, or not. A bucket that was
    # given a fallback name and is later re-tagged successfully must come out of the
    # stamped set, or the re-tagging pass keeps finding it and the button never finishes.
    # `None` is how this store deletes a frontmatter field.
    d = FakeDehydrator(answers=[{"tags": ["panel"], "name": "真名", "summary": "s"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert "name_source" in written and written["name_source"] is None, (
        "clearing the stamp has to be an explicit delete; leaving the key out entirely "
        "would let a stale `fallback` sit on a properly named bucket forever")


def test_an_empty_body_gets_no_name_and_no_stamp(runtime):
    # Criterion: there is nothing to quote, so there is nothing honest to write. An empty
    # name would be a fabricated field wearing a stamp that says it came from the body.
    d = FakeDehydrator(answers=[NAMELESS])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", "   \n  ", "event"))
    written = store.updates[0] if store.updates else {}
    assert "name" not in written
    assert "name_source" not in written


def test_the_fallback_name_is_one_line_not_the_whole_body(runtime):
    # Criterion: a name is a label in a list. Multi-line bodies are the norm here, and
    # pasting a paragraph into the name field makes both the list and the filename it
    # derives from unusable.
    d = FakeDehydrator(answers=[NAMELESS])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", "第一行就是标题\n第二行不该进名字\n第三行也不该", "event"))
    assert _written(store)["name"] == "第一行就是标题"


def test_a_very_long_first_line_is_cut(runtime):
    # Criterion: the cap is real. Bucket names become filenames, and an unbounded one
    # fails at the OS rather than anywhere this code can explain it.
    d = FakeDehydrator(answers=[NAMELESS])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", "长" * 500, "event"))
    assert 0 < len(_written(store)["name"]) <= 80


# ───────────────────── the summary's fallback is stamped the same way ─────────────────────

def test_the_fallback_summary_is_stamped(runtime):
    # Criterion: the same blindness, on the field that had the fallback all along. A
    # degraded summary has been landing on disk looking exactly like a real one since
    # before the stamp existed — this is the half of the job that was already shipped and
    # never marked.
    d = FakeDehydrator(answers=[{"tags": ["panel"], "name": "n"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert BODY.startswith(written["summary"])
    assert written["summary_source"] == "fallback"


def test_a_summary_from_the_model_is_not_stamped(runtime):
    d = FakeDehydrator(answers=[{"tags": ["panel"], "name": "n",
                                 "summary": "a real summary"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert written["summary"] == "a real summary"
    assert written.get("summary_source") is None


def test_no_summary_means_no_summary_stamp_either(runtime):
    # Criterion: absent and stood-in-for are different states, and the stamp must not
    # blur them. A stamp on a bucket with no summary would claim a stand-in is in place
    # when the field is simply still waiting.
    d = FakeDehydrator(raises=True)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert "summary" not in written
    assert "summary_source" not in written


def test_the_two_stamps_are_independent(runtime):
    # Criterion: they record two different slots, which one answer can fill one of and
    # not the other. One stamp standing in for both would misreport whichever half worked.
    d = FakeDehydrator(answers=[{**NAMELESS, "summary": "a real summary"}])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = _written(store)
    assert written["name_source"] == "fallback"
    assert written.get("summary_source") is None


# ───────────────────── the stamp has to reach disk ─────────────────────

def _real_store(tmp_path):
    from core.bucket_manager import BucketManager
    return BucketManager({"buckets_dir": str(tmp_path)})


def _frontmatter_of(store, bucket_id):
    import frontmatter
    return frontmatter.load(store._find_bucket_file(bucket_id)).metadata


def test_the_stamp_really_reaches_disk(tmp_path):
    # Criterion: `bucket_manager.update()` writes **only** whitelisted keys and silently
    # drops everything else — its own comments carry the epitaph of `last_dreamt`, a
    # field written at one end, never listed, dropped without a sound, while a smoke test
    # reported None and the bug looked like it lived somewhere else entirely. That
    # comment concludes that remembering to add it to the list does not work as a method,
    # and that the cure is an assertion. This is the assertion.
    #
    # A stamp that never lands is worse than no stamp at all: the re-tagging pass finds
    # nothing and reports a clean library. So this drives the **real** store rather than
    # the fake one, and reads the frontmatter back off the file.
    store = _real_store(tmp_path)
    bucket_id = asyncio.run(store.create(
        content=BODY, tags=[], importance=5, domain=["未分类"],
        valence=0.5, arousal=0.3, room="EVENT/SELF"))
    asyncio.run(store.update(bucket_id, name="stand-in", name_source="fallback",
                             summary="stand-in", summary_source="fallback"))
    meta = _frontmatter_of(store, bucket_id)
    assert meta.get("name_source") == "fallback"
    assert meta.get("summary_source") == "fallback"


def test_clearing_the_stamp_really_removes_it_from_disk(tmp_path):
    # Criterion: the other half. Backfill clears the stamp by passing None, and this
    # store's convention is that None deletes the field. If that convention ever changes,
    # a properly re-tagged bucket keeps a `fallback` stamp it no longer deserves and the
    # re-tagging pass comes back for it on every single run.
    store = _real_store(tmp_path)
    bucket_id = asyncio.run(store.create(
        content=BODY, tags=[], importance=5, domain=["未分类"],
        valence=0.5, arousal=0.3, room="EVENT/SELF"))
    asyncio.run(store.update(bucket_id, name="stand-in", name_source="fallback"))
    asyncio.run(store.update(bucket_id, name="a real name", name_source=None))
    meta = _frontmatter_of(store, bucket_id)
    assert "name_source" not in meta, "the stamp outlived the stand-in it was marking"


def test_placeholder_metadata_is_neutral_and_local(runtime):
    # Criterion: the placeholder written at create time must not look like an answer either.
    # It is what stands in the file during the window before backfill runs — and, if
    # backfill never runs, forever.
    p = R._placeholder_meta()
    assert p["tags"] == []
    assert p["valence"] == 0.5 and p["arousal"] == 0.3
    assert "summary" not in p, "an absent summary is the marker `backfill_sweep` looks for"
    assert "name" not in p
