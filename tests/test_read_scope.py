# -*- coding: utf-8 -*-
"""
tests/test_read_scope.py — a request carries its host, its read scope and its write key.

WHAT IS AGREED (接头样例修订 2, sections 一 and 二)
    A host's credential sets its ceiling (`max_grant`) and its mode: only a host written as
    `open` reads without a Loci-Scope; every other host gets nothing without one, and a
    scope that reaches past its ceiling — or cannot be read — is refused whole. A memory
    under a scope: every source behind it (its own, and every root of what it is derived
    from) passes its `use`, the grant and its state. A memory without sources is read only
    without a scope. Every read's first line states the scope, in one of the agreed
    wordings; nothing ever says how many entries were held back.

WHAT IS UNDER TEST HERE
    core/scope.py (hosts, parsing, resolving, the view the gate asks), the gate's answer
    on the read road, the tools' read paths under a scope (search, read by id and what it
    links to, slices), the MCP middleware naming the host, the hook routes. The whole path
    through the MCP tools runs in the exam (exam/items/g-scope.yaml).
"""

import asyncio
import json

import pytest

from core import _sources as S
from core import scope as SC
from core import visibility as V
from core.bucket_manager import BucketManager
from tools import _runtime as rt
from tools.recall import core as R

GROUP = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
BOT = SC.Host("book-bot", max_grant=(S.Place("telegram", "bot-a"),), token="bot-key")
LIFE = SC.Host("legacy", scope_mode=SC.OPEN, token="life-key")


def run(coro):
    return asyncio.run(coro)


def scope_json(venue="group", audience=("user:U", "user:X"), grant=(GROUP,), **extra):
    body = {"v": 1, "entry": GROUP, "venue": venue, "audience": list(audience),
            "grant": list(grant)}
    body.update(extra)
    return json.dumps(body)


def rec(id_, **use):
    out = {**GROUP, "id": id_}
    if use:
        out["use"] = use
    return out


# ───────────────────────── hosts ─────────────────────────

def test_no_hosts_table_is_exactly_the_legacy_host():
    hs = SC.load_hosts({}, {}, legacy_token="k")
    assert hs.implicit and list(hs.hosts) == ["legacy"]
    assert hs.default.open and hs.by_token("k") is hs.default
    assert hs.by_token("other") is None and hs.by_token("") is None


def test_a_hosts_table_is_read_and_the_legacy_host_is_one_entry_of_it():
    hs = SC.load_hosts({"hosts": {
        "legacy": {"token_env": "T_LIFE", "scope_mode": "open"},
        "bot": {"token_env": "T_BOT", "max_grant": [{"system": "telegram", "instance": "bot-a"}],
                "may_restore": True},
    }}, {"T_LIFE": "life", "T_BOT": "bot"})
    assert not hs.implicit
    assert hs.by_token("life").name == "legacy" and hs.by_token("life").open
    bot = hs.by_token("bot")
    assert bot.scope_mode == SC.RESTRICTED and bot.may_restore
    assert bot.max_grant == (S.Place("telegram", "bot-a"),)


def test_a_table_without_legacy_has_no_default_caller():
    hs = SC.load_hosts({"hosts": {"bot": {"token_env": "T"}}}, {"T": "x"})
    assert hs.default is None


def test_a_restricted_host_without_a_ceiling_may_reach_nothing():
    hs = SC.load_hosts({"hosts": {"bot": {"token_env": "T"}}}, {"T": "x"})
    req = SC.RequestScope.resolve(hs.get("bot"), scope_json())
    assert req.refusal == SC.EXCEEDS


def test_a_malformed_host_is_left_out_so_its_credential_matches_nothing():
    hs = SC.load_hosts({"hosts": {"bot": {"token_env": "T", "scope_mode": "wide"}}}, {"T": "x"})
    assert hs.by_token("x") is None and hs.errors


# ───────────────────────── one request, resolved ─────────────────────────

def test_the_open_host_with_no_scope_reads_the_whole_library():
    req = SC.RequestScope.resolve(LIFE, None)
    assert req.whole_library and req.grant is None
    assert req.first_line() == "〔范围：全库（open）〕"


def test_a_restricted_host_with_no_scope_gets_nothing():
    req = SC.RequestScope.resolve(BOT, "")
    assert req.refused and req.grant == ()
    assert req.first_line() == "〔范围：受限 · 没收到范围，什么都不给。宿主要在请求上带 Loci-Scope〕"


def test_a_scope_past_the_ceiling_is_refused_whole_and_says_where():
    req = SC.RequestScope.resolve(BOT, scope_json(grant=(GROUP, {"system": "lento", "instance": "home",
                                                                 "container": "private:V"})))
    assert req.first_line() == ("〔范围：受限 · 拒绝：范围超出这个宿主凭据（lento/home/private:V），"
                                "这次什么都没读〕")


def test_a_scope_within_the_ceiling_states_itself():
    req = SC.RequestScope.resolve(BOT, scope_json(grant=(GROUP, {**GROUP, "id": "m_1"})))
    assert not req.refused
    assert req.first_line() == "〔范围：受限 · 入口 telegram/bot-a/group:G · 场合 group · 许读 2 处〕"


def test_the_open_host_with_a_scope_is_judged_by_it():
    req = SC.RequestScope.resolve(LIFE, scope_json())
    assert not req.whole_library and req.scope.venue == "group"


@pytest.mark.parametrize("raw, says", [
    ("{", "不是合法的 JSON"),
    ("[]", "JSON 对象"),
    (scope_json(mode="full"), "不认识的字段 mode"),
    (json.dumps({"v": 1, "entry": GROUP, "venue": "group", "audience": []}), "缺 grant"),
    (scope_json().replace('"v": 1', '"v": 2'), "v 要是 1"),
    (scope_json(grant=({"instance": "bot-a"},)), "缺 system"),
    (scope_json(grant=({"system": "telegram", "container": "group:G"},)), "没写 instance"),
    (scope_json(audience=("",)), "audience[0]"),
])
def test_a_scope_that_cannot_be_read_is_refused_saying_what_is_wrong(raw, says):
    req = SC.RequestScope.resolve(BOT, raw)
    assert req.refusal == SC.MALFORMED and says in req.first_line()
    assert req.first_line().startswith("〔范围：受限 · 拒绝：Loci-Scope 读不懂（")
    assert req.first_line().endswith("），这次什么都没读〕")


def test_no_host_is_refused():
    req = SC.RequestScope.resolve(None, scope_json())
    assert req.refusal == SC.NO_HOST and "认不出是哪个宿主" in req.first_line()


def test_the_fourth_line_names_the_road_it_cannot_filter():
    assert SC.unsupported_line("梦") == "〔范围：受限 · 梦这一版还不能按范围筛，这次没放进来〕"


def test_a_scope_in_utf8_survives_the_latin1_header():
    raw = scope_json(venue="群").encode("utf-8").decode("latin-1")
    assert SC.parse_scope(raw).venue == "群"


def test_loci_turn_is_turn_hash_ordinal():
    assert SC.parse_turn("t-0925-01#2") == ("t-0925-01", 2)
    for bad in ("t-0925-01", "t 1#2", "t#0", "#2", "t#x"):
        with pytest.raises(SC.ScopeError):
            SC.parse_turn(bad)


def test_the_write_key_belongs_to_the_host():
    assert S.write_key("t-1", 2, "bot") != S.write_key("t-1", 2, "legacy")
    assert S.write_key("t-1", 2, "bot") == "bot:t-1#2"


# ───────────────────────── places ─────────────────────────

def test_a_place_covers_what_is_under_it():
    sid = S.SourceId("telegram", "bot-a", "group:G", "m_1")
    run_ = S.SourceId("telegram", "bot-a", "group:G", "m_1", through="m_9")
    assert S.Place("telegram").covers(sid) and S.Place("telegram", "bot-a").covers(sid)
    assert S.Place("telegram", "bot-a", "group:G", "m_1").covers(run_)
    assert not S.Place("telegram", "bot-a", "group:G", "m_2").covers(sid)
    assert not S.Place("telegram", "bot-b").covers(sid)
    assert S.Place("telegram", "bot-a", "group:G").within(S.Place("telegram", "bot-a"))
    assert not S.Place("telegram").within(S.Place("telegram", "bot-a"))


def test_the_write_check_and_the_read_gate_match_the_same_way(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    records = S.normalize_sources([rec("m_1")])
    assert reg.check_writable(records, [GROUP]) == ("", [])
    refusal, _ = reg.check_writable(records, [{**GROUP, "container": "group:H"}])
    assert "不在这一轮" in refusal


def test_use_is_kept_as_an_object_and_read_as_a_rule():
    [r] = S.normalize_sources([rec("m_1", venues=["private", "group"], audience=["user:U"])])
    assert r["use"] == {"venues": ["group", "private"], "audience": ["user:U"]}
    assert S.parse_use(r["use"]) == {"venues": frozenset({"group", "private"}),
                                     "audience": frozenset({"user:U"})}
    assert S.parse_use("private") is None
    with pytest.raises(S.SourceRecordError):
        S.normalize_sources([{**GROUP, "id": "m_1", "use": {"places": ["x"]}}])


# ───────────────────────── the view the gate asks ─────────────────────────

def _view(entries, scope=None, registry=None, host=BOT):
    metas = {e["id"]: e for e in entries}
    header = scope_json() if scope is None else scope
    return SC.ScopeView(SC.RequestScope.resolve(host, header), metas, registry)


def _entry(bid, sources=(), from_=()):
    return {"id": bid, "sources": list(sources),
            "prov": [{"rel": "wasDerivedFrom", "target": f} for f in from_]}


def test_the_three_conditions(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    ok = _entry("a", [rec("m_1", venues=["group"])])
    private = _entry("b", [rec("m_2", venues=["private"])])
    outsider = _entry("c", [rec("m_3", audience=["user:U"])])           # X is in the audience
    elsewhere = _entry("d", [{**rec("m_4"), "container": "group:H"}])   # not granted
    gone = _entry("e", [rec("m_5")])
    run(reg.apply_change({"change_id": "w", "source": "telegram:bot-a/group:G#m_5",
                          "kind": "withdrawn", "host_seq": 1}))
    no_rule = _entry("f", [rec("m_6")])
    view = _view([ok, private, outsider, elsewhere, gone, no_rule], registry=reg)
    assert view.permits(ok) and view.permits(no_rule)
    assert not view.permits(private) and not view.permits(outsider)
    assert not view.permits(elsewhere) and not view.permits(gone)


def test_an_unreadable_source_still_lets_what_was_understood_be_read(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    run(reg.apply_change({"change_id": "u", "source": "telegram:bot-a/group:G#m_1",
                          "kind": "unreadable", "host_seq": 1}))
    assert _view([_entry("a", [rec("m_1")])], registry=reg).permits_id("a")


def test_a_use_changed_by_the_host_wins_over_the_record(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    e = _entry("a", [rec("m_1", venues=["group"])])
    assert _view([e], registry=reg).permits(e)
    run(reg.apply_change({"change_id": "c", "source": "telegram:bot-a/group:G#m_1",
                          "kind": "use_changed", "host_seq": 1, "use": {"venues": ["private"]}}))
    assert not _view([e], registry=reg).permits(e)


def test_a_use_the_gate_cannot_read_lets_nothing_through():
    e = _entry("a", [rec("m_1")])
    e["sources"][0]["use"] = "quote-only"
    assert not _view([e]).permits(e)


def test_a_derived_entry_needs_every_root():
    group_ok = _entry("r1", [rec("m_1", venues=["group", "private"])])
    private_only = _entry("r2", [rec("m_2", venues=["private"])])
    mixed = _entry("d", from_=["r1", "r2"])
    clean = _entry("d2", from_=["r1"])
    deeper = _entry("d3", from_=["d2"])
    entries = [group_ok, private_only, mixed, clean, deeper]
    in_group = _view(entries)
    assert not in_group.permits(mixed) and in_group.permits(clean) and in_group.permits(deeper)
    in_private = _view(entries, scope_json(venue="private", audience=("user:U",)))
    assert in_private.permits(mixed)


def test_a_memory_without_sources_is_read_only_without_a_scope():
    old = _entry("old")
    on_old = _entry("d", [rec("m_1")], from_=["old"])
    dangling = _entry("x", from_=["nowhere"])
    view = _view([old, on_old, dangling])
    assert not view.permits(old) and not view.permits(on_old) and not view.permits(dangling)
    cycle_a, cycle_b = _entry("ca", from_=["cb"]), _entry("cb", from_=["ca"])
    assert not _view([cycle_a, cycle_b]).permits(cycle_a)


def test_a_refused_request_permits_nothing():
    e = _entry("a", [rec("m_1")])
    assert not _view([e], scope="").permits(e)


def test_the_gate_says_out_of_scope_and_shows_nothing_on_the_read_road():
    e = _entry("a", [rec("m_1", venues=["private"])])
    verdict = V.visible_for(e, _view([e]), road=V.READ)
    assert not verdict and verdict.out_of_scope and verdict.mark == ""
    archived = {**_entry("b", [rec("m_2")]), "type": "archived"}
    seen = V.visible_for(archived, _view([archived]), road=V.READ)
    assert seen and seen.mark            # in scope, it is still shown with its state


def test_a_container_is_covered_only_by_a_place_without_a_piece():
    view = _view([], scope_json(grant=({**GROUP, "id": "m_1"},)))
    assert not view.covers_container(GROUP)
    assert _view([]).covers_container(GROUP)


# ───────────────────────── the tools' read paths ─────────────────────────

class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    return mgr


def _library(store):
    async def seed():
        ids = {}
        ids["group"] = await store.create("The reading club meets in the south hall.",
                                          room="EVENT/WORLD", sources=[rec("m_1", venues=["group"])])
        ids["private"] = await store.create("She has a counselling slot on Saturday.",
                                            room="EVENT/WORLD",
                                            sources=[rec("m_2", venues=["private"])])
        ids["mind"] = await store.create("Saturdays are fuller than they look.", room="MIND/VIEWS",
                                         prov=[{"rel": "wasDerivedFrom", "target": ids["group"]},
                                               {"rel": "wasDerivedFrom", "target": ids["private"]}],
                                         sources=[rec("m_1", venues=["group"])])
        return ids
    return run(seed())


def _scoped(coro, scope=None, host=BOT):
    async def go():
        with SC.request_scope(SC.RequestScope.resolve(host, scope or scope_json())):
            return await coro
    return run(go())


def test_reading_out_of_scope_by_id_is_reading_what_does_not_exist(store):
    ids = _library(store)
    hidden = _scoped(R.recall_core(when="", room="", tag="", query=ids["private"]))
    missing = _scoped(R.recall_core(when="", room="", tag="", query="0123456789ab"))
    assert hidden.replace(ids["private"], "ID") == missing.replace("0123456789ab", "ID")
    short = _scoped(R.recall_core(when="", room="", tag="", query=ids["private"][:6]))
    assert "查无此桶" in short and "counselling" not in short


def test_what_a_read_by_id_links_to_is_left_out_when_out_of_scope(store):
    ids = _library(store)
    # The mind stands on a private root: in the group it does not exist.
    assert "查无此桶" in _scoped(R.recall_core(when="", room="", tag="", query=ids["mind"]))
    # The group entry is cited by the mind; in the group that line is not there.
    out = _scoped(R.recall_core(when="", room="", tag="", query=ids["group"]))
    assert "south hall" in out and ids["mind"] not in out and "被引用" not in out
    # Unscoped, the same read names it.
    assert ids["mind"] in run(R.recall_core(when="", room="", tag="", query=ids["group"]))


def test_browsing_under_a_scope_counts_only_what_it_may_read(store):
    ids = _library(store)
    out = _scoped(R.recall_core(when="7d", room="", tag="", query=""))
    assert ids["group"][:6] in out and ids["private"][:6] not in out
    assert "1 条" in out


def test_writing_from_a_source_outside_the_grant_is_refused(store, monkeypatch):
    import tools.grow as grow_mod
    from tools.grow import dispatch as grow, rooms_path

    class _Engine:
        async def ensure_started(self):
            pass

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    out = _scoped(grow(kind="event", sources=[{**GROUP, "container": "group:H", "id": "m_9"}],
                       items=[{"room": "EVENT/WORLD", "text": "Moved to Sunday.", "v": 0.5, "a": 0.3}]))
    assert "不在这一轮" in out


def test_pending_slices_are_seen_per_container_granted(store):
    from core import _slicer
    from tools import _slices

    async def side_model(system, user):
        return json.dumps({"slices": [{"from": 1, "to": 2, "gist": "two lines"}]})

    async def seed():
        for container in ("group:G", "group:H"):
            await _slicer.take_batch(store, {
                "source": {**GROUP, "container": container}, "day": "2026-09-30",
                "lines": [{"id": f"{container}-1", "text": "hello"},
                          {"id": f"{container}-2", "text": "again"}]}, model=side_model)
    run(seed())
    assert run(_slices.pending_seen()) == 2
    seen = _scoped(_slices.visible_batches())
    assert [b["source"]["container"] for b in seen] == ["group:G"]
    assert _scoped(_slices.pending_seen()) == 1
    hidden = [s["slice_id"] for b in run(_slices.visible_batches()) for s in b["slices"]
              if b["source"]["container"] == "group:H"][0]
    out = _scoped(_slices.trace_slice(hidden, drop=True, span="", kwargs={}, trace_core=None))
    assert out.startswith(f"没有这片切片：{hidden}") and "本次什么都没改" in out
    assert run(_slices.pending_seen()) == 2


# ───────────────────────── the MCP middleware names the host ─────────────────────────

def _asgi(hosts, *, auth_required=True, headers=()):
    from server_app import MCPAuthMiddleware
    seen = {}

    async def app(scope, receive, send):
        seen["host"] = scope.get("loci.host", "<not set>")
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    mw = MCPAuthMiddleware(app, auth_required=auth_required, auth_mode="token",
                           token_validator=lambda t, resource: t == "mcp-key",
                           path_matcher=lambda p: p == "/mcp",
                           host_resolver=(lambda: hosts) if hosts is not None else None)
    sent = []

    async def send(msg):
        sent.append(msg)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    scope = {"type": "http", "method": "POST", "path": "/mcp", "scheme": "http",
             "headers": [(k.encode(), v.encode()) for k, v in headers], "client": ("127.0.0.1", 1)}
    run(mw(scope, receive, send))
    return sent[0]["status"], seen.get("host", "<not set>")


TABLE = SC.load_hosts({"hosts": {"legacy": {"token_env": "T_L", "scope_mode": "open"},
                                 "bot": {"token_env": "T_B", "max_grant": [{"system": "telegram"}]}}},
                      {"T_L": "life", "T_B": "bot-key"})


def test_a_host_credential_admits_and_names_the_host():
    assert _asgi(TABLE, headers=[("x-loci-hook-token", "bot-key")]) == (200, "bot")
    assert _asgi(TABLE, headers=[("authorization", "Bearer bot-key")]) == (200, "bot")


def test_the_old_mcp_credential_is_the_default_host():
    assert _asgi(TABLE, headers=[("authorization", "Bearer mcp-key")]) == (200, "legacy")
    assert _asgi(TABLE, headers=[])[0] == 401
    assert _asgi(TABLE, auth_required=False) == (200, "legacy")


def test_an_unknown_host_credential_is_refused_never_taken_for_open():
    assert _asgi(TABLE, auth_required=False, headers=[("x-loci-hook-token", "stale")])[0] == 401
    implicit = SC.load_hosts({}, {}, legacy_token="life")
    assert _asgi(implicit, auth_required=False,
                 headers=[("x-loci-hook-token", "stale")]) == (200, "legacy")


def test_without_a_resolver_nothing_is_named():
    assert _asgi(None, auth_required=False) == (200, "<not set>")


# ───────────────────────── the hook routes ─────────────────────────

def _hook(monkeypatch, store, path, headers=(), hosts=None, method="GET"):
    from starlette.requests import Request
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    routes = {}

    class _Mcp:
        def custom_route(self, p, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], p)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(PA, "has_session", lambda r: False)
    monkeypatch.setattr(PA, "hosts", lambda: hosts or TABLE)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}
    req = Request({"type": "http", "method": method, "path": path, "query_string": b"",
                   "headers": [(k.lower().encode(), v.encode("utf-8")) for k, v in headers]},
                  receive)
    return run(routes[(method, path)](req))


def test_a_scoped_host_gets_no_dream_and_is_told_with_the_fourth_line(store, monkeypatch):
    resp = _hook(monkeypatch, store, "/api/dream/current",
                 [("x-loci-hook-token", "bot-key"), ("Loci-Scope", scope_json())])
    assert resp.status_code == 200
    assert json.loads(resp.body) == {"scope": "〔范围：受限 · 梦这一版还不能按范围筛，这次没放进来〕"}


def test_a_host_without_a_scope_is_refused_on_the_hook_routes(store, monkeypatch):
    for path in ("/api/v2/breath", "/api/dream/current", "/api/muse/pending", "/api/v2/slices"):
        resp = _hook(monkeypatch, store, path, [("x-loci-hook-token", "bot-key")])
        assert resp.status_code == 403, path
        assert "没收到范围" in json.loads(resp.body)["error"]


def test_a_malformed_scope_on_a_hook_route_is_a_400(store, monkeypatch):
    resp = _hook(monkeypatch, store, "/api/v2/breath",
                 [("x-loci-hook-token", "bot-key"), ("Loci-Scope", "{")])
    assert resp.status_code == 400 and "读不懂" in json.loads(resp.body)["scope"]


def test_breath_over_the_hook_opens_with_the_scope(store, monkeypatch):
    ids = _library(store)
    resp = _hook(monkeypatch, store, "/api/v2/breath",
                 [("x-loci-hook-token", "bot-key"), ("Loci-Scope", scope_json())])
    text = resp.body.decode("utf-8")
    assert text.startswith("〔范围：受限 · 入口 telegram/bot-a/group:G · 场合 group · 许读 1 处〕\n")
    assert ids["private"][:6] not in text
    # The legacy host, unlocked panel, no key: the whole library, said on the first line.
    open_text = _hook(monkeypatch, store, "/api/v2/breath").body.decode("utf-8")
    assert open_text.startswith("〔范围：全库（open）〕\n")


def test_an_unknown_key_on_a_hook_route_is_refused_when_hosts_are_written(store, monkeypatch):
    resp = _hook(monkeypatch, store, "/api/v2/breath", [("x-loci-hook-token", "stale")])
    assert resp.status_code == 401
