# -*- coding: utf-8 -*-
"""The pure judgment helpers under update()/create().

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    `_update_locked` delegates every "is this value acceptable" decision to a handful of
    pure functions — clamps, normalizers, the boolean parser, the path sanitizers. They
    make real decisions (reject / fall back / truncate / dedupe) and had zero direct
    coverage: a wrong fallback here does not raise, it just quietly stores the wrong
    thing, which is exactly the shape of every historical bug in this repo.

    Trivial getters are deliberately NOT tested (see the 8-22 criteria in the handover:
    a test on `return self._x` is a liability). Only functions that can answer
    differently for different inputs appear here.
"""
import pytest

from core.bucket_manager import BucketManager, _clamp01, _clamp_importance, _clamp_unit
from utils import parse_bool, safe_path, sanitize_name


# ───────────────────────── parse_bool: the bool("false") trap ─────────────────────────

def test_parse_bool_reads_quoted_false_as_false():
    # Criterion: THE reason this function exists. Python's bool("false") is True, and a
    # YAML caller quoting a boolean would otherwise flip every flag it tried to clear.
    assert parse_bool("false") is False
    assert parse_bool("False") is False
    assert parse_bool(" no ") is False
    assert parse_bool("off") is False
    assert parse_bool("0") is False


def test_parse_bool_accepts_the_common_true_spellings():
    assert parse_bool(True) is True
    assert parse_bool("true") is True
    assert parse_bool("YES") is True
    assert parse_bool("on") is True
    assert parse_bool(1) is True
    assert parse_bool(0) is False


def test_parse_bool_rejects_junk_instead_of_guessing():
    # Criterion: 2 or "maybe" is not a boolean anyone meant. Guessing truthiness would
    # silently resolve/pin/digest on garbage input.
    with pytest.raises(ValueError):
        parse_bool("maybe")
    with pytest.raises(ValueError):
        parse_bool(2)
    with pytest.raises(ValueError):
        parse_bool(None)


def test_parse_bool_junk_falls_to_the_default_when_one_is_given():
    assert parse_bool("maybe", default=False) is False
    assert parse_bool(None, default=True) is True


# ───────────────────────── the clamps ─────────────────────────

def test_importance_is_clamped_into_one_to_ten():
    assert _clamp_importance(99, "t") == 10
    assert _clamp_importance(-5, "t") == 1
    assert _clamp_importance(7, "t") == 7


def test_unparseable_importance_falls_back_to_five():
    # Criterion: five is the documented neutral default. Zero or ten would make junk
    # input the least or the most important thing in the store.
    assert _clamp_importance("junk", "t") == 5
    assert _clamp_importance(None, "t") == 5


def test_emotion_coordinates_are_clamped_into_the_unit_interval():
    assert _clamp_unit(1.5, "valence", "t") == 1.0
    assert _clamp_unit(-0.5, "valence", "t") == 0.0
    assert _clamp_unit(0.25, "valence", "t") == 0.25


def test_non_finite_emotion_values_fall_back_to_neutral():
    # Criterion: NaN passes a naive min/max clamp untouched (every comparison is
    # False), and one stored NaN poisons every weighted score it ever enters.
    assert _clamp_unit(float("nan"), "valence", "t") == 0.5
    assert _clamp_unit(float("inf"), "arousal", "t") == 0.5
    assert _clamp_unit("junk", "valence", "t") == 0.5


def test_clamp01_returns_the_callers_default_on_failure():
    assert _clamp01("junk", 0.7) == 0.7
    assert _clamp01(float("nan"), 0.2) == 0.2
    assert _clamp01(1.5, 0.0) == 1.0
    assert _clamp01(-1, 0.0) == 0.0
    assert _clamp01(0.4, 0.0) == 0.4


# ───────────────────────── the label normalizer (tags/aliases/subjects/domain) ─────────────────────────

def test_metadata_list_dedupes_and_drops_empties_preserving_order():
    out = BucketManager._normalize_metadata_list(
        ["b", "a", "b", "", "  ", "c"], max_items=10, max_chars=50
    )
    assert out == ["b", "a", "c"]


def test_metadata_list_wraps_a_bare_string_instead_of_iterating_it():
    # Criterion: iterating the string would store one label per character.
    out = BucketManager._normalize_metadata_list("solo", max_items=10, max_chars=50)
    assert out == ["solo"]


def test_metadata_list_caps_count_and_length():
    out = BucketManager._normalize_metadata_list(
        [f"tag-{i}" for i in range(20)] + ["x" * 100], max_items=3, max_chars=8
    )
    assert out == ["tag-0", "tag-1", "tag-2"]
    long_out = BucketManager._normalize_metadata_list(["x" * 100], max_items=3, max_chars=8)
    assert long_out == ["x" * 8]


def test_metadata_list_treats_none_as_empty():
    assert BucketManager._normalize_metadata_list(None, max_items=3, max_chars=8) == []


def test_metadata_list_dedupes_after_truncation_not_before():
    # Criterion: two labels that only differ beyond the cap become the same label on
    # disk; keeping both would store a visible duplicate row.
    out = BucketManager._normalize_metadata_list(
        ["y" * 20 + "a", "y" * 20 + "b"], max_items=10, max_chars=10
    )
    assert out == ["y" * 10]


# ───────────────────────── the meaning normalizers ─────────────────────────

def test_meaning_list_does_not_deduplicate():
    # Criterion: the same sentence written at two different moments is itself
    # information; deduplicating would erase that gap in time.
    assert BucketManager._normalize_meaning_list(["same", "same"]) == ["same", "same"]


def test_meaning_list_drops_empties_and_wraps_a_bare_string():
    assert BucketManager._normalize_meaning_list(["keep", "", "   "]) == ["keep"]
    assert BucketManager._normalize_meaning_list("one line") == ["one line"]
    assert BucketManager._normalize_meaning_list(None) == []


def test_meaning_item_is_a_length_cap_not_a_summary():
    out = BucketManager._normalize_meaning_item("m" * 5000)
    assert out == "m" * 2000
    assert BucketManager._normalize_meaning_item("") == ""


# ───────────────────────── the media normalizer ─────────────────────────

def test_media_entries_without_a_path_are_dropped():
    # Criterion: path is the identity used for dedup on append; an entry without one
    # can never be opened and would collide with every other pathless entry.
    out = BucketManager._normalize_media(
        [{"path": "a.png"}, {"title": "no path"}, "not a dict", None]
    )
    assert out == [{"path": "a.png"}]


def test_media_sha256_is_kept_only_when_it_really_is_one():
    good = "a" * 64
    out = BucketManager._normalize_media(
        [{"path": "a.png", "sha256": good},
         {"path": "b.png", "sha256": "ZZ-not-hex"},
         {"path": "c.png", "sha256": "abc123"}]
    )
    assert out[0]["sha256"] == good
    # Criterion: a malformed digest stored as-is would fail every later integrity
    # comparison while looking like evidence of corruption.
    assert "sha256" not in out[1]
    assert "sha256" not in out[2]


def test_media_size_must_be_a_non_negative_integer():
    out = BucketManager._normalize_media(
        [{"path": "a.png", "size": "123"},
         {"path": "b.png", "size": -1},
         {"path": "c.png", "size": "junk"}]
    )
    assert out[0]["size"] == 123
    assert "size" not in out[1]
    assert "size" not in out[2]


def test_media_stored_flag_only_survives_as_a_real_true():
    out = BucketManager._normalize_media(
        [{"path": "a.png", "stored": True}, {"path": "b.png", "stored": "yes"}]
    )
    assert out[0]["stored"] is True
    assert "stored" not in out[1]


def test_media_list_is_capped():
    out = BucketManager._normalize_media([{"path": f"p{i}.png"} for i in range(30)])
    assert len(out) == 20


# ───────────────────────── the text sanitizer (F-04) ─────────────────────────

def test_sanitize_text_strips_the_invisible_characters():
    dirty = "a" + chr(0) + "b" + chr(0x202E) + "c" + chr(0x2066) + "d" + chr(0x7F) + "e"
    assert BucketManager._sanitize_text(dirty) == "abcde"


def test_sanitize_text_keeps_newlines_tabs_and_real_writing():
    text = "line one\nline two\ttabbed\r\n中文也在 🐳"
    # Criterion: the sanitizer must strip only what can lie to a reader. Eating
    # newlines or CJK would corrupt every body it touches.
    assert BucketManager._sanitize_text(text) == text


# ───────────────────────── the path guards ─────────────────────────

def test_sanitize_name_disarms_path_traversal():
    out = sanitize_name("../../etc/passwd")
    assert "/" not in out and "\\" not in out and ".." not in out
    assert out  # never empty: an empty name would build an invalid path


def test_sanitize_name_falls_back_to_unnamed():
    assert sanitize_name("") == "unnamed"
    assert sanitize_name("!!!###") == "unnamed"
    assert sanitize_name(None) == "unnamed"


def test_sanitize_name_keeps_ordinary_names():
    assert sanitize_name("morning walk 2026-08") == "morning walk 2026-08"
    assert sanitize_name("中文名字") == "中文名字"


def test_safe_path_confines_to_the_base_directory(tmp_path):
    base = tmp_path / "buckets"
    base.mkdir()
    assert safe_path(str(base), "ok.md").name == "ok.md"
    with pytest.raises(ValueError):
        safe_path(str(base), "../escape.md")


def test_safe_path_is_not_fooled_by_a_sibling_with_the_same_prefix(tmp_path):
    # Criterion: with base=/data/buckets, target /data/buckets_evil/f.md passes a
    # string-prefix check. The guard must compare path components, not characters.
    base = tmp_path / "buckets"
    base.mkdir()
    (tmp_path / "buckets_evil").mkdir()
    with pytest.raises(ValueError):
        safe_path(str(base), "../buckets_evil/f.md")
