# -*- coding: utf-8 -*-
"""
tests/test_prov_sources.py — what the write tools store as `prov`, and what the read by
id shows of it.

`from` is still the word the tools take. What lands on disk is one typed line per
source, the relation picked by the route: a memory named in `from` is wasDerivedFrom, a
line id from the host's conversation (`m_0931`) is wasQuotedFrom and is never looked up,
and regrow adds one wasRevisionOf line for the version it replaced. Up to 64 lines, none
of them cut. Checked through the tools' dispatch against a real BucketManager, with the
results read back from the files.
"""

import asyncio

import frontmatter
import pytest

import tools.grow as grow_mod
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.fold import dispatch as fold
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.recall import core as R
from tools.regrow import dispatch as regrow

VIEW = "Quiet mornings are when the week gets sorted out."
GHOST = "0123456789ab"   # the right shape, never created


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


def _files(tmp_path):
    return sorted(p for p in tmp_path.rglob("*.md"))


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _line(rel, target):
    return {"rel": rel, "target": target}


async def _events(store, n):
    return [await store.create(f"Morning number {i} was quiet.", room="EVENT/SELF")
            for i in range(n)]


async def _mind(**kw):
    out = await grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3, **kw)
    assert out.startswith("🧠mind→"), out
    return out.split("🧠mind→", 1)[1].split()[0]


def _source_lines(out: str) -> list[str]:
    lines = out.splitlines()
    start = lines.index("来源:") + 1
    return [ln for ln in lines[start:] if ln.startswith("  ← ")]


# ───────────────────────── many sources ─────────────────────────

def test_a_memory_with_eight_sources_is_written_and_read_back(store, tmp_path):
    async def go():
        events = await _events(store, 8)
        new = await _mind(from_=events)
        # Criterion: the old ceiling was five ids; the sixth was refused and, before
        # that, cut in half. Eight now land whole and in order.
        assert _disk(tmp_path, new)["prov"] == [_line("wasDerivedFrom", e) for e in events]
        shown = _source_lines(await R.recall_core(when="", room="", tag="", query=new))
        assert [ln.split()[1] for ln in shown] == events
        assert all("[派生]" in ln for ln in shown)
    run(go())


def test_the_sixty_fifth_source_is_refused_and_nothing_is_written(store, tmp_path):
    async def go():
        events = await _events(store, 1)
        before = _files(tmp_path)
        out = await grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                         from_=events + [f"m_{i:04d}" for i in range(64)])
        assert "最多 64" in out
        assert _files(tmp_path) == before
    run(go())


# ───────────────────────── a line of the conversation ─────────────────────────

def test_a_line_id_from_the_host_is_quoted_without_a_lookup(store, tmp_path):
    async def go():
        [event] = await _events(store, 1)
        new = await _mind(from_=[event, "m_0931"])
        assert _disk(tmp_path, new)["prov"] == [_line("wasDerivedFrom", event),
                                                _line("wasQuotedFrom", "m_0931")]
        by_id = {ln.split()[1]: ln for ln in
                 _source_lines(await R.recall_core(when="", room="", tag="", query=new))}
        assert "[引原话]" in by_id["m_0931"] and "查无此桶" not in by_id["m_0931"]
        # The reverse chain is a library walk; a quoted line is not part of it.
        assert await store.referenced_by("m_0931") == []
        assert await store.referenced_by(event) == [new]
    run(go())


def test_a_memory_id_that_does_not_exist_is_still_refused(store, tmp_path):
    async def go():
        before = _files(tmp_path)
        out = await grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                         from_=[GHOST])
        # Criterion: only what is not shaped like a memory's id is taken as a quote. A
        # 12-hex id that is not there is a typo or a deletion, not the host's line.
        assert GHOST in out and _files(tmp_path) == before
    run(go())


def test_prose_in_from_is_not_taken_for_a_line_id(store, tmp_path):
    async def go():
        before = _files(tmp_path)
        out = await grow(kind="mind", room="MIND/VIEWS", text=VIEW, v=0.6, a=0.3,
                         from_=["what they said this morning"])
        assert "不像 id" in out and _files(tmp_path) == before
    run(go())


def test_an_event_and_a_fold_store_prov_too(store, tmp_path):
    async def go():
        [event] = await _events(store, 1)
        before = set(_files(tmp_path))
        await grow(kind="event", from_=["m_0001"],
                   items=[{"room": "EVENT/SELF", "text": "We talked it over.", "v": 0.6, "a": 0.2,
                           "when": "2026-09-27"}])
        [added] = set(_files(tmp_path)) - before
        assert dict(frontmatter.load(added).metadata)["prov"] == [_line("wasQuotedFrom", "m_0001")]

        a = await _mind(from_=[event])
        b = await _mind(from_=[event, "m_0002"])
        out = await fold(text="Mornings carry the week.", room="MIND/VIEWS", v=0.6, a=0.4,
                         cover=[a, b], from_=[event])
        gist = out.split("▣gist→", 1)[1].split()[0]
        assert _disk(tmp_path, gist)["prov"] == [_line("wasDerivedFrom", event)]
        assert "from" not in _disk(tmp_path, gist)
    run(go())


# ───────────────────────── regrow ─────────────────────────

def test_regrow_carries_the_lines_adds_the_new_ones_and_names_the_old_version(store, tmp_path):
    async def go():
        first, second = await _events(store, 2)
        old = await _mind(from_=[first, "m_0001"])
        touched_before = _disk(tmp_path, old)["activation_count"]
        await regrow(bucket_id=old, text=VIEW + " Even the rainy ones.", v=0.6, a=0.3,
                     mode="supplement", from_=[second, "m_0002"])
        new = _disk(tmp_path, old)["superseded_by"]
        assert _disk(tmp_path, new)["prov"] == [
            _line("wasDerivedFrom", first), _line("wasQuotedFrom", "m_0001"),
            _line("wasDerivedFrom", second), _line("wasQuotedFrom", "m_0002"),
            _line("wasRevisionOf", old)]
        # The chain fields are untouched by the typed line beside them.
        assert _disk(tmp_path, new)["supersedes"] == old
        shown = _source_lines(await R.recall_core(when="", room="", tag="", query=new))
        assert any(ln.split()[1] == old and "[新版本]" in ln for ln in shown)
        # A revision is not a source: the old version has nothing grown out of it, and
        # touching the sources does not reach it through the revision line.
        assert await store.referenced_by(old) == []
        assert _disk(tmp_path, old)["activation_count"] == touched_before
        assert _disk(tmp_path, second)["activation_count"] > 0
        assert "被引用" not in await R.recall_core(when="", room="", tag="", query=old)
    run(go())


def test_a_third_version_names_only_the_second(store, tmp_path):
    async def go():
        [event] = await _events(store, 1)
        v1 = await _mind(from_=[event])
        await regrow(bucket_id=v1, text="Second wording.", v=0.6, a=0.3, mode="supplement")
        v2 = _disk(tmp_path, v1)["superseded_by"]
        await regrow(bucket_id=v2, text="Third wording.", v=0.6, a=0.3, mode="supplement")
        v3 = _disk(tmp_path, v2)["superseded_by"]
        # Criterion: wasRevisionOf is the direct predecessor. The second version's own
        # revision line stays behind; the sources come across.
        assert _disk(tmp_path, v3)["prov"] == [_line("wasDerivedFrom", event),
                                               _line("wasRevisionOf", v2)]
    run(go())


def test_writing_prov_retires_an_unmigrated_from(store, tmp_path):
    async def go():
        [event] = await _events(store, 1)
        bid = await store.create(VIEW, room="MIND/VIEWS")
        [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
        post = frontmatter.load(path)
        post["from"] = event
        path.write_text(frontmatter.dumps(post), encoding="utf-8")

        assert await store.update(bid, prov=[_line("wasQuotedFrom", "m_0003")])
        meta = _disk(tmp_path, bid)
        # Criterion: one entry, one record of where it came from. Left behind, the
        # older string would disagree with prov the moment anyone read it directly.
        assert "from" not in meta and meta["prov"] == [_line("wasQuotedFrom", "m_0003")]
    run(go())
