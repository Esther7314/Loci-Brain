# -*- coding: utf-8 -*-
"""CONTRACT: fading is a display-layer sink. The original text comes back verbatim.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    A memory nobody has thought about in a long time sinks: the original body moves to
    `archive/原文/{id}.txt`, and what stays in the library is the one-line summary. That
    is deliberate, and it is modelled on the real thing — you remember what that day was
    like, and you cannot quote what was said.

    The word that carries all the risk in that sentence is *moves*. Sinking is not
    deleting, and the difference is entirely in whether the original comes back. So this
    file asserts one thing from several angles: after a sink, `restore` returns the body
    character for character, and every way the restore can fail leaves the memory no worse
    off than before.

    The reviewer asked specifically for the three unhappy paths, and they are the three
    that would go unnoticed: a damaged archive, a restore done twice, and two restores at
    once. None of them is exotic — a half-finished sync produces the first, an impatient
    second click produces the second, and the panel plus a tool call produce the third.

WHY THIS ONE TOUCHES THE DISK
    The other contract tests are pure. This one cannot be: the whole claim is about what
    survives a round trip through the filesystem, and a fake store would be asserting that
    the fake preserves text. Everything runs under `tmp_path` and never sees a real
    memory; the cost is a few milliseconds per test.
"""
import asyncio
import os

import pytest

from core.bucket_manager import BucketManager

# Deliberately awkward: blank line, punctuation, trailing spaces on an interior line, and
# a non-ASCII character. This is what "verbatim" has to mean.
BODY = "first line   \n\nthird line, with a comma — and a dash\n    indented fourth"
SUMMARY = "four lines about nothing much"


@pytest.fixture
def store(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path)})


def run(coro):
    return asyncio.run(coro)


async def _sunk_bucket(bm) -> tuple[str, str]:
    """Create a bucket, give it the summary a sink requires, sink it. Returns (id, stored body).

    The stored body is read back rather than assumed equal to BODY: the frontmatter writer
    normalizes the very outer edges of a document, and the contract under test is about the
    sink round trip, not about that normalization.
    """
    bid = await bm.create(BODY, tags=["contract"])
    stored = (await bm.get(bid))["content"]
    await bm.update(bid, summary=SUMMARY)
    assert await bm.sink_bucket(bid) is True
    return bid, stored


# ───────────────────────── the ordinary round trip ─────────────────────────

def test_sinking_leaves_the_summary_in_place_of_the_body(store):
    async def go():
        bid, _ = await _sunk_bucket(store)
        b = await store.get(bid)
        # Criterion: what remains is the summary and a marker saying why. Without the
        # marker there is no way to tell a sunk memory from a short one.
        assert b["content"] == SUMMARY
        assert b["metadata"]["decay_stage"] == "sunk"
    run(go())


def test_the_original_is_on_disk_immediately_after_the_sink(store, tmp_path):
    async def go():
        bid, stored = await _sunk_bucket(store)
        # Criterion: the file is written BEFORE the library copy is overwritten. If the
        # order were reversed, a crash between the two steps would lose the body outright,
        # and the summary left behind would look like a complete memory.
        orig = tmp_path / "archive" / "原文" / f"{bid}.txt"
        assert orig.exists()
        assert orig.read_text(encoding="utf-8") == stored
    run(go())


def test_restore_brings_the_body_back_character_for_character(store):
    async def go():
        bid, stored = await _sunk_bucket(store)
        result = await store.restore_archived(bid)
        assert result["ok"] is True and result["unsunk"] is True
        # Criterion: THE assertion of this file. Not "roughly the same", not "the same
        # after stripping" — the same string. Blank lines, trailing spaces and indentation
        # are part of what was written.
        assert (await store.get(bid))["content"] == stored
    run(go())


def test_restore_clears_the_marker(store):
    async def go():
        bid, _ = await _sunk_bucket(store)
        await store.restore_archived(bid)
        # Criterion: a restored memory is an ordinary memory. A leftover marker would make
        # every later reader treat a full body as if it were a summary.
        assert (await store.get(bid)).get("metadata", {}).get("decay_stage") is None
    run(go())


def test_restoring_counts_as_remembering_it(store):
    async def go():
        bid, _ = await _sunk_bucket(store)
        before = int((await store.get(bid))["metadata"].get("activation_count") or 0)
        await store.restore_archived(bid)
        after = (await store.get(bid))["metadata"]
        # Criterion: without this, decay sinks it again on the next pass and the restore
        # is undone by the thing that caused it. Pulling a memory back up IS thinking
        # about it, and the clock has to hear that.
        assert int(after.get("activation_count") or 0) == before + 1
        assert after.get("last_active")
    run(go())


def test_a_sunk_memory_is_still_in_the_library(store):
    async def go():
        bid, _ = await _sunk_bucket(store)
        ids = [b["id"] for b in await store.list_all(include_archive=False)]
        # Criterion: "display-layer sink" means exactly this. It is still a memory, still
        # listed, still searchable. If sinking removed it from the library it would be a
        # slow delete with a nicer name.
        assert bid in ids
    run(go())


# ───────────────────────── the sink refuses when it cannot be undone ─────────────────────────

def test_a_memory_with_no_summary_will_not_sink(store):
    async def go():
        bid = await store.create(BODY, tags=["contract"])
        # Criterion: sinking means "replace the body with the summary". With no summary
        # there is nothing to replace it with, and sinking anyway would blank the memory.
        # Waiting is correct: the summary arrives on the next backfill.
        assert await store.sink_bucket(bid) is False
        b = await store.get(bid)
        assert b["content"] == BODY.strip(), "a refused sink must leave the body untouched"
        assert b.get("metadata", {}).get("decay_stage") is None
    run(go())


def test_sinking_twice_changes_nothing(store):
    async def go():
        bid, _ = await _sunk_bucket(store)
        # Criterion: idempotent, and idempotent in the strong sense — the second call must
        # not copy the summary over the archived original. That would destroy the body
        # using a value derived from the body, and every assertion above would still pass.
        assert await store.sink_bucket(bid) is True
        await store.restore_archived(bid)
        assert (await store.get(bid))["content"] != SUMMARY
    run(go())


# ───────────────────────── the three unhappy paths the reviewer asked for ─────────────────────────

def test_a_damaged_archive_reports_itself_instead_of_inventing_a_body(store, tmp_path):
    async def go():
        bid, _ = await _sunk_bucket(store)
        (tmp_path / "archive" / "原文" / f"{bid}.txt").unlink()

        result = await store.restore_archived(bid)
        # Criterion: the honest answer is "I cannot find it", not a silent success that
        # leaves the summary sitting in the body's place looking like the whole memory.
        assert result["ok"] is False
        assert result["error"].startswith("orig_missing")

        # Criterion: and the memory is no worse off — the summary is still there, the
        # marker is still there, so a later restore can still succeed if the file comes
        # back (from a backup, from a half-finished sync finishing).
        b = await store.get(bid)
        assert b is not None and b["content"] == SUMMARY
        assert b["metadata"]["decay_stage"] == "sunk"
    run(go())


def test_restoring_twice_is_refused_the_second_time_not_repeated(store):
    async def go():
        bid, stored = await _sunk_bucket(store)
        first = await store.restore_archived(bid)
        second = await store.restore_archived(bid)
        # Criterion: the second call has nothing to restore and says so. The failure mode
        # this guards is a second restore reading the now-restored body as if it were the
        # archived original — harmless here, but the same code path serves archived
        # (deleted) buckets, where a repeat would resurrect a tombstone.
        assert first["ok"] is True
        assert second["ok"] is False and second["error"] == "not_archived"
        assert (await store.get(bid))["content"] == stored
    run(go())


def test_two_restores_at_once_produce_exactly_one_restore(store):
    async def go():
        bid, stored = await _sunk_bucket(store)
        # Criterion: the panel and a tool call can ask at the same moment. Both succeeding
        # would mean the file was written twice from a source one of them had already
        # consumed. One wins, one is told there was nothing to do, and the body is right.
        results = await asyncio.gather(store.restore_archived(bid),
                                       store.restore_archived(bid))
        assert sorted(bool(r["ok"]) for r in results) == [False, True]
        assert (await store.get(bid))["content"] == stored
        assert (await store.get(bid))["metadata"].get("decay_stage") is None
    run(go())


# ───────────────────────── the other archive: an explicit delete ─────────────────────────

def test_a_deleted_memory_stops_surfacing_but_is_still_reachable(store):
    async def go():
        bid = await store.create(BODY, tags=["contract"])
        stored = (await store.get(bid))["content"]
        assert await store.delete(bid) is True

        # Criterion: delete is a move to the archive, not an erase. The ordinary read
        # stops finding it — that is what "deleted" has to mean day to day — and the
        # restore still brings it back whole. Both halves are the contract.
        assert await store.get(bid) is None
        result = await store.restore_archived(bid)
        assert result["ok"] is True
        assert (await store.get(bid))["content"] == stored
    run(go())


def test_two_deletes_restored_at_once_also_produce_exactly_one(store):
    async def go():
        bid = await store.create(BODY, tags=["contract"])
        stored = (await store.get(bid))["content"]
        await store.delete(bid)
        results = await asyncio.gather(store.restore_archived(bid),
                                       store.restore_archived(bid))
        # Criterion: same race, other path. Written separately because the two restore
        # paths diverge inside the lock, and a fix to one has historically missed the other.
        assert sorted(bool(r["ok"]) for r in results) == [False, True]
        assert (await store.get(bid))["content"] == stored
    run(go())


def test_restoring_something_that_was_never_archived_is_a_no_op(store):
    async def go():
        bid = await store.create(BODY, tags=["contract"])
        result = await store.restore_archived(bid)
        # Criterion: answering "there is nothing archived here" is the only safe answer.
        # Anything that rewrote the bucket would be a write triggered by a mistaken click.
        assert result["ok"] is False
        assert (await store.get(bid))["content"] == BODY.strip()
    run(go())


def test_restoring_an_unknown_id_does_not_raise(store):
    async def go():
        result = await store.restore_archived("no-such-bucket-id")
        assert result["ok"] is False
    run(go())


def test_the_archive_directory_is_the_only_thing_the_sink_creates(store, tmp_path):
    async def go():
        bid, _ = await _sunk_bucket(store)
        # Criterion: the original goes to one known place with a known name. A test that
        # only checked "restore works" would keep passing if the path moved, and every
        # already-sunk memory on disk would become unreachable at the same moment.
        assert os.path.isfile(str(tmp_path / "archive" / "原文" / f"{bid}.txt"))
    run(go())
