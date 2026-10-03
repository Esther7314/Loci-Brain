# -*- coding: utf-8 -*-
"""
tests/test_replace_while_read.py — a write that lands while someone is reading the file.

On Windows a file that another handle has open cannot be replaced (WinError 5 / 32), and a
file in the middle of being replaced cannot be opened. Both last milliseconds: a write
waits them out instead of failing, and a read that still cannot open the file says so
(raises) instead of answering "no such entry", which callers would read as a blank one.
"""

import asyncio
import os
import threading

import frontmatter
import pytest

import utils
from core import _cue_ledger
from core.bucket_manager import BucketManager


def _sharing_violation():
    err = PermissionError(13, "The process cannot access the file because it is being "
                              "used by another process")
    err.winerror = 32
    return err


def test_a_replace_refused_for_a_moment_is_retried(tmp_path, monkeypatch):
    target = tmp_path / "entry.md"
    target.write_text("old", encoding="utf-8")
    real_replace = os.replace
    refused = {"n": 0}

    def replace(src, dst):
        if refused["n"] < 3:
            refused["n"] += 1
            raise _sharing_violation()
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    utils.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert not list(tmp_path.glob("*.tmp")), "the temp file is gone once the write lands"


@pytest.mark.skipif(os.name != "nt", reason="only Windows refuses to replace an open file")
def test_a_reader_holding_the_file_open_does_not_fail_the_write(tmp_path):
    target = tmp_path / "entry.md"
    target.write_text("old", encoding="utf-8")
    reader = open(target, encoding="utf-8")
    threading.Timer(0.2, reader.close).start()
    utils.atomic_write_text(target, "new")
    assert target.read_text(encoding="utf-8") == "new"


def test_a_compaction_waits_out_a_reader(tmp_path, monkeypatch):
    ledger = _cue_ledger.CueLedger(str(tmp_path))
    real_replace = os.replace
    refused = {"n": 0}

    def replace(src, dst):
        if str(dst).endswith(os.path.basename(ledger.path)) and refused["n"] < 2:
            refused["n"] += 1
            raise _sharing_violation()
        return real_replace(src, dst)
    monkeypatch.setattr(os, "replace", replace)
    ledger.path.parent.mkdir(parents=True, exist_ok=True)
    ledger._compact_locked()
    assert refused["n"] == 2
    assert ledger.path.is_file()


@pytest.fixture
def store(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path)})


def test_get_waits_out_a_file_being_replaced(store, monkeypatch):
    bid = asyncio.run(store.create("Body.", room="EVENT/SELF"))
    real_load = frontmatter.load
    refused = {"n": 0}

    def load(path, *a, **k):
        if refused["n"] < 2:
            refused["n"] += 1
            raise _sharing_violation()
        return real_load(path, *a, **k)
    monkeypatch.setattr(frontmatter, "load", load)
    got = asyncio.run(store.get(bid))
    assert got is not None and got["content"] == "Body."


def test_get_raises_when_the_file_stays_unreadable(store, monkeypatch):
    bid = asyncio.run(store.create("Body.", room="EVENT/SELF"))

    def load(path, *a, **k):
        raise _sharing_violation()
    monkeypatch.setattr(frontmatter, "load", load)
    # Criterion: "could not read it" is not "it does not exist" — a backfill or an append
    # reading None would treat the entry as blank and overwrite it.
    with pytest.raises(OSError):
        asyncio.run(store.get(bid))
