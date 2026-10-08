# -*- coding: utf-8 -*-
"""
tests/test_present_proxy.py — the panel's present page through Loci, and a host's day
files in the export package.

The present layer lives in a gateway; the panel asks Loci, and Loci forwards to the host
whose `hosts.<name>.present_url` is set (web/loci_present.py, core/scope.py):

  · the path is an allowlist, matched exactly: anything else under /api/loci/present is a
    404 and the gateway hears nothing of it;
  · no host with a present_url is `{"connected": false}`; a gateway that cannot be
    reached adds `error` (200 on a GET, 502 on a write);
  · the gateway receives Loci's fetch credential toward that host as Bearer, on the
    same-named /present path, the `host` key taken off; its reply comes back byte for
    byte, status included, uncached;
  · the gateway's private data (its window's compressed text) never shows in a proxy
    reply or in the export package.

The export package carries `_hosts/<name>/days/*.jsonl` and `_hosts/<name>/reports/*.md`,
never a report's `.err.json` nor anything else a host keeps there, and an import into an
empty library puts them back (core/export_package.py).

The fake gateway is an in-process httpx transport: no socket is opened.
"""

import asyncio
import json
import os
import zipfile

import httpx
import pytest

from _panel_kit import Reply, make_store
from core import export_package as EP
from core import scope as S
from test_package_into_a_living_library import _export, _import, _library, run

SENTINEL = "SENTINEL-carry-7c1f-the-window-he-compressed"
ERR_SENTINEL = "SENTINEL-err-0b9e-upstream-said-no"
PASSPHRASE = "pass-4d2a"
OUT_KEY = "loci-to-gateway-key"
IN_KEY = "gateway-to-loci-key"


# ───────────────────────── the fake gateway ─────────────────────────

class FakeGateway:
    """Answers the gateway's /present routes. Its private state holds SENTINEL, as a
    real gateway's thread state holds the compressed window; no reply carries it."""

    def __init__(self):
        self.private = {"threads": {"t_3f9c1a": {"carry": SENTINEL, "mark": "m_20261007_0001"}}}
        self.seen = []
        self.replies = {}
        self.fail = None

    def reply(self, method, path, status, raw: bytes, ctype="application/json"):
        self.replies[(method, path)] = (status, raw, ctype)

    def handler(self, request: httpx.Request) -> httpx.Response:
        body = request.content.decode("utf-8") if request.content else ""
        self.seen.append({"method": request.method, "host": request.url.host,
                          "path": request.url.path, "query": request.url.query.decode(),
                          "auth": request.headers.get("authorization"),
                          "body": json.loads(body) if body else None})
        if self.fail is not None:
            raise self.fail
        status, raw, ctype = self.replies.get(
            (request.method, request.url.path),
            (200, json.dumps({"ok": True}).encode(), "application/json"))
        return httpx.Response(status, content=raw, headers={"content-type": ctype})


STATUS_RAW = json.dumps({
    "host": "gateway", "connected": True,
    "settings": {"compress": {"on": True, "context_tokens": 128000, "ask_pct": 75,
                              "keep_raw": 40, "weak_pct": [65], "force_pct": 85},
                 "push": {"bark_set": True, "bark_hint": "https://api.day.app/••••7Q"}},
    "status": {"compress": {"thread": "t_3f9c1a", "fill_pct": 58,
                            "last": {"at": "2026-10-07T15:02:11+08:00", "how": "self",
                                     "how_words": "他自己压的"}}}},
    ensure_ascii=False, indent=1).encode("utf-8")


# ───────────────────────── calling the panel's routes ─────────────────────────

def _routes(monkeypatch, *, session=True):
    """call(method, path, query="", body=None, origin=True, key=None) -> Reply, through
    the gate as web/loci.register wires it (a locked panel; logged in unless `session`
    is False)."""
    from starlette.requests import Request
    import web
    from web import loci as Wb
    from web import panel_auth as PA

    table = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                table[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    monkeypatch.setattr(PA, "mcp_auth_on", lambda: True)
    monkeypatch.setattr(PA, "has_session", lambda r: session)
    monkeypatch.setattr(PA, "hook_token", lambda: "s3cret")

    def call(method, path, query="", body=None, origin=True, key=None):
        assert path.startswith("/api/loci/present")
        rest = path[len("/api/loci/present"):]
        if rest:
            assert rest.startswith("/")
            route, params = "/api/loci/present/{rest:path}", {"rest": rest[1:]}
        else:
            route, params = "/api/loci/present", {}
        headers = [(b"host", b"testserver"), (b"content-type", b"application/json")]
        if origin:
            headers.append((b"origin", b"http://testserver"))
        if key:
            headers.append((b"x-loci-hook-token", key.encode()))
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "headers": headers,
                       "query_string": query.encode("utf-8"), "path_params": params},
                      receive)
        response = asyncio.run(table[(method, route)](req))
        reply = Reply(response)
        reply.headers = dict(response.headers)
        return reply
    return call


def _gateway_host(name="gateway", url=f"http://gateway.test:3100/{PASSPHRASE}", **extra):
    return {"token_env": f"T_{name.upper()}_IN", "fetch_token_env": f"T_{name.upper()}_OUT",
            "max_grant": [{"system": "gateway", "instance": name}],
            "provides": [{"system": "gateway", "instance": name}],
            "present_url": url, **extra}


@pytest.fixture
def panel(tmp_path, monkeypatch):
    from web import _shared as sh
    from web import loci_present as LP
    make_store(tmp_path / "lib", monkeypatch)
    gw = FakeGateway()
    monkeypatch.setattr(LP, "_transport", httpx.MockTransport(gw.handler))
    sh.config["hosts"] = {"gateway": _gateway_host()}
    monkeypatch.setenv("T_GATEWAY_IN", IN_KEY)
    monkeypatch.setenv("T_GATEWAY_OUT", OUT_KEY)
    monkeypatch.delenv("LOCI_HOOK_TOKEN", raising=False)
    return {"gw": gw, "call": _routes(monkeypatch), "config": sh.config}


ALLOWED = [("GET", ""), ("POST", ""), ("POST", "/compress"), ("POST", "/report"),
           ("POST", "/push-test"), ("GET", "/prompts"), ("POST", "/prompts")]


# ───────────────────────── present_url in the hosts table ─────────────────────────

def test_present_url_is_parsed_with_the_host_and_checked():
    hs = S.load_hosts({"hosts": {"gateway": _gateway_host(url="http://127.0.0.1:3100/")}},
                      {"T_GATEWAY_IN": IN_KEY, "T_GATEWAY_OUT": OUT_KEY})
    host = hs.get("gateway")
    assert host.present_url == "http://127.0.0.1:3100" and host.fetch_token == OUT_KEY
    assert S.load_hosts({"hosts": {"g": {"token_env": "T"}}}, {}).get("g").present_url == ""
    for bad in ("ftp://gw.test", "http://u:p@gw.test", "http://gw.test/?k=1",
                "http://gw.test/#x", "gw.test:3100", "http:///present"):
        hs = S.load_hosts({"hosts": {"gateway": _gateway_host(url=bad)}}, {})
        assert hs.get("gateway") is None, bad
        assert any("present_url" in e for e in hs.errors), (bad, hs.errors)


# ───────────────────────── the allowlist ─────────────────────────

@pytest.mark.parametrize("method, path", [
    ("GET", "/api/loci/present/compress"),
    ("GET", "/api/loci/present/report"),
    ("GET", "/api/loci/present/push-test"),
    ("POST", "/api/loci/present/settings"),
    ("POST", "/api/loci/present/"),
    ("GET", "/api/loci/present/prompts/"),
    ("POST", "/api/loci/present/compress/now"),
    ("GET", "/api/loci/present/../../v1/models"),
    ("POST", "/api/loci/present/%2e%2e/loci/source"),
    ("GET", "/api/loci/present/Prompts"),
    ("POST", "/api/loci/present/report?kind=now"),
])
def test_a_path_off_the_allowlist_is_a_404_and_never_reaches_the_gateway(panel, method,
                                                                         path):
    out = panel["call"](method, path, body={} if method == "POST" else None)
    assert out.status == 404, (path, out.json)
    assert panel["gw"].seen == []


def test_the_allowlist_is_exactly_the_present_page(panel):
    from web import loci_present as LP
    assert LP.PRESENT_PATHS == frozenset(ALLOWED)
    for method, sub in ALLOWED:
        out = panel["call"](method, "/api/loci/present" + sub,
                            body={} if method == "POST" else None)
        assert out.status == 200, (method, sub, out.json)
    assert [(s["method"], s["path"]) for s in panel["gw"].seen] == [
        (m, f"/{PASSPHRASE}/present{sub}") for m, sub in ALLOWED]


# ───────────────────────── not connected ─────────────────────────

def test_no_host_with_a_present_url_is_not_connected(panel):
    panel["config"]["hosts"] = {"gateway": _gateway_host(url="")}
    for method, sub in ALLOWED:
        out = panel["call"](method, "/api/loci/present" + sub,
                            body={} if method == "POST" else None)
        assert out.status == 200 and out.json == {"connected": False}, (method, sub)
    assert panel["gw"].seen == []


def test_no_hosts_table_at_all_is_not_connected(panel):
    panel["config"].pop("hosts")
    out = panel["call"]("GET", "/api/loci/present")
    assert out.status == 200 and out.json == {"connected": False}
    assert panel["gw"].seen == []


@pytest.mark.parametrize("failure", [httpx.ConnectError("refused"),
                                     httpx.ReadTimeout("slow"),
                                     httpx.RemoteProtocolError("cut")])
def test_a_gateway_that_cannot_be_reached_is_not_connected_with_why(panel, failure):
    panel["gw"].fail = failure
    out = panel["call"]("GET", "/api/loci/present")
    assert out.status == 200, out.json
    assert out.json["connected"] is False and out.json["host"] == "gateway"
    assert out.json["error"] and PASSPHRASE not in out.raw.decode("utf-8")
    write = panel["call"]("POST", "/api/loci/present/compress", body={})
    assert write.status == 502 and write.json["connected"] is False and write.json["error"]


def test_a_gateway_refusing_lokis_key_is_not_passed_on_as_the_panels_own_401(panel):
    gw = panel["gw"]
    gw.reply("GET", f"/{PASSPHRASE}/present", 401, b'{"error":"bad token"}')
    out = panel["call"]("GET", "/api/loci/present")
    assert out.status == 200 and out.json["connected"] is False
    assert "LOCI_GATEWAY_TOKEN" in out.json["error"]


def test_a_reply_that_is_not_json_or_redirects_does_not_pass(panel):
    gw = panel["gw"]
    gw.reply("GET", f"/{PASSPHRASE}/present", 200, b"<html>hi</html>", "text/html")
    out = panel["call"]("GET", "/api/loci/present")
    assert out.json["connected"] is False and b"<html>" not in out.raw
    gw.reply("GET", f"/{PASSPHRASE}/present", 302, b"", "text/plain")
    out = panel["call"]("GET", "/api/loci/present")
    assert out.json["connected"] is False and "302" in out.json["error"]


def test_a_host_without_a_fetch_credential_is_not_asked(panel, monkeypatch):
    monkeypatch.delenv("T_GATEWAY_OUT")
    out = panel["call"]("GET", "/api/loci/present")
    assert out.status == 200 and out.json["connected"] is False
    assert "fetch_token_env" in out.json["error"]
    assert panel["gw"].seen == []


# ───────────────────────── what the gateway receives, what comes back ─────────────────────────

def test_the_gateway_gets_lokis_fetch_key_as_bearer_and_its_reply_comes_back_unchanged(panel):
    gw = panel["gw"]
    gw.reply("GET", f"/{PASSPHRASE}/present", 200, STATUS_RAW)
    out = panel["call"]("GET", "/api/loci/present", query="host=gateway&thread=t_3f9c1a")
    assert out.status == 200 and out.raw == STATUS_RAW
    seen = gw.seen[-1]
    assert seen["auth"] == f"Bearer {OUT_KEY}" and IN_KEY not in str(seen)
    assert (seen["host"], seen["path"]) == ("gateway.test", f"/{PASSPHRASE}/present")
    assert seen["query"] == "thread=t_3f9c1a", "host is Loci's routing, not the gateway's"


def test_a_write_forwards_its_body_without_host_and_any_status_passes_through(panel):
    gw = panel["gw"]
    raw = '{"error" : "已经有一份在写",  "kind":"now"}'.encode("utf-8")
    gw.reply("POST", f"/{PASSPHRASE}/present/report", 409, raw)
    out = panel["call"]("POST", "/api/loci/present/report",
                        body={"host": "gateway", "kind": "now"})
    assert out.status == 409 and out.raw == raw
    assert gw.seen[-1]["body"] == {"kind": "now"}
    bad = b'{"error":"compress.ask_pct","field":"compress.ask_pct"}'
    gw.reply("POST", f"/{PASSPHRASE}/present", 400, bad)
    patch = {"patch": {"compress": {"ask_pct": 120}}}
    out = panel["call"]("POST", "/api/loci/present", body=patch)
    assert out.status == 400 and out.raw == bad and gw.seen[-1]["body"] == patch


def test_the_reply_is_not_cached(panel):
    gw = panel["gw"]
    gw.reply("GET", f"/{PASSPHRASE}/present/prompts", 200, b'[{"key":"wake","text":"a"}]')
    first = panel["call"]("GET", "/api/loci/present/prompts")
    gw.reply("GET", f"/{PASSPHRASE}/present/prompts", 200, b'[{"key":"wake","text":"b"}]')
    second = panel["call"]("GET", "/api/loci/present/prompts")
    assert (first.raw, second.raw) == (b'[{"key":"wake","text":"a"}]',
                                       b'[{"key":"wake","text":"b"}]')
    assert len(gw.seen) == 2, "each read asks the gateway again"
    assert first.headers["cache-control"] == second.headers["cache-control"] == "no-store"


def test_which_host_when_several_have_a_present_url(panel, monkeypatch):
    panel["config"]["hosts"] = {
        "gateway": _gateway_host(),
        "second": _gateway_host("second", url="http://second.test:3200")}
    monkeypatch.setenv("T_SECOND_IN", "second-in")
    monkeypatch.setenv("T_SECOND_OUT", "second-out")
    out = panel["call"]("GET", "/api/loci/present")
    assert out.status == 400 and out.json["hosts"] == ["gateway", "second"]
    assert panel["gw"].seen == []
    out = panel["call"]("POST", "/api/loci/present/push-test", body={"host": "second"})
    assert out.status == 200
    seen = panel["gw"].seen[-1]
    assert (seen["host"], seen["path"], seen["auth"]) == (
        "second.test", "/present/push-test", "Bearer second-out")
    out = panel["call"]("GET", "/api/loci/present", query="host=nobody")
    assert out.status == 404 and out.json["connected"] is False


# ───────────────────────── the panel's alone ─────────────────────────

def test_the_proxy_sits_behind_the_panel_gate(panel, monkeypatch):
    from web import panel_auth as PA
    for method, sub in ALLOWED:
        path = "/api/loci/present" + sub
        assert not PA.is_public(path) and not PA.is_hook(path) and not PA.is_host_read(path)
    host_key = panel["call"]("GET", "/api/loci/present", key=IN_KEY)
    assert host_key.status == 403
    no_origin = panel["call"]("POST", "/api/loci/present/compress", body={}, origin=False)
    assert no_origin.status == 403
    # Not logged in (this rewires the gate for the rest of the test).
    out = _routes(monkeypatch, session=False)("GET", "/api/loci/present")
    assert out.status == 401
    assert panel["gw"].seen == []


# ───────────────────────── the sentinel ─────────────────────────

def test_the_gateways_private_text_never_shows_in_a_proxy_reply(panel):
    gw = panel["gw"]
    gw.reply("GET", f"/{PASSPHRASE}/present", 200, STATUS_RAW)
    replies = []
    for method, sub in ALLOWED:
        replies.append(panel["call"](method, "/api/loci/present" + sub,
                                     body={} if method == "POST" else None).raw)
    gw.fail = httpx.ConnectError(SENTINEL)
    replies.append(panel["call"]("GET", "/api/loci/present").raw)
    assert gw.private["threads"]["t_3f9c1a"]["carry"] == SENTINEL
    for raw in replies:
        assert SENTINEL.encode("utf-8") not in raw


# ───────────────────────── the export package ─────────────────────────

DAY = ('{"id":"m_20261007_0001","rev":1,"at":"2026-10-07T20:41:05+08:00",'
       '"thread":"t_3f9c1a","role":"user","text":"考完了！","state":"live"}\n')
REPORT = "# 2026-10-07\n\n考完了，晚上一起庆祝。\n"


def _host_files(root, name="gateway"):
    host = root / EP.HOSTS_DIR / name
    (host / "days").mkdir(parents=True)
    (host / "reports").mkdir(parents=True)
    # Bytes, so the comparison after the round trip is byte for byte on every platform.
    (host / "days" / "2026-10-07.jsonl").write_bytes(DAY.encode("utf-8"))
    (host / "reports" / "2026-10-07.md").write_bytes(REPORT.encode("utf-8"))
    (host / "reports" / "2026-10-06.err.json").write_text(
        json.dumps({"error": ERR_SENTINEL, "tries": 3}), encoding="utf-8")
    # A gateway's private state belongs outside the library; one put here by mistake
    # still does not travel.
    (host / "threads").mkdir()
    (host / "threads" / "t_3f9c1a.json").write_text(
        json.dumps({"carry": SENTINEL}), encoding="utf-8")
    (host / "present.json").write_text(json.dumps({"push": {"bark": SENTINEL}}),
                                       encoding="utf-8")
    return host


def _zip_members(path):
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n) for n in z.namelist()}


def test_the_export_carries_days_and_reports_and_no_err_json(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    store = _library(root, monkeypatch)
    run(store.create("考完试那天晚上去吃了火锅。", room="EVENT/SELF"))
    _host_files(root)
    private = tmp_path / "gateway-private" / "threads"
    private.mkdir(parents=True)
    (private / "t_3f9c1a.json").write_text(json.dumps({"carry": SENTINEL}), encoding="utf-8")
    zip_path = _export(store, root)
    try:
        members = _zip_members(zip_path)
    finally:
        os.unlink(zip_path)
    day_member = f"{EP.STATE_PREFIX}_hosts/gateway/days/2026-10-07.jsonl"
    report_member = f"{EP.STATE_PREFIX}_hosts/gateway/reports/2026-10-07.md"
    assert members[day_member] == DAY.encode("utf-8")
    assert members[report_member] == REPORT.encode("utf-8")
    hosts_members = sorted(n for n in members if "_hosts/" in n)
    assert hosts_members == [day_member, report_member], hosts_members
    assert not any(n.endswith(".err.json") for n in members)
    for name, data in members.items():
        assert SENTINEL.encode("utf-8") not in data, name
        assert ERR_SENTINEL.encode("utf-8") not in data, name
    manifest = json.loads(members["backup_manifest.json"])
    left = {r["path"]: r for r in manifest["package"]["not_included"]}
    assert left["_hosts/*.err.json"]["files"] == 1
    assert left["_hosts/*"]["files"] == 2
    assert day_member[len(EP.STATE_PREFIX):] in manifest["package"]["sections"]["state"]


def test_an_import_into_an_empty_library_brings_the_host_files_back(tmp_path, monkeypatch):
    src_root = tmp_path / "lib"
    store = _library(src_root, monkeypatch)
    run(store.create("考完试那天晚上去吃了火锅。", room="EVENT/SELF"))
    _host_files(src_root)
    zip_path = _export(store, src_root)
    try:
        dst_root = tmp_path / "empty"
        dst = _library(dst_root, monkeypatch)
        _parsed, st = _import(zip_path, dst, dst_root, monkeypatch)
        assert st["phase"] == "done", st
        host = dst_root / EP.HOSTS_DIR / "gateway"
        assert (host / "days" / "2026-10-07.jsonl").read_bytes() == DAY.encode("utf-8")
        assert (host / "reports" / "2026-10-07.md").read_bytes() == REPORT.encode("utf-8")
        assert not (host / "reports" / "2026-10-06.err.json").exists()
        assert not (host / "threads").exists() and not (host / "present.json").exists()

        # A library that has entries keeps its host's own days: nothing is mixed in.
        living_root = tmp_path / "living"
        living = _library(living_root, monkeypatch)
        run(living.create("这个库自己的一条。", room="EVENT/SELF"))
        _parsed, st = _import(zip_path, living, living_root, monkeypatch)
        assert st["phase"] == "done", st
        assert not (living_root / EP.HOSTS_DIR).exists()
        not_merged = {r["path"] for r in st["library_state"]["not_merged"]}
        assert "_hosts/gateway/days/2026-10-07.jsonl" in not_merged, st["library_state"]
    finally:
        os.unlink(zip_path)
