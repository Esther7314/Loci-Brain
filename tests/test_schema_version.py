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


# ───────────────────────── 2 -> 3: want and the old plan become telic ─────────────────────────

@pytest.fixture
def v2_library(tmp_path):
    """A library at version 2: one want, three old plans (open, done, dropped)."""
    _write(tmp_path / "dynamic" / "日常" / "w_aaaaaaaaaaaa.md",
           {"id": "aaaaaaaaaaaa", "room": "EVENT/SELF", "status": "want", "weight": 0.6,
            "when": "2026-10-01"}, body="Finish her gift.")
    for status, bid in (("active", "bbbbbbbbbbbb"), ("resolved", "cccccccccccc"),
                        ("abandoned", "dddddddddddd")):
        _write(tmp_path / "plans" / status / f"p_{bid}.md",
               {"id": bid, "type": "plan", "status": status, "room": "EVENT/SELF",
                "domain": ["学习"], "weight": 0.5, "change_log": [{"kind": "create"}]},
               body=f"Plan {status}.")
    (tmp_path / "_state").mkdir()
    (tmp_path / "_state" / "schema.json").write_text(
        json.dumps({"schema_version": 2, "history": []}), encoding="utf-8")
    return tmp_path


def test_a_dry_run_lists_every_wanted_entry_and_moves_nothing(v2_library):
    report = S.migrate(v2_library)
    step = report["steps"][0]
    assert (step["from"], step["files"], step["moved"]) == (2, 4, 3)
    assert step["fields"] == {"want": 1, "plan": 3}
    assert sorted(r["id"] for r in report["telic"]) == [
        "aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"]
    assert (v2_library / "plans" / "active" / "p_bbbbbbbbbbbb.md").exists()


def test_apply_turns_want_and_plans_into_telic_events(v2_library):
    S.migrate(v2_library, apply=True)
    want = _meta(v2_library / "dynamic" / "日常" / "w_aaaaaaaaaaaa.md")
    assert want["direction_of_fit"] == "telic" and "status" not in want
    assert want["weight"] == 0.6 and want["when"] == "2026-10-01"

    assert not list((v2_library / "plans").rglob("*.md")), "plans/ is emptied"
    opened = _meta(v2_library / "dynamic" / "学习" / "p_bbbbbbbbbbbb.md")
    assert (opened["type"], opened["direction_of_fit"]) == ("dynamic", "telic")
    assert "status" not in opened, "an open plan is simply open"
    done = _meta(v2_library / "dynamic" / "学习" / "p_cccccccccccc.md")
    dropped = _meta(v2_library / "dynamic" / "学习" / "p_dddddddddddd.md")
    assert (done["status"], dropped["status"]) == ("resolved", "abandoned")
    assert done["change_log"] == [{"kind": "create"}], "history stays as data"
    assert S.library_version(v2_library) == 3


def test_the_moved_plans_are_found_by_the_store(v2_library):
    import asyncio
    from core.bucket_manager import BucketManager
    S.migrate(v2_library, apply=True)
    mgr = BucketManager({"buckets_dir": str(v2_library)})
    ids = {b["id"] for b in asyncio.run(mgr.list_all())}
    assert {"aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc", "dddddddddddd"} <= ids


def test_a_deleted_plan_stays_in_the_archive(v2_library):
    _write(v2_library / "archive" / "p_eeeeeeeeeeee.md",
           {"id": "eeeeeeeeeeee", "type": "plan", "status": "active",
            "deleted_at": "2026-07-06T20:26:53"}, body="Dropped long ago.")
    S.migrate(v2_library, apply=True)
    meta = _meta(v2_library / "archive" / "p_eeeeeeeeeeee.md")
    assert (meta["type"], meta["direction_of_fit"]) == ("dynamic", "telic")
    assert meta["deleted_at"] == "2026-07-06T20:26:53"
