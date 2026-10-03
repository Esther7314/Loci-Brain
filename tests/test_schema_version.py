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
    # triggered_by -> from at 1 -> 2, and from -> prov at 4 -> 5.
    assert "triggered_by" not in b and "from" not in b
    assert b["prov"] == [{"rel": "wasDerivedFrom", "target": "aaaaaaaaaaaa"}]

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
    assert meta["prov"] == [{"rel": "wasDerivedFrom", "target": "111111111111"}]
    assert "triggered_by" not in meta and "from" not in meta


def test_a_backup_leaves_the_panel_password_and_sessions_behind(old_library):
    (old_library / ".dashboard_auth.json").write_text('{"hash": "x"}', encoding="utf-8")
    (old_library / ".dashboard_sessions.json").write_text("{}", encoding="utf-8")
    with zipfile.ZipFile(S.backup(old_library, "secrets")) as zf:
        names = set(zf.namelist())
    assert ".dashboard_auth.json" not in names and ".dashboard_sessions.json" not in names
    assert "dynamic/a_aaaaaaaaaaaa.md" in names


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
    assert S.library_version(v2_library) == S.CURRENT_VERSION


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


# ───────────────────────── 3 -> 4: __gist__ only where the chain began as a gist ─────────────────────────

GIST = "__gist__"
PERIOD = "__大event__"


@pytest.fixture
def v3_library(tmp_path):
    """A library at version 3 with every shape the step has to tell apart:
    a plain memory regrown once under the old rule (the new version tagged, the root
    not), a fold's gist regrown once (both tagged), a period, and a deleted re-version."""
    # (a) plain root -> tagged re-version
    _write(tmp_path / "dynamic" / "r_aaaaaaaaaaaa.md",
           {"id": "aaaaaaaaaaaa", "room": "EVENT/SELF", "tags": ["hill"],
            "superseded_by": "aaaaaaaaaaa2", "dont_surface": True}, body="Up the hill.")
    _write(tmp_path / "dynamic" / "r_aaaaaaaaaaa2.md",
           {"id": "aaaaaaaaaaa2", "room": "EVENT/SELF", "tags": [GIST, "hill"],
            "supersedes": "aaaaaaaaaaaa", "cover": ["aaaaaaaaaaaa"]}, body="Up the hill, in rain.")
    # (b) fold gist root -> tagged re-version
    _write(tmp_path / "dynamic" / "g_bbbbbbbbbbbb.md",
           {"id": "bbbbbbbbbbbb", "room": "MIND/TRAITS", "tags": [GIST],
            "cover": ["111111111111", "222222222222"], "superseded_by": "bbbbbbbbbbb2"},
           body="Two thoughts in one.")
    _write(tmp_path / "dynamic" / "g_bbbbbbbbbbb2.md",
           {"id": "bbbbbbbbbbb2", "room": "MIND/TRAITS", "tags": [GIST],
            "supersedes": "bbbbbbbbbbbb", "cover": ["bbbbbbbbbbbb"]}, body="Two thoughts, reworded.")
    # (c) a period
    _write(tmp_path / "permanent" / "p_cccccccccccc.md",
           {"id": "cccccccccccc", "tags": [GIST, PERIOD], "when": "2026-07-31..2026-08-05"},
           body="The move.")
    # (a') a re-version of a plain memory that was later deleted: archive counts too
    _write(tmp_path / "archive" / "r_dddddddddddd.md",
           {"id": "dddddddddddd", "room": "EVENT/SELF", "tags": [GIST],
            "supersedes": "aaaaaaaaaaa2", "deleted_at": "2026-09-01T00:00:00"},
           body="A third wording, dropped.")
    (tmp_path / "_state").mkdir()
    (tmp_path / "_state" / "schema.json").write_text(
        json.dumps({"schema_version": 3, "history": []}), encoding="utf-8")
    return tmp_path


def test_a_dry_run_counts_the_tags_to_strip_and_writes_nothing(v3_library):
    before = {p: p.read_bytes() for p in v3_library.rglob("*.md")}
    report = S.migrate(v3_library)
    step = report["steps"][0]
    assert (step["from"], step["to"], step["files"]) == (3, 4, 2)
    assert step["fields"] == {"gist_tag": 2}
    assert {p: p.read_bytes() for p in v3_library.rglob("*.md")} == before


def test_apply_strips_the_tag_only_where_the_chain_began_as_an_ordinary_memory(v3_library):
    S.migrate(v3_library, apply=True)
    regrown = _meta(v3_library / "dynamic" / "r_aaaaaaaaaaa2.md")
    assert regrown["tags"] == ["hill"], "the user's tag stays, the machinery's goes"
    assert "supersedes" in regrown and regrown["cover"] == ["aaaaaaaaaaaa"], "the chain is untouched"
    assert GIST not in _meta(v3_library / "archive" / "r_dddddddddddd.md")["tags"]

    assert GIST in _meta(v3_library / "dynamic" / "g_bbbbbbbbbbb2.md")["tags"], "a fold's gist, reworded"
    assert GIST in _meta(v3_library / "dynamic" / "g_bbbbbbbbbbbb.md")["tags"]
    period = _meta(v3_library / "permanent" / "p_cccccccccccc.md")
    assert set(period["tags"]) == {GIST, PERIOD}
    assert S.library_version(v3_library) == S.CURRENT_VERSION


def test_a_second_run_finds_nothing_left_to_strip(v3_library):
    S.migrate(v3_library, apply=True)
    again = S.migrate(v3_library, apply=True)
    assert again["steps"] == []
    # And the step itself, run again over the result, changes no file.
    step = S.STEPS[3]
    step.prepare(v3_library)
    for path in v3_library.rglob("*.md"):
        meta = _meta(path)
        assert step(meta, S.PurePosixPath(path.relative_to(v3_library).as_posix())) == ([], None)


def test_a_supersedes_loop_or_a_missing_root_does_not_hang_the_walk(tmp_path):
    _write(tmp_path / "dynamic" / "x_eeeeeeeeeeee.md",
           {"id": "eeeeeeeeeeee", "tags": [GIST], "supersedes": "ffffffffffff"})
    _write(tmp_path / "dynamic" / "x_ffffffffffff.md",
           {"id": "ffffffffffff", "tags": [GIST], "supersedes": "eeeeeeeeeeee"})
    _write(tmp_path / "dynamic" / "x_999999999999.md",
           {"id": "999999999999", "tags": [GIST], "supersedes": "000000000000"})
    (tmp_path / "_state").mkdir()
    (tmp_path / "_state" / "schema.json").write_text(
        json.dumps({"schema_version": 3, "history": []}), encoding="utf-8")
    report = S.migrate(tmp_path, apply=True)
    # A loop of tagged versions and a chain whose root is gone are judged by the last
    # version the walk could read, which carries the tag: nothing is stripped.
    assert report["steps"][0]["files"] == 0


# ───────────────────────── 4 -> 5: `from` becomes typed `prov` lines ─────────────────────────

DERIVED = "wasDerivedFrom"
REVISION = "wasRevisionOf"
QUOTED = "wasQuotedFrom"


@pytest.fixture
def v4_library(tmp_path):
    """A library at version 4 with every shape the step has to tell apart: a thought with
    two sources and a placeholder that is no memory's id, a new version with a source of its own, an entry already written in the
    new shape, a deleted thought, a want with a source, and an entry with no links."""
    _write(tmp_path / "dynamic" / "m_aaaaaaaaaaaa.md",
           {"id": "aaaaaaaaaaaa", "room": "MIND/VIEWS", "from": "111111111111, TBD, 222222222222"},
           body="Walks reset the week.")
    _write(tmp_path / "dynamic" / "m_aaaaaaaaaaa2.md",
           {"id": "aaaaaaaaaaa2", "room": "MIND/VIEWS", "from": "111111111111",
            "supersedes": "aaaaaaaaaaaa", "cover": ["aaaaaaaaaaaa"]},
           body="Walks reset the week, rain or not.")
    _write(tmp_path / "dynamic" / "m_bbbbbbbbbbbb.md",
           {"id": "bbbbbbbbbbbb", "room": "MIND/VIEWS", "supersedes": "aaaaaaaaaaa2",
            "prov": [{"rel": DERIVED, "target": "111111111111"},
                     {"rel": REVISION, "target": "aaaaaaaaaaa2"}]},
           body="Already in the new shape.")
    _write(tmp_path / "archive" / "m_cccccccccccc.md",
           {"id": "cccccccccccc", "room": "MIND/TRAITS", "from": "333333333333",
            "deleted_at": "2026-09-01T00:00:00"}, body="A thought, dropped.")
    _write(tmp_path / "dynamic" / "w_dddddddddddd.md",
           {"id": "dddddddddddd", "room": "EVENT/SELF", "direction_of_fit": "telic",
            "from": "111111111111"}, body="Walk on Saturday.")
    _write(tmp_path / "dynamic" / "e_eeeeeeeeeeee.md",
           {"id": "eeeeeeeeeeee", "room": "EVENT/SELF"}, body="Saturday we went up the hill.")
    (tmp_path / "_state").mkdir()
    (tmp_path / "_state" / "schema.json").write_text(
        json.dumps({"schema_version": 4, "history": []}), encoding="utf-8")
    return tmp_path


def test_a_dry_run_counts_the_sources_to_type_and_writes_nothing(v4_library):
    before = {p: p.read_bytes() for p in v4_library.rglob("*.md")}
    report = S.migrate(v4_library)
    [step] = report["steps"]
    assert (step["from"], step["to"], step["files"]) == (4, 5, 4)
    assert step["fields"] == {"from": 4, "revision_of": 1}
    # The want's shape did not change, so it is not put on the list for `bound`.
    assert report["telic"] == []
    assert {p: p.read_bytes() for p in v4_library.rglob("*.md")} == before


def test_apply_types_each_old_source_and_names_the_replaced_version(v4_library):
    S.migrate(v4_library, apply=True)
    thought = _meta(v4_library / "dynamic" / "m_aaaaaaaaaaaa.md")
    assert "from" not in thought
    # Criterion: only a memory's id is a source in the library. Anything else in the old
    # string names something outside it, so it is quoted, kept verbatim and in place.
    assert thought["prov"] == [{"rel": DERIVED, "target": "111111111111"},
                               {"rel": QUOTED, "target": "TBD"},
                               {"rel": DERIVED, "target": "222222222222"}], "order kept"

    revised = _meta(v4_library / "dynamic" / "m_aaaaaaaaaaa2.md")
    assert revised["prov"] == [{"rel": DERIVED, "target": "111111111111"},
                               {"rel": REVISION, "target": "aaaaaaaaaaaa"}]
    assert revised["supersedes"] == "aaaaaaaaaaaa", "the chain field stays; the chain runs on it"

    already = _meta(v4_library / "dynamic" / "m_bbbbbbbbbbbb.md")
    assert already["prov"] == [{"rel": DERIVED, "target": "111111111111"},
                               {"rel": REVISION, "target": "aaaaaaaaaaa2"}], "nothing added twice"
    dropped = _meta(v4_library / "archive" / "m_cccccccccccc.md")
    assert dropped["prov"] == [{"rel": DERIVED, "target": "333333333333"}], "archive counts"
    assert "prov" not in _meta(v4_library / "dynamic" / "e_eeeeeeeeeeee.md")
    assert S.library_version(v4_library) == 5


def test_the_compat_reader_and_the_migration_agree(v4_library):
    # Criterion: a library that was never migrated reads the same chains the migrated one
    # stores, so nothing downstream changes on the day the migration runs.
    from utils import read_from_ids, read_prov
    path = v4_library / "dynamic" / "m_aaaaaaaaaaaa.md"
    legacy = dict(_meta(path))
    S.migrate(v4_library, apply=True)
    migrated = dict(_meta(path))
    assert read_prov(legacy) == read_prov(migrated)
    assert read_from_ids(legacy) == read_from_ids(migrated) == ["111111111111", "222222222222"]


def test_a_second_run_finds_nothing_left_to_type(v4_library):
    S.migrate(v4_library, apply=True)
    again = S.migrate(v4_library, apply=True)
    assert again["steps"] == []
    # And the step itself, run again over the result, changes no file.
    for path in v4_library.rglob("*.md"):
        meta = _meta(path)
        assert S.STEPS[4](meta, S.PurePosixPath(path.relative_to(v4_library).as_posix())) == ([], None)
