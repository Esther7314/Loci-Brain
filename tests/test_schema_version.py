# -*- coding: utf-8 -*-
"""
tests/test_schema_version.py — the library says which version it is, and migrating
backs it up first (core/schema.py).
"""

import json
import zipfile

import frontmatter
import pytest

from core import schema as S


def _write(path, meta, body="body"):
    path.parent.mkdir(parents=True, exist_ok=True)
    post = frontmatter.Post(body, **meta)
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


@pytest.fixture
def old_library(tmp_path):
    """A 1.4.0-shaped library: no version file, one legacy room, one triggered_by."""
    _write(tmp_path / "dynamic" / "a_aaaaaaaaaaaa.md",
           {"id": "aaaaaaaaaaaa", "room": "I/EVENT/SELF/WHO"})
    _write(tmp_path / "dynamic" / "b_bbbbbbbbbbbb.md",
           {"id": "bbbbbbbbbbbb", "room": "MIND/VIEWS", "triggered_by": "aaaaaaaaaaaa"})
    _write(tmp_path / "archive" / "c_cccccccccccc.md",
           {"id": "cccccccccccc", "room": "YOU/MIND/WHAT"})
    _write(tmp_path / "dynamic" / "d_dddddddddddd.md",
           {"id": "dddddddddddd", "room": "EVENT/SELF"})
    (tmp_path / "config.yaml").write_text("api_key: secret\n", encoding="utf-8")
    return tmp_path


def _meta(path):
    return frontmatter.load(path).metadata


def test_a_new_empty_library_is_stamped_current(tmp_path):
    assert S.library_version(tmp_path) is None
    S.stamp_new_library(tmp_path)
    assert S.library_version(tmp_path) == S.CURRENT_VERSION
    assert not S.status(tmp_path)["behind"]


def test_a_library_from_before_versions_is_version_one(old_library):
    S.stamp_new_library(old_library)          # must not stamp over existing memories
    assert S.library_version(old_library) == 1
    assert S.status(old_library)["behind"]


def test_a_dry_run_counts_and_writes_nothing(old_library):
    before = {p: p.read_bytes() for p in old_library.rglob("*") if p.is_file()}
    report = S.migrate(old_library)
    step = report["steps"][0]
    assert step["files"] == 3
    assert step["fields"] == {"room": 2, "triggered_by": 1}
    after = {p: p.read_bytes() for p in old_library.rglob("*") if p.is_file()}
    assert after == before
    assert not (old_library / S.BACKUP_DIR).exists()


def test_apply_backs_up_first_then_migrates(old_library):
    a_before = (old_library / "dynamic" / "a_aaaaaaaaaaaa.md").read_bytes()
    report = S.migrate(old_library, apply=True)

    with zipfile.ZipFile(report["backup"]) as zf:
        names = set(zf.namelist())
        assert "dynamic/a_aaaaaaaaaaaa.md" in names
        assert "archive/c_cccccccccccc.md" in names
        assert "config.yaml" not in names, "the config holds keys and is not backed up"
        assert zf.read("dynamic/a_aaaaaaaaaaaa.md") == a_before, "backup is pre-migration"

    assert _meta(old_library / "dynamic" / "a_aaaaaaaaaaaa.md")["room"] == "EVENT/SELF"
    assert _meta(old_library / "archive" / "c_cccccccccccc.md")["room"] == "MIND/VIEWS"
    b = _meta(old_library / "dynamic" / "b_bbbbbbbbbbbb.md")
    assert "triggered_by" not in b and b["from"] == "aaaaaaaaaaaa"

    state = json.loads((old_library / "_state" / "schema.json").read_text(encoding="utf-8"))
    assert state["schema_version"] == S.CURRENT_VERSION
    assert state["history"][-1]["backup"] == report["backup"]


def test_migrating_twice_changes_nothing_the_second_time(old_library):
    S.migrate(old_library, apply=True)
    again = S.migrate(old_library, apply=True)
    assert again["steps"] == [] and again["backup"] == ""


def test_an_existing_from_wins_over_triggered_by(tmp_path):
    _write(tmp_path / "dynamic" / "e_eeeeeeeeeeee.md",
           {"id": "eeeeeeeeeeee", "room": "MIND/VIEWS",
            "from": "111111111111", "triggered_by": "222222222222"})
    S.migrate(tmp_path, apply=True)
    meta = _meta(tmp_path / "dynamic" / "e_eeeeeeeeeeee.md")
    assert meta["from"] == "111111111111" and "triggered_by" not in meta


def test_a_later_backup_does_not_contain_earlier_ones(old_library):
    first = S.backup(old_library, "one")
    second = S.backup(old_library, "two")
    with zipfile.ZipFile(second) as zf:
        assert not any(n.startswith(S.BACKUP_DIR + "/") for n in zf.namelist())
    assert first.exists()
