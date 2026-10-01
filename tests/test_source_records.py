# -*- coding: utf-8 -*-
"""
tests/test_source_records.py — the stored form of a source record and its string form.

A record names one piece of the host's material by system + instance + container + id;
revision, fingerprint and span ride along but are not identity. The string form is what
a prov line, the registry and the host's change feed all use, so it has to round-trip
and must never be cut. Malformed records are refused whole, never trimmed to fit.
"""

import pytest

from core import _sources as S

REC = {"system": "lento", "instance": "home", "container": "private:U",
       "id": "m_20260925_0142"}


def test_a_full_record_is_kept_with_every_field():
    [rec] = S.normalize_sources([{**REC, "revision": 3, "fingerprint": "sha256:9c1e",
                                  "fingerprint_by": "adapter",
                                  "span": {"unit": "utf16", "start": 120, "end": 188},
                                  "use": "private"}])
    assert rec == {**REC, "revision": "3", "fingerprint": "sha256:9c1e",
                   "fingerprint_by": "adapter",
                   "span": {"unit": "utf16", "start": 120, "end": 188}, "use": "private"}


def test_absent_optional_fields_are_null_and_no_span_is_written():
    [rec] = S.normalize_sources(REC)
    assert rec["revision"] is None and rec["fingerprint"] is None and rec["use"] is None
    assert "span" not in rec


def test_string_form_round_trips_with_and_without_revision():
    [rec] = S.normalize_sources([{**REC, "revision": "7"}])
    assert S.record_string(rec) == "lento:home/private:U#m_20260925_0142@7"
    sid, revision = S.SourceId.parse("lento:home/private:U#m_20260925_0142@7")
    assert (sid, revision) == (S.record_id(rec), "7")
    sid, revision = S.SourceId.parse(str(sid))
    assert revision is None and sid.container == "private:U"


@pytest.mark.parametrize("bad", ["m_0142", "lento#m_1", "lento:home#m_1", "lento:home/c#",
                                 "lento:home/c#m 1"])
def test_anything_but_a_full_string_form_is_refused(bad):
    with pytest.raises(S.SourceRecordError):
        S.SourceId.parse(bad)


@pytest.mark.parametrize("field", ["system", "instance", "container", "id"])
def test_each_identity_field_is_required(field):
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources([{**REC, field: " "}])


@pytest.mark.parametrize("field, value", [("system", "a:b"), ("instance", "a/b"),
                                          ("container", "a#b"), ("id", "m@1"),
                                          ("revision", "1#2"), ("id", "m 1")])
def test_a_character_that_would_split_the_string_form_is_refused(field, value):
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources([{**REC, field: value}])


@pytest.mark.parametrize("span", [{"unit": "bytes", "start": 0, "end": 1},
                                  {"unit": "utf16", "start": 5, "end": 5},
                                  {"unit": "utf16", "start": -1, "end": 3},
                                  {"unit": "utf16", "start": "0", "end": 3},
                                  {"unit": "utf16", "start": True, "end": 3},
                                  {"unit": "char", "start": 0, "end": 3, "len": 3}])
def test_a_bad_span_is_refused(span):
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources([{**REC, "span": span}])


def test_unknown_keys_are_refused_not_dropped():
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources([{**REC, "window": "w1"}])


def test_the_same_piece_twice_is_kept_once_but_two_spans_are_two():
    span_a = {"unit": "char", "start": 0, "end": 4}
    span_b = {"unit": "char", "start": 4, "end": 9}
    out = S.normalize_sources([{**REC, "span": span_a}, {**REC, "span": span_a},
                               {**REC, "span": span_b}, REC, REC])
    assert [r.get("span") for r in out] == [span_a, span_b, None]


def test_more_than_the_cap_is_refused_whole():
    many = [{**REC, "id": f"m_{i}"} for i in range(S.SOURCES_MAX + 1)]
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources(many)
    assert len(S.normalize_sources(many[:-1])) == S.SOURCES_MAX


def test_a_string_form_longer_than_a_prov_target_is_refused():
    long = {**REC, "container": "c" * 64, "id": "i" * 64}
    with pytest.raises(S.SourceRecordError) as exc:
        S.normalize_sources([long])
    assert exc.value.zh   # a tool can say why in its own words


def test_same_delivery_is_identity_plus_fingerprint():
    a = {**REC, "fingerprint": "sha256:aa", "revision": None}
    assert S.same_delivery(a, {**a, "revision": "2"})
    assert not S.same_delivery(a, {**a, "fingerprint": "sha256:bb"})
    assert not S.same_delivery(a, {**a, "id": "m_other"})
    plain = {**REC, "fingerprint": None, "revision": "1"}
    assert S.same_delivery(plain, dict(plain))
    assert not S.same_delivery(plain, {**plain, "revision": "2"})


def test_a_json_string_argument_is_read_as_records():
    raw = '[{"system": "lento", "instance": "home", "container": "c", "id": "m_1"}]'
    assert S.normalize_sources(S.coerce_sources_arg(raw))[0]["id"] == "m_1"
    assert S.coerce_sources_arg("  ") is None
