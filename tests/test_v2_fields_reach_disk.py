# -*- coding: utf-8 -*-
"""
tests/test_v2_fields_reach_disk.py — the v2 write-layer fields really land on disk.

update() writes only whitelisted keys and drops the rest without a word; that trap bit
three times (closed_by, last_asked, last_dreamt). So every new field is asserted on the
file itself, through create() and through update().
"""

import asyncio

import frontmatter
import pytest

from core.bucket_manager import BucketManager

BODY = "She said she wants to see the aurora someday."


@pytest.fixture
def store(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path)})


def run(coro):
    return asyncio.run(coro)


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return frontmatter.load(path).metadata


def test_create_writes_every_marked_field(store, tmp_path):
    bid = run(store.create(
        BODY, tags=["t"], direction_of_fit="telic", bound=["Es", "Es", "DT"],
        evidential="inference", internally_generated=True, recurrence="freq=yearly",
        backfilled=["bound"], weight=0.7))
    meta = _disk(tmp_path, bid)
    assert meta["direction_of_fit"] == "telic"
    assert meta["bound"] == ["Es", "DT"]
    assert meta["evidential"] == "inference"
    assert meta["internally_generated"] is True
    assert meta["recurrence"] == "FREQ=YEARLY"
    assert meta["backfilled"] == ["bound"]
    assert meta["weight"] == 0.7, "telic carries weight"


def test_defaults_are_not_written(store, tmp_path):
    bid = run(store.create(BODY, tags=["t"], direction_of_fit="thetic", weight=0.7))
    meta = _disk(tmp_path, bid)
    for field in ("direction_of_fit", "bound", "evidential", "internally_generated",
                  "recurrence", "backfilled"):
        assert field not in meta, field
    assert "weight" not in meta, "weight only goes with what is wanted"


def test_update_writes_each_field_and_the_default_removes_it(store, tmp_path):
    bid = run(store.create(BODY, tags=["t"]))
    assert run(store.update(bid, direction_of_fit="telic", bound=["DT"],
                            evidential="assumption", internally_generated=True,
                            recurrence="FREQ=YEARLY", backfilled=["recurrence"]))
    meta = _disk(tmp_path, bid)
    assert (meta["direction_of_fit"], meta["bound"], meta["evidential"]) == \
        ("telic", ["DT"], "assumption")
    assert meta["internally_generated"] is True and meta["recurrence"] == "FREQ=YEARLY"
    assert meta["backfilled"] == ["recurrence"]

    assert run(store.update(bid, direction_of_fit="thetic", bound=[], evidential="",
                            internally_generated=False, recurrence="", backfilled=[]))
    meta = _disk(tmp_path, bid)
    for field in ("direction_of_fit", "bound", "evidential", "internally_generated",
                  "recurrence", "backfilled"):
        assert field not in meta, field


@pytest.mark.parametrize("field,value", [
    ("direction_of_fit", "want"),
    ("evidential", "direct"),
    ("recurrence", "FREQ=MONTHLY"),
])
def test_a_value_outside_the_enum_is_refused(store, tmp_path, field, value):
    with pytest.raises(ValueError):
        run(store.create(BODY, tags=["t"], **{field: value}))
    bid = run(store.create(BODY + " (2)", tags=["t"]))
    assert run(store.update(bid, **{field: value})) is False
    assert field not in _disk(tmp_path, bid)
