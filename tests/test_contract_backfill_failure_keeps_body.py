# -*- coding: utf-8 -*-
"""CONTRACT: when the tagging model fails, the body is already safe and nothing is invented.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    Writing a memory happens in two moves. The body goes to disk immediately, by itself,
    with neutral placeholder metadata. Then a background pass asks a model for tags,
    subjects, a domain, a name and a summary, and writes those on top.

    The second move can fail — an expired key, a rate limit, a model that returns nothing,
    a network that is not there. The contract is about what the failure is allowed to cost:

        the body                 never at risk; it was on disk before the model was asked
        tags / subjects / name   left ABSENT, never filled with a guess
        summary                  falls back to the opening of the body — the caller's own
                                 words, quoted, not a model's paraphrase of them

    The dangerous version of this code is not one that crashes. It is one that helpfully
    writes `tags: []` and `subjects: []` when the model fails, because an empty list is
    indistinguishable from "the model looked and found nothing" — and the memory is then
    permanently invisible to every search that goes through tags, with no sign anything
    went wrong and nothing left to retry against.

WHY THE FAKE RUNTIME
    `_backfill_one` reaches for the global runtime for its model, its store and its logger.
    That is exactly what step two of this work order is about changing. These tests are
    written BEFORE that change, on purpose: they are what tells us the change did not move
    the semantics. So the runtime is faked rather than the function reshaped — and when the
    signature does change, this file should keep passing with a smaller fake, not a bigger one.
"""
import asyncio

import pytest

from tools import _runtime as rt
from tools.grow import rooms_path as R

BODY = "She said the panel stays in Chinese and the comments go. That is the whole rule."


class FakeDehydrator:
    """Stands in for the tagging model. Each knob is one way the real thing fails."""

    def __init__(self, *, analyze_raises=False, analyze_returns=None,
                 chat_returns=None, chat_raises=False, has_chat=True):
        self.analyze_raises = analyze_raises
        self.analyze_returns = analyze_returns
        self.chat_returns = list(chat_returns or [])
        self.chat_raises = chat_raises
        self.analyze_calls = 0
        self.chat_calls = 0
        if not has_chat:
            self._chat = None

    async def analyze(self, text, for_mind=False):
        self.analyze_calls += 1
        if self.analyze_raises:
            raise RuntimeError("upstream 401: your API key expired")
        return self.analyze_returns

    async def _chat(self, system, user, max_tokens=0, temperature=0.0):
        self.chat_calls += 1
        if self.chat_raises:
            raise RuntimeError("connection reset")
        return self.chat_returns.pop(0) if self.chat_returns else ""


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

def test_analyze_raising_does_not_propagate(runtime):
    # Criterion: backfill runs detached, behind the call that already returned success to
    # the caller. An exception escaping here is an unhandled task exception nobody sees,
    # and the caller has already been told the memory was saved — which it was.
    d = FakeDehydrator(analyze_raises=True, chat_returns=["a summary"])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))       # must simply return


def test_nothing_is_invented_when_analyze_fails(runtime):
    # Criterion: THE assertion of this file. With no answer from the model, none of the
    # fields it would have filled may be written — not as empty, not as a placeholder.
    # An absent field can be retried; a field written empty looks answered forever.
    d = FakeDehydrator(analyze_raises=True, chat_returns=["a real summary"])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))

    assert len(store.updates) == 1
    written = store.updates[0]
    for invented in ("tags", "subjects", "domain", "name", "aliases"):
        assert invented not in written, f"{invented!r} was fabricated out of a failed model call"


def test_a_failed_model_is_written_down_somewhere(runtime):
    # Criterion: silence is the thing that makes this class of failure expensive. It does
    # not have to interrupt anyone, but it does have to be findable afterwards.
    d = FakeDehydrator(analyze_raises=True, chat_returns=["s"])
    logger = runtime(d)
    run(R._backfill_one("b1", BODY, "event"))
    assert any("b1" in line for line in logger.lines), \
        "a bucket whose tagging failed must be identifiable from the log"


def test_the_body_is_never_written_by_backfill(runtime):
    # Criterion: backfill only ever adds metadata. The body reached disk before the model
    # was asked, so any write of content here would be backfill overwriting the one thing
    # that was already safe — with a value derived from a model that just failed.
    d = FakeDehydrator(analyze_raises=True, chat_returns=["s"])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert all("content" not in u for u in store.updates)


def test_a_failed_write_does_not_propagate_either(runtime):
    # Criterion: the store can fail too (disk full, file locked). Same reasoning as the
    # model: the caller is long gone and the body is already safe.
    d = FakeDehydrator(analyze_returns={"tags": ["x"]}, chat_returns=["s"])
    runtime(d, FakeStore(update_raises=True))
    run(R._backfill_one("b1", BODY, "event"))


def test_no_write_at_all_when_there_is_nothing_to_write(runtime):
    # Criterion: model down AND summary unavailable means there is genuinely nothing to
    # add. Writing an empty update would bump the file's timestamp and, worse, clear the
    # "unfinished" marker that `backfill_sweep` uses to find this bucket again later.
    # Both halves have to be unavailable for there to be genuinely nothing to write, and
    # only one shape of failure does that: no chat channel at all (an unconfigured
    # deployment). With a channel present, `_make_summary` degrades to the body's opening
    # and there is always a summary to write.
    d = FakeDehydrator(analyze_raises=True, has_chat=False)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert store.updates == []


# ───────────────────────── the summary's own fallback ─────────────────────────

def test_summary_falls_back_to_the_callers_own_words(runtime):
    # Criterion: the fallback quotes the body rather than inventing a line. The summary is
    # the hook that makes a memory visible in a zoomed-out view, so an empty one hides it —
    # but a made-up one would be worse, because it would read as something she wrote.
    d = FakeDehydrator(analyze_returns=None, chat_returns=["", ""])
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
    d = FakeDehydrator(analyze_returns={"tags": ["t"]}, has_chat=False)
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert "summary" not in store.updates[0]


def test_a_raising_chat_does_not_fall_back(runtime):
    # Criterion: an empty answer and a thrown exception are different. Empty means the model
    # answered and had nothing; a raise means it never answered, and the right record of
    # that is "not done yet", not a degraded stand-in that ends the retry.
    d = FakeDehydrator(analyze_returns={"tags": ["t"]}, chat_raises=True)
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
    d = FakeDehydrator(analyze_returns={"tags": ["evening", "panel"]}, chat_returns=["s"])
    store = FakeStore(existing_tags=["__archive_fact__"])
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    assert store.updates[0]["tags"] == ["__archive_fact__", "evening", "panel"]


def test_fields_the_model_left_out_are_not_written(runtime):
    # Criterion: a model that answers with tags but no subjects has not said "no subjects".
    # Only what came back gets written; the rest stays absent and retryable.
    d = FakeDehydrator(analyze_returns={"tags": ["one"]}, chat_returns=["s"])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = store.updates[0]
    assert written["tags"] == ["one"]
    for absent in ("subjects", "domain", "name", "aliases"):
        assert absent not in written


def test_valence_and_arousal_are_never_backfilled(runtime):
    # Criterion: how it felt is not the model's to answer. This is the one field the
    # tagging pass is forbidden to touch even when it succeeds — a model-guessed v/a turns
    # the memory into someone else's account of the moment.
    d = FakeDehydrator(
        analyze_returns={"tags": ["t"], "valence": 0.9, "arousal": 0.8},
        chat_returns=["s"])
    store = FakeStore()
    runtime(d, store)
    run(R._backfill_one("b1", BODY, "event"))
    written = store.updates[0]
    assert "valence" not in written and "arousal" not in written


def test_placeholder_metadata_is_neutral_and_local(runtime):
    # Criterion: the placeholder written at create time must not look like an answer either.
    # It is what stands in the file during the window before backfill runs — and, if
    # backfill never runs, forever.
    p = R._placeholder_meta()
    assert p["tags"] == []
    assert p["valence"] == 0.5 and p["arousal"] == 0.3
    assert "summary" not in p, "an absent summary is the marker `backfill_sweep` looks for"
    assert "name" not in p
