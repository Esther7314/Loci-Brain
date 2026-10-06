# -*- coding: utf-8 -*-
"""
tests/test_names_table.py — the names table: aliases, plus what a name is and where it
appears (stage 4.3).

Two shapes are read: the older one (a bare alias list under each name, the way every
table written before kinds existed looks) and the mapping one (aliases / instance_of /
present_in / member_of, every key optional). The flat {spelling: canonical} view every
normaliser reads must come out the same from either.

The writers edit the file's text, never re-dump it: the table is hand-written and its
comments carry the reasons. So each writer test compares the whole file.
"""

import asyncio
import json

import pytest
import yaml

from core import names as S

LEGACY = """\
# A hand-written header that explains the table.
# It must survive every click.

Michael Chen:
  - Mike
  # an aside about Mike
  - "77"

# not a person, as the panel writes it
__不是人__:
  - teacher
  - Galaxy

Sarah:
"""

NEW = """\
Connor:
  aliases: [RK800]
  instance_of: 人
  present_in: Detroit
Detroit: {instance_of: 游戏}
Book Club:
  instance_of: 群
Mike Chen:
  aliases:
  - Mike C
  member_of:
    - Book Club
    - Galaxy
Galaxy:
  instance_of: 群
__不是人__:
  - Galaxy
  - teacher
"""


@pytest.fixture
def table(tmp_path, monkeypatch):
    path = tmp_path / "aliases.yaml"
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(path))
    monkeypatch.setattr(S, "_cache", None)

    def write(text: str):
        path.write_text(text, encoding="utf-8")
        S._cache = None
        return path
    return write


def _text(path) -> str:
    return path.read_text(encoding="utf-8")


# ───────────────────────── reading ─────────────────────────

def test_a_legacy_table_reads_as_it_always_did(table):
    table(LEGACY)
    assert S.load_alias_table() == {
        "michael chen": "Michael Chen", "mike": "Michael Chen", "77": "Michael Chen",
        "sarah": "Sarah"}
    assert S.load_not_person() == frozenset({"teacher", "galaxy"})
    assert S.canonical("mike") == "Michael Chen"
    assert S.canonical("Galaxy") == ""
    rec = S.load_names_table()["Michael Chen"]
    assert rec.aliases == ("Mike", "77") and rec.instance_of == ""
    assert S.load_names_table()["Sarah"].aliases == ()
    # A table of aliases says nothing about kinds: unknown, not presumed a person.
    assert S.is_person("Mike") is None and S.kind_of("Mike") == ""
    assert S.is_person("teacher") is False
    assert S.is_person("someone new") is None


def test_the_mapping_shape_reads_kinds_and_links(table):
    table(NEW)
    names = S.load_names_table()
    assert names["Connor"] == S.NameRecord(
        name="Connor", aliases=("RK800",), instance_of="人", present_in=("Detroit",))
    assert names["Detroit"].instance_of == "游戏"
    assert names["Mike Chen"].member_of == ("Book Club", "Galaxy")
    assert S.load_alias_table()["rk800"] == "Connor"
    assert S.load_alias_table()["mike c"] == "Mike Chen"
    assert (S.kind_of("rk800"), S.is_person("rk800")) == ("人", True)
    assert (S.kind_of("Detroit"), S.is_person("Detroit")) == ("游戏", False)


def test_an_explicit_kind_wins_over_the_not_person_list(table):
    table(NEW)
    # Galaxy is listed as not a person AND given a kind: the kind speaks for it, so it
    # is no longer dropped from subjects. teacher has no entry of its own: still blocked.
    assert S.load_not_person() == frozenset({"teacher"})
    assert S.canonical("Galaxy") == "Galaxy"
    assert S.canonical("teacher") == ""
    assert S.load_names_table()["Galaxy"].not_person is False


def test_a_shared_alias_stands_for_neither_and_a_key_stands_for_itself(table):
    table("Leon:\n  instance_of: 人\n"
          "Leon (Detroit):\n  aliases: [Leon, Lee]\n  present_in: [Detroit]\n"
          "Lee Ann:\n  - Lee\n")
    flat = S.load_alias_table()
    assert flat["leon"] == "Leon", "a key always stands for itself"
    assert "lee" not in flat, "two entries share it: collapsed into neither"
    assert S.canonical("Lee") == "Lee"
    assert S.record_of("Lee") is None
    assert S.candidates("Lee") == ["Leon (Detroit)", "Lee Ann"]
    assert S.candidates("leon") == ["Leon", "Leon (Detroit)"]


def test_name_key_keeps_a_not_person_name_that_canonical_drops(table):
    table(LEGACY)
    assert S.canonical("Galaxy") == ""
    assert S.name_key("Galaxy") == "Galaxy"
    assert S.name_key("mike") == "Michael Chen"
    assert S.name_key("我") == ""


def test_a_missing_or_broken_table_is_empty_not_an_error(table, tmp_path):
    assert S.load_alias_table() == {} and S.load_names_table() == {}
    table("Connor: [unclosed\n")
    assert S.load_alias_table() == {} and S.is_person("Connor") is None


# ───────────────────────── writing ─────────────────────────

def test_set_kind_turns_a_bare_list_into_aliases_and_keeps_every_comment(table):
    path = table(LEGACY)
    assert S.set_kind("Mike", "人") is True
    assert _text(path) == LEGACY.replace(
        'Michael Chen:\n  - Mike\n  # an aside about Mike\n  - "77"\n',
        'Michael Chen:\n  aliases:\n    - Mike\n    # an aside about Mike\n    - "77"\n'
        '  instance_of: 人\n')
    assert S.kind_of("Michael Chen") == "人"
    assert S.load_alias_table()["77"] == "Michael Chen"
    # Same kind again: nothing to write.
    assert S.set_kind("Michael Chen", "人") is False
    # A different kind replaces the line rather than adding a second one.
    assert S.set_kind("Michael Chen", "群") is True
    assert _text(path).count("instance_of:") == 1 and S.kind_of("Mike") == "群"


def test_set_kind_adds_an_unknown_name_and_takes_it_off_the_not_person_list(table):
    path = table(LEGACY)
    assert S.set_kind("Galaxy", "群") is True
    text = _text(path)
    assert "  - Galaxy\n" not in text and "  - teacher\n" in text
    assert text.endswith("Sarah:\n\nGalaxy:\n  instance_of: 群\n")
    assert S.canonical("Galaxy") == "Galaxy" and S.is_person("Galaxy") is False
    assert S.set_kind("Sarah", "人") is True
    assert "Sarah:\n  instance_of: 人\n" in _text(path)


def test_add_alias_files_under_aliases_whichever_shape_the_entry_has(table):
    path = table(LEGACY + "Connor:\n  instance_of: 人\n")
    assert S.add_alias("Michael Chen", "MC") is True
    assert '  - "77"\n  - MC\n' in _text(path), "legacy list: appended after the last item"
    assert S.add_alias("Connor", "RK800") is True
    assert "Connor:\n  instance_of: 人\n  aliases:\n    - RK800\n" in _text(path)
    assert S.add_alias("connor", "rk800") is False, "already there, any case"
    assert S.add_alias("RK800", "Android") is True, "an alias spelling files under its entry"
    assert S.load_alias_table()["android"] == "Connor"


def test_add_alias_refuses_a_spelling_another_entry_owns(table):
    table(LEGACY)
    with pytest.raises(ValueError):
        S.add_alias("Sarah", "Mike")
    with pytest.raises(ValueError):
        S.add_alias("Sarah", "Michael Chen")
    with pytest.raises(ValueError):
        S.add_alias("Sarah", "我")


def test_link_name_hangs_a_name_where_it_appears(table):
    path = table(LEGACY)
    assert S.link_name("Connor", "present_in", "Detroit") is True
    assert S.link_name("Connor", "present_in", "Detroit") is False
    assert S.link_name("Mike", "member_of", "Book Club") is True
    names = S.load_names_table()
    assert names["Connor"].present_in == ("Detroit",)
    # A target the table did not have is added as a bare name.
    assert "Detroit" in names and names["Detroit"] == S.NameRecord(name="Detroit")
    assert names["Michael Chen"].member_of == ("Book Club",)
    assert names["Michael Chen"].aliases == ("Mike", "77")
    assert yaml.safe_load(_text(path))["Book Club"] is None
    for bad in (("Connor", "lives_in", "Detroit"), ("Connor", "present_in", "connor")):
        with pytest.raises(ValueError):
            S.link_name(*bad)


def test_writers_expand_a_flow_style_entry_in_place(table):
    path = table(NEW)
    assert S.link_name("Connor", "present_in", "Detroit: Become Human") is True
    assert S.set_kind("Detroit", "书") is True
    text = _text(path)
    assert "  present_in:\n    - Detroit\n    - 'Detroit: Become Human'\n" in text
    assert "Detroit:\n  instance_of: 书\n" in text
    assert S.load_names_table()["Connor"].present_in == ("Detroit", "Detroit: Become Human")
    assert S.add_alias("Connor", "Con") is True
    assert S.load_names_table()["Connor"].aliases == ("RK800", "Con")
    assert S.load_names_table()["Mike Chen"].aliases == ("Mike C",)


def test_mark_not_person_follows_instance_of(table):
    path = table(NEW + "Sarah:\n  - SJ\n")
    before = _text(path)
    assert S.mark_not_person("Detroit") is False, "a thing's kind already says it"
    assert _text(path) == before
    with pytest.raises(ValueError):
        S.mark_not_person("RK800")
    assert S.mark_not_person("Sarah") is True, "no kind: the older list, as always"
    assert S.load_not_person() == frozenset({"teacher", "sarah"})


def test_merge_folds_a_legacy_list_entry_into_a_mapping_entry(table):
    path = table(LEGACY + "Mike Chen:\n  instance_of: 人\n  member_of: [Book Club]\n"
                 "Connor:\n  instance_of: 人\n  member_of:\n    - Michael Chen\n")
    assert S.merge_names("Michael Chen", "Mike Chen") is True
    text = _text(path)
    assert "Michael Chen:" not in text
    assert "# an aside about Mike" not in text, "the entry's block went with it"
    assert text.startswith("# A hand-written header that explains the table.\n")
    names = S.load_names_table()
    assert "Michael Chen" not in names
    assert names["Mike Chen"].aliases == ("Michael Chen", "Mike", "77")
    assert names["Mike Chen"].instance_of == "人"
    assert names["Mike Chen"].member_of == ("Book Club",)
    # A link another entry hung on the folded name now hangs on the one it went into.
    assert names["Connor"].member_of == ("Mike Chen",)
    flat = S.load_alias_table()
    assert flat["michael chen"] == flat["mike"] == flat["77"] == "Mike Chen"
    assert yaml.safe_load(text)["Sarah"] is None, "the rest of the file is untouched"


def test_merge_skips_aliases_already_there_and_keeps_the_one_kind(table):
    table("Kara:\n  aliases: [K, AX400]\n  instance_of: 人\n  present_in: [Detroit]\n"
          "Kara Ann:\n  - k\n  - KA\n")
    assert S.merge_names("Kara", "Kara Ann") is True
    rec = S.load_names_table()["Kara Ann"]
    assert rec.aliases == ("k", "KA", "Kara", "AX400"), "K is already there as k"
    assert (rec.instance_of, rec.present_in) == ("人", ("Detroit",))
    assert list(S.load_names_table()) == ["Kara Ann"]


def test_merge_refuses_a_kind_conflict_itself_and_its_own_alias(table):
    path = table(NEW)
    before = _text(path)
    with pytest.raises(ValueError, match="游戏"):
        S.merge_names("Detroit", "Connor")
    with pytest.raises(ValueError):
        S.merge_names("Connor", "connor")
    with pytest.raises(ValueError):
        S.merge_names("Connor", "RK800")
    with pytest.raises(ValueError):
        S.merge_names("Connor", "__不是人__")
    assert _text(path) == before


def test_rename_to_a_new_spelling_creates_the_entry(table):
    path = table(LEGACY)
    assert S.merge_names("Sarah", "Sarah Jones") is True
    assert S.load_names_table()["Sarah Jones"].aliases == ("Sarah",)
    assert "\nSarah:\n" not in _text(path)
    # A name the table has never seen is simply filed as an alias.
    assert S.merge_names("SJ", "Sarah Jones") is True
    assert S.load_alias_table()["sj"] == "Sarah Jones"
    assert S.merge_names("SJ", "Sarah Jones") is False


def test_the_panel_merge_and_rename_actions_fold_names(table, tmp_path, monkeypatch):
    from starlette.requests import Request
    from core.bucket_manager import BucketManager
    from web import _shared as sh
    from web import loci as W

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods):
            def keep(fn):
                routes[(path, methods[0])] = fn
                return fn
            return keep
    W.register(_Mcp())
    action = routes[("/api/loci/subjects/action", "POST")]

    def post(body: dict) -> dict:
        raw = json.dumps(body).encode()

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": "POST", "path": "/", "headers": [
            (b"host", b"127.0.0.1:8000"), (b"origin", b"http://127.0.0.1:8000"),
            (b"content-type", b"application/json")]}, receive)
        resp = asyncio.run(action(req))
        return {"status": resp.status_code, **json.loads(resp.body)}

    table(LEGACY + "Mike Chen:\n  instance_of: 人\n")
    mgr = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    monkeypatch.setattr(sh, "bucket_mgr", mgr)
    asyncio.run(mgr.create("x", tags=["t"], subjects=["Michael Chen", "Mike Chen", "Sarah"]))
    assert len(asyncio.run(W.build_subjects())["names"]) == 3

    # The case add_alias refuses: the name being merged is a key of its own.
    out = post({"action": "merge", "name": "Michael Chen", "target": "Mike Chen"})
    assert out["status"] == 200 and out["changed"] is True, out
    rows = {r["name"]: r for r in asyncio.run(W.build_subjects())["names"]}
    assert set(rows) == {"Mike Chen", "Sarah"}
    assert rows["Mike Chen"]["n"] == 1 and rows["Mike Chen"]["kind"] == "人"
    assert rows["Mike Chen"]["variants"] == ["Michael Chen"]

    out = post({"action": "rename", "name": "Sarah", "target": "Sarah Jones"})
    assert out["status"] == 200 and out["changed"] is True, out
    assert {r["name"] for r in asyncio.run(W.build_subjects())["names"]} == \
        {"Mike Chen", "Sarah Jones"}
    out = post({"action": "merge", "name": "Sarah Jones", "target": "Sarah Jones"})
    assert out["status"] == 400


@pytest.mark.parametrize("shape", ["legacy", "mapping"])
def test_the_panel_names_screen_reads_either_shape(table, tmp_path, monkeypatch, shape):
    from core.bucket_manager import BucketManager
    from web import _shared as sh
    from web import loci as W

    table(LEGACY if shape == "legacy" else NEW)
    mgr = BucketManager({"buckets_dir": str(tmp_path / "buckets")})
    monkeypatch.setattr(sh, "bucket_mgr", mgr)
    asyncio.run(mgr.create("x", tags=["t"], subjects=["Mike", "RK800", "teacher"]))
    out = asyncio.run(W.build_subjects())
    json.dumps(out)                   # the route returns it as JSON
    rows = {r["name"]: r for r in out["names"]}
    assert out["blocked"] == [{"name": "teacher", "n": 1}]
    if shape == "legacy":
        assert set(rows) == {"Michael Chen", "RK800"}
        assert rows["Michael Chen"]["canonical"] == "Michael Chen"
        assert (rows["Michael Chen"]["kind"], rows["Michael Chen"]["present_in"]) == ("", [])
    else:
        assert set(rows) == {"Mike", "Connor"}
        assert rows["Connor"]["kind"] == "人"
        assert rows["Connor"]["present_in"] == ["Detroit"]
        assert rows["Connor"]["variants"] == ["RK800"]
        assert rows["Mike"]["canonical"] == "" and rows["Mike"]["kind"] == ""


def test_a_new_table_starts_with_its_header(table, tmp_path):
    assert S.set_kind("Detroit", "游戏") is True
    text = (tmp_path / "aliases.yaml").read_text(encoding="utf-8")
    assert text.startswith("# ")
    assert yaml.safe_load(text) == {"Detroit": {"instance_of": "游戏"}}


BROKEN = """\
Connor:
  instance_of: person
  aliases: [Con
Hank:
  - Lieutenant
"""


@pytest.mark.parametrize("write", [
    lambda: S.set_kind("Connor", "android"),
    lambda: S.set_kind("Markus", "android"),
    lambda: S.add_alias("Hank", "Anderson"),
    lambda: S.mark_not_person("Detroit"),
    lambda: S.link_name("Connor", "member_of", "CyberLife"),
    lambda: S.merge_names("Hank", "Connor"),
])
def test_a_table_that_does_not_parse_is_never_written(table, write):
    # Criterion: the writers edit the file's text line by line; on a file that does not
    # parse they cannot know what a line belongs to, and an edit would replace a kind or
    # turn aliases into top-level names. They refuse, and the file stays byte for byte.
    path = table(BROKEN)
    with pytest.raises(ValueError):
        write()
    assert _text(path) == BROKEN
