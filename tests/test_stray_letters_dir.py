# -*- coding: utf-8 -*-
"""
tests/test_stray_letters_dir.py — a library that still has a `letters/` folder on disk.

`letters/` is not a memory directory. Whatever sits in it stays where it is and is never
read as a memory: not listed, not searched, not surfaced, not counted, not found by id,
not restored from a backup zip, not touched by a schema migration. Nothing here deletes it.
"""

import asyncio

import frontmatter
import pytest

from core import schema
from core.bucket_manager import BucketManager
from core.decay_engine import DecayEngine
from core.package_import import MigrateEngine
from core import runtime as rt
from tools.breath import awaken as A
from tools.grow import rooms_path
from tools.recall import core as R

LETTER_ID = "1e77e2000001"
LETTER_TEXT = "Dear you, the tide came in early today."


class _Log:
    def _noop(self, *a, **k):
        return None
    warning = info = debug = error = _noop


def _write_stray_letter(root):
    folder = root / "letters" / "history"
    folder.mkdir(parents=True)
    post = frontmatter.Post(
        LETTER_TEXT,
        id=LETTER_ID, name="tide", type="letter", author="user", title="t",
        letter_date="2026-08-01", created="2026-08-01T10:00:00",
        tags=["tide"], domain=["history"],
    )
    path = folder / f"tide_{LETTER_ID}.md"
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    return path


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


def test_a_stray_letters_folder_is_not_a_memory_anywhere(store, tmp_path):
    letter_path = _write_stray_letter(tmp_path)
    before = letter_path.read_text(encoding="utf-8")

    async def go():
        bid = await store.create("Bread from the shop downstairs, still warm.", tags=["t"])

        everything = await store.list_all(include_archive=True)
        assert [b["id"] for b in everything] == [bid]
        assert [b["id"] for b in await store.list_all()] == [bid]

        assert await store.get(LETTER_ID) is None
        assert not [h for h in await store.search("tide came in early") if h["id"] == LETTER_ID]

        stats = await store.get_stats()
        assert "letter_count" not in stats
        assert stats["dynamic_count"] == 1 and "history" not in stats["domains"]

        out = await R.recall_core(when="", room="", tag="", query="tide came in early")
        assert f"({LETTER_ID[:6]})" not in out and LETTER_TEXT not in out

        screen = A.render_breath(await A.build_breath())
        assert "tide came in early" not in screen
    asyncio.run(go())

    assert letter_path.read_text(encoding="utf-8") == before   # left exactly as it was


def test_a_stray_letters_folder_is_not_swept_by_decay(store, tmp_path):
    letter_path = _write_stray_letter(tmp_path)
    before = letter_path.read_text(encoding="utf-8")

    async def go():
        await store.create("Bread from the shop downstairs, still warm.", tags=["t"])
        return await DecayEngine({}, store).run_decay_cycle()
    asyncio.run(go())
    assert letter_path.read_text(encoding="utf-8") == before
    assert not list((tmp_path / "archive").rglob(f"*{LETTER_ID}*.md"))


def test_a_schema_migration_does_not_look_inside_letters(tmp_path):
    letter_path = _write_stray_letter(tmp_path)
    before = letter_path.read_text(encoding="utf-8")
    assert list(schema._memory_files(tmp_path)) == []
    assert schema.library_version(tmp_path) is None     # nothing but a letter: a new library
    assert schema.migrate(tmp_path, apply=True)["steps"] == []
    assert letter_path.read_text(encoding="utf-8") == before


def test_a_backup_zip_does_not_bring_letters_back_as_memories(store, tmp_path):
    engine = MigrateEngine({"buckets_dir": str(tmp_path)}, store, None)
    memory = frontmatter.dumps(frontmatter.Post(
        "Bread from the shop downstairs.", id="a0a0a0a0a0a0", name="bread", type="dynamic",
        domain=["daily"], tags=["t"]))
    letter = frontmatter.dumps(frontmatter.Post(
        LETTER_TEXT, id=LETTER_ID, name="tide", type="letter", domain=["history"]))
    package = {
        "files": {
            "buckets/dynamic/daily/bread_a0a0a0a0a0a0.md": memory.encode("utf-8"),
            f"buckets/letters/history/tide_{LETTER_ID}.md": letter.encode("utf-8"),
        },
        "integrity_verified": True, "integrity_warning": "", "manifest": {},
    }
    parsed = engine._parse_package(package, disk_backed=False)
    assert [b.bucket_id for b in parsed["buckets"]] == ["a0a0a0a0a0a0"]
