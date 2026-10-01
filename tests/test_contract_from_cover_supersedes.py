# -*- coding: utf-8 -*-
"""CONTRACT: `prov`, `cover`/`covered_by` and `supersedes`/`superseded_by` are three
different things, and none of them loses the older material.

WHAT THIS FREEZES AND WHY IT IS FROZEN NOW
    Three fields, all of them pointing from one memory to another, all of them written by
    a different verb:

        prov          where this came from          → the sources KEEP surfacing
        cover         what this gist stands for     → the members STOP surfacing alone
        supersedes    which version this replaces    → the old version STOPS surfacing

    They are easy to confuse and easy to "unify" — two of the three even have the same
    read-side effect, which is exactly the trap. A refactor that merges them looks like a
    simplification and passes anything that only checks "does the list come back". What it
    destroys is the distinction between *I thought about those* and *those are now this*,
    and nothing raises when it goes.

    `prov` is typed (W3C PROV): each line names its relation. A new version records the
    version it replaced as a wasRevisionOf line *as well as* in `supersedes` — the line is
    the typed record, the chain field is what surfacing runs on, and the line must never
    start suppressing anything.

    So: one test per field for what it suppresses, one per field for what it must NOT
    suppress, and one for the walk back down to the material underneath.

THE PART THAT MATTERS MOST
    Nothing here is ever deleted. "Stops surfacing on its own" is a display decision, not
    a storage decision — the older material stays searchable, stays reachable by id, and
    stays reachable by walking the link. Every assertion about suppression in this file is
    paired with one about reachability, and the pairing is the contract.
"""
from core import _bigevent as B
from core import _fold as F
from utils import read_from_ids, read_prov


def meta(**kw) -> dict:
    """A metadata dict with only the fields the predicates under test actually read."""
    return {"id": kw.pop("id", "bucket1"), **kw}


def line(rel: str, target: str) -> dict:
    return {"rel": rel, "target": target}


def derived(*ids) -> list[dict]:
    return [line("wasDerivedFrom", i) for i in ids]


# ───────────────────────── `prov`: sources go on surfacing ─────────────────────────

def test_prov_does_not_suppress_anything():
    # Criterion: a realization pointing at the three events it grew out of must not push
    # those events out of view. They are still things that happened; the thought about
    # them is an addition, not a replacement. If provenance ever started suppressing, a
    # busy week of thinking would quietly empty out that week's timeline.
    m = meta(prov=derived("e1", "e2", "e3"))
    assert F.is_covered(m) is False
    assert B._usable(m) is True


def test_a_revision_line_does_not_suppress_the_new_version():
    # Criterion: the new version names the old one in prov. That line is a record, not a
    # cover: read the wrong way, every regrow would hide the version it just wrote.
    m = meta(prov=[line("wasRevisionOf", "v1")], supersedes="v1")
    assert F.is_covered(m) is False
    assert B._usable(m) is True


def test_prov_reads_as_typed_lines_in_order():
    m = meta(prov=[line("wasDerivedFrom", "e1"), line("wasQuotedFrom", "m_0931"),
                   line("wasRevisionOf", "v1"), line("hadPrimarySource", "e2")])
    assert read_prov(m) == m["prov"]


def test_the_source_reader_returns_sources_only():
    # Criterion: read_from_ids answers "what does this stand on", which is what `from`
    # always meant. Its callers walk the library with it — the reverse chain, the
    # overturn walk, the sources an insight digested. A revision line there would make
    # every new version "grown out of" its predecessor; a quoted `m_…` id would be a
    # lookup that can only fail.
    m = meta(prov=[line("wasDerivedFrom", "e1"), line("wasQuotedFrom", "m_0931"),
                   line("wasRevisionOf", "v1"), line("hadPrimarySource", "e2")])
    assert read_from_ids(m) == ["e1", "e2"]


def test_an_unmigrated_from_string_reads_the_way_the_migration_converts_it():
    # Criterion: a library before schema 5 keeps its chains until it is migrated. Order is
    # kept (it is the order they were handed in) and blanks are dropped; a memory's id is
    # a source, anything else (a placeholder, a host line id) points outside the library.
    m = {"from": "aaaaaaaaaaaa, TBD ,,feel_202607041917_V050"}
    assert read_prov(m) == [line("wasDerivedFrom", "aaaaaaaaaaaa"), line("wasQuotedFrom", "TBD"),
                            line("wasDerivedFrom", "feel_202607041917_V050")]
    assert read_from_ids(m) == ["aaaaaaaaaaaa", "feel_202607041917_V050"]


def test_prov_wins_when_both_shapes_are_present():
    # Criterion: writing prov retires `from` on the same entry; a file edited by hand that
    # still carries both is read from the field the code writes.
    m = meta(prov=derived("e9"), **{"from": "aaaaaaaaaaaa,bbbbbbbbbbbb"})
    assert read_from_ids(m) == ["e9"]


def test_a_line_the_reader_cannot_understand_is_skipped_not_fatal():
    # Criterion: these readers run over the whole library; one hand-edited line must not
    # stop the sweep, and must not be read as some other relation either.
    m = meta(prov=[line("inspiredBy", "e1"), "e2", {"rel": "wasDerivedFrom"},
                   line("wasDerivedFrom", "e3")])
    assert read_prov(m) == derived("e3")


def test_the_old_field_name_is_not_read_any_more():
    # Criterion: the library migration (core/schema.py, step 1 -> 2) moves every
    # `triggered_by` into `from`, so reading it too would only hide a library that was
    # never migrated (the health page says so instead).
    assert read_from_ids({"triggered_by": "aaaaaaaaaaaa,bbbbbbbbbbbb"}) == []


def test_eight_sources_survive_the_read():
    # Criterion: the old field held five ids and silently cut the sixth in half. The
    # ceiling is now a count of lines, and nothing under it is lost.
    ids = [f"e{i}" for i in range(8)]
    assert read_from_ids(meta(prov=derived(*ids))) == ids


# ───────────────────────── `covered_by`: folded away, not gone ─────────────────────────

def test_covered_by_stops_a_memory_surfacing_on_its_own():
    # Criterion: this is what fold is for. Once a gist stands for these, they give way to
    # it in anything that lays memories out; otherwise folding would add a line instead of
    # replacing several, and the whole point was to stop reading the same thought four times.
    m = meta(covered_by=["gist1"])
    assert F.is_covered(m) is True
    assert B._usable(m) is False


def test_a_covered_memory_still_names_what_covers_it():
    # Criterion: the walk back up. Suppression without a pointer would be deletion with
    # extra steps — you could no longer answer "what did that line stand for".
    assert F.covers_of(meta(covered_by=["gist1", "gist2"])) == ["gist1", "gist2"]


def test_one_memory_can_be_covered_by_more_than_one_gist():
    # Criterion: a small memory can belong to two threads at once. That is a fact about
    # memory, not a conflict to resolve. The first version of this field was single-valued
    # and turned every crossing into a silent theft — whichever gist folded second won,
    # and the first one quietly stopped standing for it.
    m = meta(covered_by=["gist1", "gist2", "gist3"])
    assert F._covered_list(m) == ["gist1", "gist2", "gist3"]


def test_a_comma_string_from_older_data_still_reads_as_a_list():
    # Criterion: buckets written before this became a list, and buckets hand-edited in a
    # text editor, both carry a plain string. Read wide, write narrow.
    assert F._covered_list(meta(covered_by="gist1, gist2")) == ["gist1", "gist2"]


def test_the_gist_names_everything_it_stands_for():
    # Criterion: the walk back down. Both directions are written at fold time on purpose —
    # the filter that runs over the whole library every round needs one field it can read
    # without building an index, and the drill-down needs the other.
    assert F.cover_ids(meta(cover=["a", "b", "c"])) == ["a", "b", "c"]
    assert F.cover_ids(meta(cover="a, b")) == ["a", "b"]


def test_holding_a_cover_list_does_not_make_the_gist_itself_covered():
    # Criterion: THE test that keeps the two directions apart. A gist covers others; it is
    # not itself covered. Read the wrong field here and every gist disappears from view the
    # moment it is written — the one memory that was supposed to be the visible one.
    gist = meta(cover=["a", "b"], tags=[F.GIST_TAG])
    assert F.is_covered(gist) is False
    assert B._usable(gist) is True
    assert F.is_gist(gist) is True


# ───────────────────────── `superseded_by`: the previous version ─────────────────────────

def test_superseded_by_stops_the_old_version_surfacing():
    # Criterion: regrow puts a new version in place. Both versions surfacing would mean
    # "I changed my mind" reads as two contradictory beliefs held at once.
    m = meta(superseded_by="v2")
    assert F.is_covered(m) is True
    assert B._usable(m) is False


def test_the_old_version_still_names_its_successor():
    # Criterion: the version chain is walkable, so "what did I think before" has an answer.
    assert F.covers_of(meta(superseded_by="v2")) == ["v2"]


def test_superseded_and_covered_are_read_together_but_stored_apart():
    # Criterion: read side treats them alike (both mean "do not surface alone"), write side
    # never does (one is a revision, the other is a fold). This asserts both halves at once:
    # the combined read includes each of them, and the cover-only read excludes the revision.
    m = meta(covered_by=["gist1"], superseded_by="v2")
    assert F.covers_of(m) == ["gist1", "v2"]
    assert F._covered_list(m) == ["gist1"], "the revision pointer is not a fold and must not be reported as one"


def test_a_superseded_pointer_is_not_duplicated_when_it_is_already_in_the_cover_list():
    # Criterion: fold writes the superseded id into `covered_by` in one particular path
    # (regrow-through-fold). Reporting it twice would double-count it in anything that
    # counts how many gists stand over a memory.
    m = meta(covered_by=["gist1", "v2"], superseded_by="v2")
    assert F.covers_of(m) == ["gist1", "v2"]


# ───────────────────── the three of them do not leak into one another ─────────────────────

def test_the_three_fields_are_independent():
    # Criterion: the summary of this whole file. Setting one must not imply another. Written
    # as one table because the failure this guards against is a refactor that "simplifies"
    # them into one field — and that failure shows up as a whole row changing at once.
    sources_only = meta(prov=derived("e1", "e2"))
    folded_only = meta(covered_by=["gist1"])
    revised_only = meta(superseded_by="v2")

    assert (read_from_ids(sources_only), F.covers_of(sources_only)) == (["e1", "e2"], [])
    assert (read_from_ids(folded_only), F.covers_of(folded_only)) == ([], ["gist1"])
    assert (read_from_ids(revised_only), F.covers_of(revised_only)) == ([], ["v2"])

    # And the same table read through the fold-only lens. This row is the one that catches
    # a merge of the two fields: with a `covered_by` present the merged read short-circuits
    # and looks correct, so the only case that can see the difference is a memory that was
    # revised and never folded. The mutation check found this hole; it did not start here.
    assert F._covered_list(sources_only) == []
    assert F._covered_list(folded_only) == ["gist1"]
    assert F._covered_list(revised_only) == [], \
        "a revised memory was not folded — reporting its predecessor as a fold makes " \
        "'what does this gist stand for' answer with version-chain material it never covered"


def test_a_memory_can_be_all_three_at_once_without_them_interfering():
    # Criterion: real buckets are like this — grown out of events, a new version of an
    # older one, later folded, later revised again. Each field still answers only its own
    # question: the revision line is the typed record of the chain, not a source.
    m = meta(prov=derived("e1", "e2") + [line("wasRevisionOf", "v0")], supersedes="v0",
             covered_by=["gist1"], superseded_by="v2")
    assert read_from_ids(m) == ["e1", "e2"]
    assert F._covered_list(m) == ["gist1"]
    assert F.covers_of(m) == ["gist1", "v2"]
    assert F.is_covered(m) is True


def test_none_of_the_readers_choke_on_a_bucket_that_has_none_of_them():
    # Criterion: the ordinary case is a memory with no links at all. Every reader here has
    # to answer "nothing" rather than raise, because these run over the whole library.
    plain = meta()
    assert read_prov(plain) == []
    assert read_from_ids(plain) == []
    assert F.covers_of(plain) == []
    assert F.cover_ids(plain) == []
    assert F.is_covered(plain) is False
    assert B._usable(plain) is True


def test_the_readers_survive_a_non_dict():
    # Criterion: a damaged frontmatter block parses to something that is not a dict. These
    # predicates run over every file, so one bad file must not stop the sweep.
    assert F.is_covered(None) is False
    assert F.covers_of("not a dict") == []
    assert F.cover_ids(None) == []
    assert read_from_ids(None) == []
    assert read_prov("not a dict") == []
    assert read_prov(meta(prov="e1,e2")) == []
