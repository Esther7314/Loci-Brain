# -*- coding: utf-8 -*-
"""
tests/test_hosts_fetch_contract.py — the partner team's review of the hosts/fetch contract.

WHAT IS AGREED
    A bare HTTP 410 from a host's fetch endpoint says nothing about any one source: this
    call only, nothing held, nothing recorded, the next read asks again. 502 / 503 / 504
    are the host's side down for now: the memory's own body stands in, as for a timeout.
    A TLS or certificate failure is never "unreachable": nothing is fed, nothing held.
    A run's line list comes from the change authority of its lines or from the host the
    table entrusts with registering them (`registers:`); material nobody is the authority
    of is no longer open to every host within its ceiling. Two hosts declaring the same
    place at the same depth still name neither there, but the table says so: both hosts
    load, `errors` names them, and the setup screen shows it red.

WHAT IS UNDER TEST HERE
    core/_originals.fetch and recall(view="original") against a fake host; core/scope's
    `registers` key, `registrar_for` and collision report; core/_source_change.
    registration_refusal through handle_lines and a slicing batch; web/loci.build_setup.
"""

import logging
import ssl

import httpx
import pytest

from core import _invalidation as I
from core import _originals as O
from core import _slicer as SL
from core import _source_change as SC
from core import _sources as S
from core import scope as SCOPE
from core.bucket_manager import BucketManager

from test_host_table_lock import deploy  # noqa: F401
from test_source_originals import (BODY, M, PHRASE, FakeHost, _serving, given,  # noqa: F401
                                   host, library, original, run)


def _held_records(store, bid):
    meta = run(store.get_including_archive(bid))["metadata"]
    return I.open_records(meta, I.SOURCE_HELD)


# ───────────────────────── 410 is the endpoint's word, not the source's ─────────────────────────

def test_a_bare_410_holds_nothing_and_the_next_read_asks_again(library, host, caplog):
    store, e, root = library
    host.answer = (410, {})
    assert run(O.fetch(M, hosts=_serving(host.url))).holds == (), "nothing to hold"
    caplog.set_level(logging.WARNING, logger="loci_brain.originals")
    out = original(e)
    assert out.splitlines()[0].startswith("原话不许看了"), out
    assert BODY not in out and "海边的约定" not in out, "not fed this time"
    assert "410" in out and "下次再问" in out
    sid = S.record_id(M)
    assert store.sources.state_of(sid) == S.ACTIVE
    assert store.sources.held_of(sid) is None, "no hold row"
    assert _held_records(store, e) == [], "no source_held record"
    assert not (root / S.SOURCES_DIR / S.CHANGES_FILE).exists()
    assert any("http_410" in r.getMessage() and "nothing is held" in r.getMessage()
               for r in caplog.records), "logged as the endpoint's fault"
    # The next read asks again, and a good answer is taken.
    asked = len(host.requests)
    host.answer = given({"id": "m_0003", "revision": "r1", "text": PHRASE})
    out = original(e)
    assert len(host.requests) == asked + 1
    assert out.splitlines()[0].startswith("原话：宿主给了") and PHRASE in out


def test_withdrawn_said_in_a_200_answer_still_holds(host):
    host.answer = {"v": 1, "status": "not_allowed", "reason": "deleted"}
    answer = run(O.fetch(M, hosts=_serving(host.url)))
    assert answer.holds == (("lento:home/private:U#m_0003", "deleted"),)


# ───────────────────────── 502 / 503 / 504 are temporary ─────────────────────────

@pytest.mark.parametrize("status", [502, 503, 504])
def test_a_gateway_status_is_unavailable_and_the_body_stands_in(library, host, status):
    store, e, _root = library
    host.answer = (status, {})
    answer = run(O.fetch(M, hosts=_serving(host.url)))
    assert (answer.outcome, answer.why, answer.holds) == (O.UNAVAILABLE, f"http_{status}", ())
    out = original(e)
    assert out.splitlines()[0].startswith("原话暂时取不到"), out
    assert f"HTTP {status}" in out
    assert out.rstrip().endswith(BODY)
    assert store.sources.state_of(S.record_id(M)) == S.ACTIVE


def test_other_statuses_still_let_nothing_through(host):
    for status in (500, 501, 429):
        host.answer = (status, {})
        answer = run(O.fetch(M, hosts=_serving(host.url)))
        assert (answer.outcome, answer.reason) == (O.NOT_ALLOWED, f"http_{status}"), status


# ───────────────────────── a TLS failure is not "unreachable" ─────────────────────────

def _raising(exc):
    def handler(request):
        raise exc
    return httpx.MockTransport(handler)


def _connect_error(cause):
    try:
        try:
            raise cause
        except BaseException as inner:
            raise httpx.ConnectError(str(inner)) from inner
    except httpx.ConnectError as outer:
        return outer


def test_a_certificate_failure_is_not_allowed_and_a_refused_connect_is_unavailable():
    hosts = _serving("https://host.example/api/loci/source")
    bad_cert = _connect_error(ssl.SSLCertVerificationError(
        1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self-signed"))
    answer = run(O.fetch(M, hosts=hosts, transport=_raising(bad_cert)))
    assert (answer.outcome, answer.reason, answer.holds) == (O.NOT_ALLOWED, O.TLS_FAILED, ())
    refused = _connect_error(ConnectionRefusedError(10061, "No connection could be made"))
    answer = run(O.fetch(M, hosts=hosts, transport=_raising(refused)))
    assert (answer.outcome, answer.why) == (O.UNAVAILABLE, O.UNREACHABLE)
    no_name = _connect_error(OSError(11001, "getaddrinfo failed"))
    assert run(O.fetch(M, hosts=hosts, transport=_raising(no_name))).why == O.UNREACHABLE


def test_https_to_a_host_that_does_not_speak_tls_is_a_tls_failure(host):
    url = host.url.replace("http://", "https://")
    answer = run(O.fetch(M, hosts=_serving(url), settings=O.Settings(timeout_seconds=3.0)))
    assert (answer.outcome, answer.reason) == (O.NOT_ALLOWED, O.TLS_FAILED), answer


def test_a_tls_failure_feeds_nothing_and_holds_nothing(library, monkeypatch):
    store, e, root = library

    async def bad_cert(*args, **kwargs):
        raise _connect_error(ssl.SSLCertVerificationError(
            1, "[SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed"))
    monkeypatch.setattr(O, "_post", bad_cert)
    out = original(e)
    assert out.splitlines()[0].startswith("原话不许看了"), out
    assert BODY not in out and "TLS" in out
    assert store.sources.state_of(S.record_id(M)) == S.ACTIVE
    assert store.sources.held_of(S.record_id(M)) is None
    assert _held_records(store, e) == []


# ───────────────────────── a line list needs provenance ─────────────────────────

def _table(**spec):
    env = {f"T_{name.upper()}": f"{name}-key" for name in spec}
    table = {name: {"token_env": f"T_{name.upper()}", **body} for name, body in spec.items()}
    return SCOPE.load_hosts({"hosts": table}, env)


def _lines(source, *ids, revision=None):
    return {"source": source, "revision": revision, "lines": list(ids)}


@pytest.fixture
def store(tmp_path):
    return BucketManager({"buckets_dir": str(tmp_path)})


LENTO = [{"system": "lento"}]
HOME = [{"system": "lento", "instance": "home"}]


def test_registers_is_a_host_key_within_its_ceiling():
    hosts = _table(r={"max_grant": LENTO, "registers": HOME})
    assert hosts.get("r").registers == (S.Place("lento", "home"),)
    assert hosts.registrar_for("lento:home/c#m_1").name == "r"
    assert hosts.registrar_for("lento:work/c#m_1") is None
    past = _table(r={"max_grant": HOME, "registers": [{"system": "telegram"}]})
    assert past.get("r") is None and any("registers" in e for e in past.errors)
    assert hosts.registrar_for("import:imp_1/c0001#l0001") is None


def test_a_line_list_comes_from_the_authority_or_its_registrar(store):
    hosts = _table(a={"max_grant": LENTO, "authority": HOME},
                   r={"max_grant": LENTO, "registers": HOME},
                   c={"max_grant": LENTO})
    a, r, c = hosts.get("a"), hosts.get("r"), hosts.get("c")

    def submit(body, who):
        status, out = run(SC.handle_lines(store, body, who, hosts=hosts))
        assert status == 200
        return out["status"], out.get("note")
    home = _lines("lento:home/c#m_1..m_3", "m_1", "m_2", "m_3")
    assert submit(home, c) == ("forbidden", SC.NOT_AUTHORITY)
    assert store.sources.members_of("lento:home/c#m_1..m_3") is None
    assert submit(home, r) == ("recorded", None), "the entrusted registrar"
    assert submit(_lines("lento:home/d#m_1..m_2", "m_1", "m_2"), a) == ("recorded", None)
    work = _lines("lento:work/c#m_1..m_2", "m_1", "m_2")
    assert submit(work, c) == ("forbidden", SC.NOT_REGISTRAR), "nobody's: not open to all"
    assert submit(work, r) == ("forbidden", SC.NOT_REGISTRAR), "r registers home only"
    assert store.sources.members_of("lento:work/c#m_1..m_2") is None


def test_a_line_list_over_an_authority_tie_is_no_change_authority(store):
    tie = [{"system": "lento", "instance": "home", "container": "t"}]
    hosts = _table(x={"max_grant": LENTO, "authority": tie},
                   y={"max_grant": LENTO, "authority": tie},
                   c={"max_grant": LENTO})
    body = _lines("lento:home/t#m_1..m_2", "m_1", "m_2")
    for who in ("x", "y", "c"):
        _s, out = run(SC.handle_lines(store, body, hosts.get(who), hosts=hosts))
        assert (out["status"], out["note"]) == ("forbidden", SC.NO_AUTHORITY), who


def test_without_a_table_the_legacy_host_registers_as_before(store):
    hosts = SCOPE.load_hosts({}, {}, legacy_token="life")
    body = _lines("lento:home/c#m_1..m_2", "m_1", "m_2")
    _s, out = run(SC.handle_lines(store, body, hosts.default, hosts=hosts))
    assert out["status"] == "recorded"


def test_a_slicing_batch_needs_the_same_provenance(store):
    hosts = _table(r={"max_grant": LENTO, "registers": HOME}, c={"max_grant": LENTO})
    called = []

    async def side_model(system, user):
        called.append(1)
        return '{"slices": []}'
    batch = {"source": {"system": "lento", "instance": "home", "container": "d"},
             "day": "2026-10-01", "lines": [{"id": "m_1", "text": "x"}, {"id": "m_2", "text": "y"}]}
    with pytest.raises(SL.BatchForbidden) as got:
        run(SL.take_batch(store, batch, model=side_model, host=hosts.get("c"), hosts=hosts))
    assert SC.NOT_REGISTRAR in str(got.value) and called == []
    assert SC.registration_refusal(hosts, hosts.get("r"), batch["source"], ["m_1", "m_2"]) == ""


def test_one_watermark_is_one_delivery_and_another_is_a_new_one(store):
    who = SCOPE.Host("bridge", max_grant=(S.Place("lento", "home"),))
    first = {"source": "lento:home/c#m_1..m_3", "revision": "w-1",
             "lines": [{"id": "m_1", "revision": "e1"}, "m_2", "m_3"]}
    assert run(SC.handle_lines(store, first, who))[1]["status"] == "recorded"
    clash = {**first, "lines": [{"id": "m_1", "revision": "e9"}, "m_2", "m_3"]}
    _s, out = run(SC.handle_lines(store, clash, who))
    assert out["status"] == "conflict" and "m_1" in out["note"]
    later = {"source": "lento:home/c#m_1..m_3", "revision": "w-2",
             "lines": [{"id": "m_1", "revision": "e9"}, "m_3"]}
    assert run(SC.handle_lines(store, later, who))[1]["status"] == "recorded", "a new delivery"
    assert store.sources.members_of("lento:home/c#m_1..m_3") == ["m_1", "m_2", "m_3"]


# ───────────────────────── same-depth collisions are said ─────────────────────────

CHAT = {"system": "lento", "instance": "home", "container": "private:U"}


def test_a_same_depth_collision_is_reported_and_refuses_only_that_place():
    hosts = _table(a={"max_grant": LENTO, "authority": [CHAT, {"system": "lento",
                                                                "instance": "work"}]},
                   b={"max_grant": LENTO, "authority": [CHAT, {"system": "lento",
                                                                "instance": "shop"}]})
    assert hosts.get("a") is not None and hosts.get("b") is not None, "both still load"
    [said] = hosts.collisions
    assert said in hosts.errors
    assert "a and b" in said and "authority" in said and "lento/home/private:U" in said
    assert hosts.authority_for("lento:home/private:U#m_1") is None
    assert hosts.authority_for("lento:work/c#m_1").name == "a"
    assert hosts.authority_for("lento:shop/c#m_1").name == "b"
    assert [h.name for h in hosts.claimants("authority", "lento:home/private:U#m_1")] == [
        "a", "b"]


def test_collisions_are_per_key_and_a_deeper_place_is_not_one():
    hosts = _table(a={"max_grant": LENTO, "provides": HOME, "registers": HOME,
                      "authority": HOME, "fetch_url": "http://127.0.0.1:1/x"},
                   b={"max_grant": LENTO, "provides": HOME, "registers": HOME,
                      "authority": [CHAT]})
    keys = sorted(k for k in ("authority", "provides", "registers")
                  if any(f" {k} " in c for c in hosts.collisions))
    assert keys == ["provides", "registers"], hosts.collisions
    assert hosts.provider_for("lento:home/c#m_1") is None
    assert hosts.registrar_for("lento:home/c#m_1") is None
    assert hosts.authority_for("lento:home/private:U#m_1").name == "b", "deeper wins"


def test_the_setup_screen_shows_a_collision_red(deploy):  # noqa: F811
    from web import loci as Wb
    table = {"hosts": {"legacy": {"token_env": "T_L", "scope_mode": "open",
                                  "authority": [CHAT]},
                       "bot": {"token_env": "T_B", "scope_mode": "open",
                               "authority": [CHAT]}}}
    deploy(table, password=True)
    rows = {r["key"]: r for r in run(Wb.build_setup())["rows"]}
    row = rows["hosts_collision"]
    assert row["ok"] is False and not row["note"]
    assert "legacy and bot" in row["now"] and "lento/home/private:U" in row["now"]
    deploy({"hosts": {"legacy": {"token_env": "T_L", "scope_mode": "open"}}}, password=True)
    assert "hosts_collision" not in {r["key"] for r in run(Wb.build_setup())["rows"]}
