# -*- coding: utf-8 -*-
"""The size gates on the way in: how big one memory may be, how many fit in one call.

WHY THIS FILE EXISTS
    These are the only things standing between a runaway caller and the disk, and every
    one of them was uncovered. They are also the easiest functions in the repo to test —
    a string in, a message or None out, no world required — which is exactly why nobody
    got around to it.

    🔴 Writing this file found a live hole. `check_grow_items_payload` reads
       `item.get("content")`, while the items `grow` actually receives are keyed
       **`text`** (`rooms_path.py` does `item["text"]`). `content` is not merely a
       different name — it is the parameter that was **withdrawn**; the source comment a
       few lines away says so. So the gate was guarding a field that no longer arrives,
       every dict item measured as 0 bytes, and the batch-total ceiling never once fired.

       Blast radius, with the shipped defaults: the per-item gate (50 KB) and the item
       count gate (100) both still work, so the hole is not "anything goes" — it is
       **100 × 50 KB = 4.9 MB accepted in one call against a 2 MB ceiling**.

       It stayed invisible because the gate fails *open* and says nothing. There is no
       error to notice; the batch simply goes in.

WHAT THIS DOES NOT CHECK
    Whether the ceilings are the right numbers. That is a judgement call and it lives in
    config. What is checked is that a ceiling, once set, is actually applied — and applied
    to the field that really shows up.
"""
import pytest

from tools import _runtime as rt
from tools._common import (
    check_content_size,
    check_grow_input_size,
    check_grow_items_payload,
    check_metadata_size,
    check_query_size,
    max_bucket_bytes,
    max_grow_items,
)


@pytest.fixture
def limits(monkeypatch):
    """Set config.limits for one test. Everything unset falls back to the shipped default."""
    def apply(**kw):
        monkeypatch.setattr(rt, "config", {"limits": kw})
    return apply


def item(text, **extra):
    """An item shaped the way grow really receives one."""
    return {"room": "EVENT/SELF", "text": text, "v": 0.5, "a": 0.5, **extra}


# ── the batch total, i.e. the one that was not working ───────────────────────

def test_the_batch_total_counts_the_field_that_actually_arrives(limits):
    """🔴 The hole. `grow` items carry `text`; the gate was reading `content`.

    Without this assertion the gate reports None for every dict that ever reaches it,
    and nothing anywhere goes red — it fails open and in silence.
    """
    limits(max_grow_input_bytes=1024)
    err = check_grow_items_payload([item("x" * 5000)])
    assert err is not None, "5 KB of text against a 1 KB ceiling must be refused"
    assert "过大" in err


def test_the_batch_total_still_understands_the_old_shapes(limits):
    # Bare strings and `content` dicts were what worked before; keep them working, or
    # fixing the above would quietly break whoever still calls it the old way.
    limits(max_grow_input_bytes=1024)
    assert check_grow_items_payload(["x" * 5000]) is not None
    assert check_grow_items_payload([{"content": "x" * 5000}]) is not None


def test_many_small_items_add_up(limits):
    """One item under the line, the batch over it. This is the whole point of a *total*."""
    limits(max_grow_input_bytes=1000)
    ten = [item("x" * 300) for _ in range(10)]          # 3000 bytes across ten items
    assert check_grow_items_payload([item("x" * 300)]) is None
    assert check_grow_items_payload(ten) is not None


def test_the_count_gate_is_separate_from_the_byte_gate(limits):
    limits(max_grow_items=3, max_grow_input_bytes=10 * 1024)
    assert check_grow_items_payload([item("x")] * 3) is None
    err = check_grow_items_payload([item("x")] * 4)
    assert err is not None and "过多" in err, "four tiny items must still trip the count gate"


def test_unmeasurable_items_are_skipped_not_crashed(limits):
    limits(max_grow_input_bytes=1024)
    assert check_grow_items_payload([None, 42, item("x" * 5000)]) is not None
    assert check_grow_items_payload([None, 42]) is None


# ── the per-entry ceiling ────────────────────────────────────────────────────

def test_one_entry_over_the_ceiling_is_refused(limits):
    limits(max_bucket_bytes=1024)
    assert check_content_size("x" * 1024) is None
    assert check_content_size("x" * 1025) is not None


def test_the_ceiling_is_bytes_not_characters(limits):
    """Every memory in this system is Chinese, and Chinese is 3 bytes a character.

    Counting characters would make the real ceiling three times what the config says —
    the kind of mistake that only shows up on a full disk.
    """
    limits(max_bucket_bytes=300)
    assert len("中" * 101) == 101                        # 101 characters
    assert check_content_size("中" * 101) is not None     # 303 bytes: refused
    assert check_content_size("中" * 100) is None         # 300 bytes: exactly at the line


@pytest.mark.parametrize("gate,key", [
    (check_content_size, "max_bucket_bytes"),
    (check_grow_input_size, "max_grow_input_bytes"),
    (check_query_size, "max_query_bytes"),
])
def test_a_ceiling_of_zero_means_no_ceiling(limits, gate, key):
    # 0 is "switch this gate off", not "allow nothing" — getting that backwards would
    # reject every single write.
    limits(**{key: 0})
    assert gate("x" * 10_000_000) is None


@pytest.mark.parametrize("junk", ["abc", None, -5])
def test_a_broken_config_falls_back_to_the_default_rather_than_opening_the_gate(limits, junk):
    limits(max_bucket_bytes=junk)
    assert max_bucket_bytes() == 50 * 1024, f"{junk!r} should fall back, not disable the gate"


def test_a_fraction_below_one_must_not_silently_switch_the_gate_off(limits):
    """🔴 0 means "no ceiling", and `int(0.5)` is 0.

    So a typo in the config — a decimal where an integer was meant — does not produce a
    tiny ceiling, it produces **no ceiling**, and nothing says a word. A gate that fails
    open on a typo is worse than one that fails closed: the closed one gets fixed the
    same afternoon.
    """
    limits(max_bucket_bytes=0.5)
    assert max_bucket_bytes() != 0, "0.5 truncating to 0 would read as 'gate switched off'"
    assert max_bucket_bytes() == 50 * 1024


def test_a_decimal_above_one_is_just_truncated(limits):
    # The other side of the line: 3.7 bytes is a sane thing to round down. Only the
    # sub-1 case is dangerous, because only it lands on the "off" value.
    limits(max_bucket_bytes=3.7)
    assert max_bucket_bytes() == 3


def test_no_limits_section_at_all_still_has_the_defaults(monkeypatch):
    monkeypatch.setattr(rt, "config", {})
    assert max_bucket_bytes() == 50 * 1024
    assert max_grow_items() == 100


# ── metadata ─────────────────────────────────────────────────────────────────

def test_metadata_fields_are_summed_not_measured_one_by_one(limits):
    limits(max_metadata_bytes=100)
    assert check_metadata_size(name="x" * 60) is None
    err = check_metadata_size(name="x" * 60, tags="y" * 60)
    assert err is not None, "two fields under the line separately, over it together"
    assert "name" in err and "tags" in err, "the message should name the fields involved"


def test_metadata_ignores_empty_fields(limits):
    limits(max_metadata_bytes=100)
    assert check_metadata_size(name=None, tags="", domain="x" * 50) is None
