# -*- coding: utf-8 -*-
"""
tests/test_grow_same_body_dedup.py — a body word for word the same as a stored one is a
duplicate only when nothing the new call brings would be lost.

The old check matched the text alone: a new source, a `from`, a cue, a different day were
dropped without a word, and a new want came back with the id of a want already closed.
Now the stored entry must be live and current, wanted the same way, still open, on the same
day, and already carry every source, line and cue the call brings.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.grow import dispatch as grow
from tools.grow import rooms_path

REC = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0142",
       "fingerprint": "sha256:aa", "fingerprint_by": "adapter"}
BODY = "We talked it over at breakfast."


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
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


def _grow(text=BODY, when="", **kw):
    item = {"room": "EVENT/WORLD", "text": text, "v": 0.6, "a": 0.3}
    if when:
        item["when"] = when
    out = run(grow(kind="event", items=[item], **kw))
    return out, (out.split("📝", 1)[1].split()[0] if "📝" in out else "")


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def test_the_same_call_twice_is_still_one_entry(store):
    _out, first = _grow()
    out, second = _grow()
    assert first and not second and f"♻️{first}" in out


def test_a_new_source_on_the_same_words_is_not_dropped(store, tmp_path):
    _out, first = _grow()
    out, second = _grow(sources=[REC])
    assert second and second != first, out
    assert [r["id"] for r in _disk(tmp_path, second)["sources"]] == ["m_0142"]


def test_a_from_on_the_same_words_is_not_dropped(store, tmp_path):
    _out, first = _grow()
    out, second = _grow(from_=["m_0931"])
    assert second and second != first, out
    assert _disk(tmp_path, second)["prov"] == [{"rel": "wasQuotedFrom", "target": "m_0931"}]


def test_a_cue_on_the_same_words_is_not_dropped(store, tmp_path):
    _out, first = _grow(direction_of_fit="telic")
    out, second = _grow(direction_of_fit="telic", cue={"condition": "after the exam"})
    assert second and second != first, out
    assert _disk(tmp_path, second)["cue"]["condition"] == "after the exam"


def test_another_day_is_another_event(store):
    _out, first = _grow(when="2026-09-01")
    out, second = _grow(when="2026-09-02")
    assert second and second != first, out


def test_a_new_want_never_comes_back_as_a_closed_one(store):
    _out, first = _grow(direction_of_fit="telic")
    assert run(store.update(first, status="resolved"))
    out, second = _grow(direction_of_fit="telic")
    assert second and second != first, out


def test_a_want_is_not_a_duplicate_of_a_record(store):
    _out, first = _grow()
    out, second = _grow(direction_of_fit="telic")
    assert second and second != first, out


def test_an_old_version_is_not_a_duplicate(store):
    _out, first = _grow()
    assert run(store.update(first, superseded_by="0123456789ab"))
    out, second = _grow()
    assert second and second != first, out
