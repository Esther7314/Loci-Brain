# -*- coding: utf-8 -*-
"""
tests/test_host_reads.py — the panel's reads a host may make too, and the writes it may not
(panel contract 「面板接口」 §六, decision Q1).

WHAT IS AGREED
    The reads Lento's memory page makes — breath as last handed out, the awake pool, what
    hangs open, the names page and a name's card, recall and the rooms, the detail
    window's body / lineage / source, a window's turns, the usage counts — answer a host's
    credential, filtered by that host's read scope exactly as its own reads are, each
    reply's `scope` saying the host's scope line. A host never sees more on them than its
    scope permits: not an entry, not an id. Every write stays the panel's: a host's
    credential is refused on every one of them. The panel's cookie still does both, and
    an unlocked panel with no hosts table still reads as the panel.
"""

import json
from datetime import timedelta
from urllib.parse import urlencode

import pytest
from starlette.requests import Request

import web
from core import _when as W
from core import breath_snapshot as SNAP
from core import names as NAMES
from core import scope as SC
from core.scope import OPEN_LINE
from web import loci as L
from web import panel_auth as PA

from _panel_kit import Reply, make_store, run

TG = {"system": "telegram", "instance": "b", "container": "g"}
HOME = {"system": "lento", "instance": "home", "container": "p"}
SCOPE = json.dumps({"v": 1, "entry": TG, "venue": "group", "audience": ["user:U"],
                    "grant": [TG]})
TABLE = {"life": {"token_env": "T_LIFE", "scope_mode": "open"},
         "bot": {"token_env": "T_BOT", "max_grant": [{"system": "telegram"}]}}
LIFE = [("x-loci-hook-token", "life-key")]
BOT = [("x-loci-hook-token", "bot-key"), ("loci-scope", SCOPE)]
ALIASES = """\
小林:
  instance_of: 人
小秘:
  instance_of: 人
"""


@pytest.fixture
def lib(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    from web import _shared as sh
    sh.config["hosts"] = TABLE
    monkeypatch.setenv("T_LIFE", "life-key")
    monkeypatch.setenv("T_BOT", "bot-key")
    monkeypatch.delenv("LOCI_HOOK_TOKEN", raising=False)
    monkeypatch.setattr(PA, "_lock_logged", {"message": "", "at": 0.0})
    monkeypatch.setattr(PA, "gate_needed", lambda: True)
    session = {"on": False}
    monkeypatch.setattr(PA, "has_session", lambda r: session["on"])
    table = tmp_path / "aliases.yaml"
    table.write_text(ALIASES, encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(table))
    monkeypatch.setattr(NAMES, "_cache", None)

    async def seed():
        mine = await store.create("群里说考完去海边。", room="EVENT/WORLD",
                                  sources=[{**TG, "id": "m_1"}], subjects=["小林"],
                                  tags=["海边"])
        secret = await store.create("私聊里说的海边秘密。", room="EVENT/SELF",
                                    sources=[{**HOME, "id": "p_1"}], subjects=["小秘"],
                                    tags=["秘密标签"], direction_of_fit="telic", bound=["AI"])
        hold = await store.create("海边那事先别催。", room="EVENT/WORLD",
                                  sources=[{**TG, "id": "m_2"}], direction_of_fit="telic",
                                  bound=["AI"], exception_of=secret, hold="defer",
                                  when=(W.now() + timedelta(days=5)).strftime("%Y-%m-%d"))
        return mine, secret, hold
    mine, secret, hold = run(seed())

    for host in ("life", "bot"):
        SNAP.save(str(store.base_dir), {"recent": {"items": [{"id": mine}, {"id": secret}]}},
                  host=host, scope_line=OPEN_LINE, at=W.now())
        store.cues.open_window(host, "w1", [])
        store.cues.offer(host, "w1", "t1", [
            {"card": f"{mine}@aaaa", "id": mine, "kind": "memory", "why": "海边"},
            {"card": f"{secret}@bbbb", "id": secret, "kind": "memory", "why": "海边"}])
    store.usage.path.parent.mkdir(parents=True, exist_ok=True)
    with store.usage.path.open("a", encoding="utf-8") as f:
        for host in ("life", "bot"):
            f.write(json.dumps({"kind": "shown", "road": "breath.recent", "ids": [mine, secret],
                                "host": host, "key": f"{host}:t1#1",
                                "at": W.now().isoformat(timespec="seconds")}) + "\n")

    found = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                found[(methods[0], path)] = fn
                return fn
            return keep
    L.register(web._Gated(_Mcp()))

    def call(method, path, params=None, headers=(), body=None, **query):
        raw = [(b"host", b"testserver"), (b"origin", b"http://testserver"),
               (b"content-type", b"application/json")]
        raw += [(k.encode(), v.encode()) for k, v in headers]
        data = b"" if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")

        async def receive():
            return {"type": "http.request", "body": data, "more_body": False}
        req = Request({"type": "http", "method": method, "path": path, "headers": raw,
                       "query_string": urlencode(query).encode(),
                       "path_params": params or {}}, receive)
        return Reply(run(found[(method, path)](req)))

    return {"store": store, "mine": mine, "secret": secret, "hold": hold, "call": call,
            "routes": found, "session": session}


def bot_line() -> str:
    hs = SC.load_hosts({"hosts": TABLE}, {"T_LIFE": "life-key", "T_BOT": "bot-key"})
    return SC.RequestScope.resolve(hs.get("bot"), SCOPE).first_line()


def _reads(lib):
    """(path, path params, query) of every host read, aimed at the entries seeded."""
    secret, mine = lib["secret"], lib["mine"]
    return [
        ("/api/loci/awake", None, {}),
        ("/api/loci/hanging", None, {"part": "surface"}),
        ("/api/loci/hanging", None, {"part": "deep"}),
        ("/api/loci/names", None, {}),
        ("/api/loci/names/{name}", {"name": "小林"}, {}),
        ("/api/loci/recall", None, {"query": "海边"}),
        ("/api/loci/rooms", None, {}),
        ("/api/loci/bucket/{bucket_id}", {"bucket_id": mine}, {}),
        ("/api/loci/lineage/{bucket_id}", {"bucket_id": mine}, {}),
        ("/api/loci/source/{bucket_id}", {"bucket_id": mine}, {}),
        ("/api/loci/turns/{window}", {"window": "w1"}, {}),
        ("/api/loci/usage", None, {}),
    ]


# ───────────────────────── which routes are which ─────────────────────────

def test_the_memory_pages_reads_are_host_reads_and_get_only(lib):
    assert set(PA.HOST_READ_PATHS) == {p for p, _pp, _q in _reads(lib)} | {
        "/api/loci/breath/last"}
    for method, path in lib["routes"]:
        if PA.is_host_read(path):
            assert method == "GET", path
    for path in PA.HOST_READ_PATHS:
        assert ("GET", path) in lib["routes"], path
        assert not PA.is_hook(path) and not PA.is_public(path), path


def test_a_write_cannot_be_registered_on_a_host_read_path():
    gated = web._Gated(type("M", (), {"custom_route": lambda s, p, methods=None, **k: (
        lambda fn: fn)})())
    with pytest.raises(ValueError):
        gated.custom_route("/api/loci/hanging", methods=["POST"])


# ───────────────────────── a host reads by its scope ─────────────────────────

def test_a_scoped_host_reads_each_page_under_its_scope_and_sees_nothing_past_it(lib):
    call, secret = lib["call"], lib["secret"]
    line = bot_line()
    assert line.startswith("〔范围：受限")
    for path, params, query in _reads(lib):
        r = call("GET", path, params, BOT, **query)
        assert r.status == 200, (path, r.json)
        assert r.json["scope"] == line, path
        text = json.dumps(r.json, ensure_ascii=False)
        assert secret not in text and secret[:6] not in text, path
        assert "秘密" not in text and "小秘" not in text, path


def test_a_scoped_host_gets_its_in_scope_rows(lib):
    call, mine, hold = lib["call"], lib["mine"], lib["hold"]
    awake = call("GET", "/api/loci/awake", None, BOT).json
    assert mine in {i["id"] for i in awake["items"]}
    halves = [call("GET", "/api/loci/hanging", None, BOT, part=p).json for p in ("surface", "deep")]
    [row] = [i for h in halves for i in h["items"] if i["id"] == hold]
    assert row["kind"] == "hold" and row["on"] == "", "the entry it hangs on is out of scope"
    turns = call("GET", "/api/loci/turns/{window}", {"window": "w1"}, BOT).json
    assert [c["id"] for c in turns["items"][0]["cards"]] == [mine]
    usage = call("GET", "/api/loci/usage", None, BOT).json
    assert [i["id"] for i in usage["items"]] == [mine]
    rooms = call("GET", "/api/loci/rooms", None, BOT).json
    assert rooms["total"] == 2 and {t["tag"] for t in rooms["top_tags"]} == {"海边"}
    names = call("GET", "/api/loci/names", None, BOT).json
    assert [i["name"] for i in names["items"]] == ["小林"]


def test_out_of_scope_reads_as_missing(lib):
    call, secret = lib["call"], lib["secret"]
    for path in ("/api/loci/bucket/{bucket_id}", "/api/loci/lineage/{bucket_id}",
                 "/api/loci/source/{bucket_id}"):
        assert call("GET", path, {"bucket_id": secret}, BOT).status == 404, path
    r = call("GET", "/api/loci/names/{name}", {"name": "小秘"}, BOT)
    assert r.status == 404 and "小秘" in r.json["error"]
    assert call("GET", "/api/loci/names/{name}", {"name": "小秘"}, LIFE).status == 200


def test_a_host_without_its_scope_is_refused_and_reads_nothing(lib):
    call = lib["call"]
    for path, params, query in _reads(lib):
        r = call("GET", path, params, [BOT[0]], **query)
        assert r.status == 403, (path, r.status)
        assert lib["secret"] not in json.dumps(r.json, ensure_ascii=False), path


def test_breath_last_is_a_hosts_own_and_withheld_under_a_scope(lib):
    call, secret = lib["call"], lib["secret"]
    r = call("GET", "/api/loci/breath/last", None, BOT)
    assert r.status == 200 and r.json == {"scope": SC.unsupported_line("上一次递出去的 breath ")}
    r = call("GET", "/api/loci/breath/last", None, LIFE)
    assert r.status == 200 and r.json["host"] == "life" and r.json["hosts"] == ["life"]
    assert call("GET", "/api/loci/breath/last", None, LIFE, host="bot").status == 403
    assert secret in json.dumps(r.json), "an open host reads its whole library"


def test_a_host_reads_only_its_own_windows(lib):
    call = lib["call"]
    assert call("GET", "/api/loci/turns/{window}", {"window": "w1"}, LIFE,
                host="bot").status == 403
    r = call("GET", "/api/loci/turns/{window}", {"window": "w1"}, LIFE)
    assert r.status == 200 and r.json["host"] == "life" and r.json["scope"] == OPEN_LINE


# ───────────────────────── every write stays the panel's ─────────────────────────

def _writes(lib):
    return sorted(path for method, path in lib["routes"]
                  if method == "POST" and not PA.is_hook(path) and not PA.is_public(path))


def test_a_host_credential_is_refused_on_every_write(lib):
    call = lib["call"]
    writes = _writes(lib)
    assert {"/api/loci/trace", "/api/loci/names/action", "/api/loci/entry/fix",
            "/api/loci/embedding/backfill"} <= set(writes)
    lib["session"]["on"] = True          # even with a session in the same browser
    for path in writes:
        for headers in (LIFE, BOT):
            r = call("POST", path, None, headers, body={"id": lib["hold"], "action": "withdraw"})
            assert r.status == 403 and "宿主" in r.json["error"], (path, r.json)
    fresh = run(lib["store"].get(lib["hold"]))["metadata"]
    assert fresh.get("status") not in ("resolved", "abandoned"), "nothing was written"


def test_without_a_table_the_hosts_key_does_not_write_either(lib, monkeypatch):
    from web import _shared as sh
    sh.config.pop("hosts")
    monkeypatch.setattr(PA, "hook_token", lambda: "life-key")
    r = lib["call"]("POST", "/api/loci/trace", None, LIFE,
                    body={"id": lib["hold"], "action": "withdraw"})
    assert r.status == 401
    r = lib["call"]("GET", "/api/loci/awake", None, LIFE)
    assert r.status == 200 and r.json["scope"] == OPEN_LINE, "the legacy host reads"


# ───────────────────────── the panel's cookie does both ─────────────────────────

def test_the_panel_reads_everything_and_writes(lib):
    call, secret, hold = lib["call"], lib["secret"], lib["hold"]
    for path, params, query in _reads(lib):
        assert call("GET", path, params, **query).status == 401, path
    lib["session"]["on"] = True
    seen = ""
    for path, params, query in _reads(lib):
        if path == "/api/loci/turns/{window}":
            query = {"host": "bot"}
        r = call("GET", path, params, **query)
        assert r.status == 200 and r.json["scope"] == OPEN_LINE, (path, r.json)
        seen += json.dumps(r.json, ensure_ascii=False)
    assert secret in seen and "秘密标签" in seen
    r = call("GET", "/api/loci/breath/last", None, host="bot")
    assert r.status == 200 and r.json["host"] == "bot" and set(r.json["hosts"]) == {"life", "bot"}
    r = call("POST", "/api/loci/trace", None, body={"id": hold, "action": "withdraw"})
    assert r.status == 200 and r.json["ok"], r.json
    r = call("POST", "/api/loci/names/action", None,
             body={"action": "set_kind", "name": "小林", "kind": "朋友"})
    assert r.status == 200 and r.json["ok"], r.json


def test_an_unlocked_panel_without_a_table_reads_as_the_panel(lib, monkeypatch):
    from web import _shared as sh
    sh.config.pop("hosts")
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    r = lib["call"]("GET", "/api/loci/turns/{window}", {"window": "w1"}, host="bot")
    assert r.status == 200 and r.json["host"] == "bot", r.json
    r = lib["call"]("GET", "/api/loci/breath/last", None, host="bot")
    assert r.status == 200 and r.json["host"] == "bot"
