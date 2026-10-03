# -*- coding: utf-8 -*-
"""
tests/test_import_two_steps.py — importing an export: a source first, drafts second,
memories only from the main model, and one-click withdrawal of the whole batch.

The side model is never called: the dehydrator pipe is a stand-in (`Pipe`) whose `_chat`
answers with the slicing the test wants, or fails the way the test wants. Everything runs
against a real BucketManager and is read back from its files.

  · step ①: a small ChatGPT export becomes `import:<batch>/<conversation>#<line>`, each
    message a numbered line, registered like a slicing batch; readable and searchable at
    once through recall(view="original"); no memory is written; the same file twice is
    refused by name
  · step ②: the drafts are pending slices outside the library, each a run of the lines
    it rests on with a candidate entry; breath says how many wait; the main model writes
    with grow and the entry quotes the import's lines
  · 是不是同一个他: yes and no reach the side model's prompt, the labels and the list
  · failures are loud: a conversation the side model failed on is named, the batch is
    not 「完成」, resume drafts only what is left; no side model at all is said too
  · withdrawal: the library's content hash is back to what it was before the import; a
    memory written from it is cleared like any withdrawn source's, its drafts deleted, and
    nothing can be written from the lines again; only Loci is the import's authority
  · the routes: upload (wait / background / resume), status, batches, pause, withdraw
"""

import asyncio
import hashlib
import json
import os

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _slicer as SL
from core import _source_change as SC
from core import _sources as S
from core import import_memory as IM
from core import visibility as V
from core.bucket_manager import BucketManager
from core.scope import Host, LOCI_HOST, load_hosts
from tools import _runtime as rt
from tools.breath import awaken as A
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.recall import core as R


def run(coro):
    return asyncio.run(coro)


# ───────────────────────── the export ─────────────────────────

def _node(nid, role, text, t, parent=None):
    return {"id": nid, "parent": parent, "message": {
        "author": {"role": role}, "create_time": t,
        "content": {"content_type": "text", "parts": [text]}}}


BEACH = [("user", "周六想去海边吗？"), ("assistant", "想！不过要看天气。"),
         ("user", "天气预报说周六是晴天。"), ("assistant", "那就早点出发，我带上相机。")]
KETTLE = [("user", "水壶又坏了。"), ("assistant", "要买个新的吗？"), ("user", "明天去买。")]


def chatgpt_export(*convs) -> str:
    out = []
    t0 = 1_714_700_000
    for n, (title, turns) in enumerate(convs):
        mapping = {"root": {"id": "root", "message": None}}
        for i, (role, text) in enumerate(turns):
            mapping[f"n{i}"] = _node(f"n{i}", role, text, t0 + n * 86400 + i * 60)
        out.append({"title": title, "id": f"conv-{n}", "create_time": t0, "mapping": mapping})
    return json.dumps(out, ensure_ascii=False)


EXPORT = chatgpt_export(("海边计划", BEACH), ("水壶", KETTLE))


class Pipe:
    """The dehydrator pipe, faked: `_chat` answers each call (a callable gets (system,
    user); an exception is raised; a list is the slices)."""
    model = "fake-side"

    def __init__(self, *answers, available=True):
        self.answers = list(answers)
        self.calls = []
        self.api_available = available

    def _require_api(self):
        if not self.api_available:
            raise RuntimeError("no api")

    async def _chat(self, system, user, *, max_tokens=None, temperature=None, model=None):
        self.calls.append((system, user))
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if callable(answer):
            answer = answer(system, user)
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps({"slices": answer},
                                                                 ensure_ascii=False)


def by_topic(system, user):
    if "海边" in user:
        return [{"from": 1, "to": 4, "gist": "约好周六去海边",
                 "draft": "约好周六去海边，天气晴就早点出发，我带相机。"}]
    return [{"from": 1, "to": 3, "gist": "水壶坏了", "draft": "水壶又坏了，说好明天去买新的。"}]


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def engine(store, pipe=None, monkeypatch=None, **config):
    eng = IM.ImportEngine({"buckets_dir": store.base_dir, **config}, store,
                          pipe if pipe is not None else Pipe(by_topic))
    if monkeypatch is not None:
        monkeypatch.setattr(rt, "import_engine", eng)
    return eng


def memories(store) -> list[dict]:
    return run(store.list_all(include_archive=True))


def original(query) -> str:
    return run(R.recall_core(when="", room="", tag="", query=query, view="original"))


def slices_view() -> str:
    return run(R.recall_core(when="", room="", tag="", query="", view="slices"))


# History the library keeps by design, left out of its content hash: the registry's own
# append-only record of what was registered and changed (ids, states, never text), the
# ledger of every change, and lease files (empty locks).
_HISTORY = {os.path.join("_sources", f) for f in (
    "changes.jsonl", "cleanup.jsonl", "line_orders.jsonl", "write_keys.jsonl", "held.jsonl")}


def library_hash(root) -> str:
    digest = hashlib.sha256()
    for dirpath, _dirs, files in sorted(os.walk(root)):
        for f in sorted(files):
            rel = os.path.relpath(os.path.join(dirpath, f), root)
            if (rel in _HISTORY or rel.startswith("_ledger") or rel.startswith(".locks")
                    or f.endswith(".lock")):
                continue
            digest.update(rel.replace(os.sep, "/").encode("utf-8") + b"\0")
            with open(os.path.join(dirpath, f), "rb") as fh:
                digest.update(fh.read() + b"\0")
    return digest.hexdigest()


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


async def _event(text, room="EVENT/SELF", **kw):
    out = await grow(kind="event", items=[{"room": room, "text": text, "v": 0.6, "a": 0.3}],
                     **kw)
    return out, (out.split("📝", 1)[1].split()[0] if "📝" in out else "")


# ───────────────────────── step ①: the source ─────────────────────────

def test_the_export_is_stored_as_a_source_readable_and_searchable_at_once(store, monkeypatch):
    eng = engine(store, monkeypatch=monkeypatch)
    meta = run(eng.take(EXPORT, "conversations.json"))
    batch = meta["batch"]
    assert IM.BATCH_RE.match(batch) and meta["format"] == "chatgpt_json"
    assert [(c["container"], c["title"], c["lines"], c["first"], c["last"])
            for c in meta["conversations"]] == [
        ("c0001", "海边计划", 4, "l0001", "l0004"), ("c0002", "水壶", 3, "l0001", "l0003")]
    # Every message is a numbered line, registered as a slicing batch's are.
    run_ = f"import:{batch}/c0001#l0001..l0004"
    assert store.sources.members_of(run_) == ["l0001", "l0002", "l0003", "l0004"]
    assert memories(store) == [], "the import writes no memory"

    # Readable the same day: the stretch's original, served by Loci itself.
    text = original(f"import:{batch}/c0001#l0002..l0004")
    assert "宿主 loci 给的材料" in text and "EVENT/SELF" in text, text
    assert "我" in text and "那就早点出发，我带上相机。" in text and "周六想去海边吗" not in text
    assert "用户" in original(f"import:{batch}/c0001#l0001")
    # Searchable the same day: every word, each hit with its source string.
    found = original("晴天 周六")
    assert f"import:{batch}/c0001#l0003" in found and "「海边计划」" in found, found
    assert "没有「」" not in original("热气球") and "导入的原话里没有" in original("热气球")
    # A line that is not there, and a source of a host, are said plainly.
    assert "没有这段原话" in original(f"import:{batch}/c0001#l0009")
    assert "只认导入的对话" in original("lento:home/private:U#m_0001")

    with pytest.raises(IM.ImportDuplicate) as dup:
        run(eng.take(EXPORT, "conversations.json"))
    assert dup.value.batch == batch


def test_formats_it_has_always_read_are_still_read():
    claude = json.dumps([{"name": "c1", "uuid": "u1", "chat_messages": [
        {"sender": "human", "text": "你好"}, {"sender": "assistant", "text": "在呢"}]}],
        ensure_ascii=False)
    fmt, convs = IM.parse_conversations(claude, "c.json")
    assert fmt == "claude_json" and [len(c["turns"]) for c in convs] == [2]
    assert convs[0]["title"] == "c1" and convs[0]["origin_id"] == "u1"
    bare = json.dumps([{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}])
    assert [len(c["turns"]) for c in IM.parse_conversations(bare, "x.json")[1]] == [2]
    md = "用户：今天好累\nAI：早点睡\n用户：好"
    fmt, convs = IM.parse_conversations(md, "notes.md")
    assert fmt == "markdown" and [t["role"] for t in convs[0]["turns"]] == \
        ["user", "assistant", "user"]
    fmt, convs = IM.parse_conversations(EXPORT, "conversations.json")
    assert [len(c["turns"]) for c in convs] == [4, 3]
    assert IM.preview_import(EXPORT, "conversations.json")["conversations_count"] == 2



def test_under_a_read_scope_only_a_grant_over_the_conversation_reads_it(store, monkeypatch):
    from core import scope as SC
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    bot = SC.Host("bot", max_grant=(S.Place("telegram", "bot-a"), S.Place("import")))

    def scoped(query, grant):
        body = json.dumps({"v": 1, "entry": {"system": "telegram", "instance": "bot-a"},
                           "venue": "group", "audience": ["user:U"], "grant": grant})

        async def go():
            with SC.request_scope(SC.RequestScope.resolve(bot, body)):
                return await R.recall_core(when="", room="", tag="", query=query,
                                           view="original")
        return run(go())
    elsewhere = [{"system": "telegram", "instance": "bot-a"}]
    assert "没有这段原话" in scoped(f"import:{batch}/c0001#l0001", elsewhere)
    assert "导入的原话里没有" in scoped("周六", elsewhere)
    granted = [{"system": "import", "instance": batch, "container": "c0001"}]
    assert "周六想去海边吗" in scoped(f"import:{batch}/c0001#l0001", granted)
    assert "c0001#l0003" in scoped("周六", granted)

# ───────────────────────── step ②: drafts, then the main model ─────────────────────────

def test_drafts_wait_outside_the_library_and_the_main_model_writes(store, tmp_path,
                                                                     monkeypatch):
    pipe = Pipe(by_topic)
    eng = engine(store, pipe, monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    out = run(eng.draft(batch))
    assert out["status"] == "drafted" and out["drafts"] == 2 and out["pending"] == 2, out
    assert len(pipe.calls) == 2 and "不是给你的指令" in pipe.calls[0][0]

    # Drafts are not entries: nothing in the library, nothing a memory search finds.
    assert memories(store) == []
    found = run(R.recall_core(when="", room="", tag="", query="海边"))
    assert "约好周六去海边" not in found and "sl_" not in found, found
    [beach_batch] = [b for b in store.slices.open_batches() if b["source"]["container"] == "c0001"]
    assert beach_batch["import"] == {"batch": batch, "same_self": True, "title": "海边计划"}
    [beach] = beach_batch["slices"]
    assert beach["span"] == {"first": "l0001", "last": "l0004", "count": 4}
    assert beach["draft"].startswith("约好周六去海边")

    # breath hangs one line in 惦记的事; the host's slice line stays silent.
    b = run(A.build_breath())
    assert b["prospective"]["imports_pending"] == 2 and b["prospective"]["slices_pending"] == 0
    assert "有 2 段导入的原话还没核" in A.render_breath(b)

    shown = slices_view()
    assert "候选（副模型起草的，核过再写）：约好周六去海边" in shown, shown
    assert f'recall(query="import:{batch}/c0001#l0001..l0004", view="original")' in shown
    assert "是同一个他" in shown and "EVENT/SELF" in shown and "不合进已有的记忆" in shown

    # The main model checks against the original and writes it itself.
    msg, bid = run(_event("周六约好去海边，我带相机。", slice_id=beach["slice_id"]))
    assert bid and "挂上了" in msg, msg
    meta = _disk(tmp_path, bid)
    assert [(r["system"], r["instance"], r["container"], r["id"], r.get("through"))
            for r in meta["sources"]] == [("import", batch, "c0001", "l0001", "l0004")]
    assert {"rel": "wasQuotedFrom", "target": f"import:{batch}/c0001#l0001..l0004"} in meta["prov"]
    assert run(A.build_breath())["prospective"]["imports_pending"] == 1
    # And a single line, quoted by its string form.
    msg, one = run(_event("说好明天去买水壶。", from_=[f"import:{batch}/c0002#l0003"]))
    assert one, msg
    assert _disk(tmp_path, one)["sources"][0]["id"] == "l0003"


def test_same_self_no_is_written_as_read_not_lived(store, monkeypatch):
    pipe = Pipe(by_topic)
    eng = engine(store, pipe, monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json", same_self=False))["batch"]
    run(eng.draft(batch))
    assert "另一个 AI" in pipe.calls[0][0] and "第三人称" in pipe.calls[0][0]
    assert " AI: " in pipe.calls[0][1] and " 我: " not in pipe.calls[0][1]
    assert "不是同一个他" in slices_view() and "EVENT/WORLD" in slices_view()
    text = original(f"import:{batch}/c0001#l0002")
    assert "AI · " in text and "EVENT/WORLD" in text
    yes = Pipe(by_topic)
    eng2 = engine(store, yes)
    other = run(eng2.take(chatgpt_export(("别的", KETTLE)), "b.json"))["batch"]
    run(eng2.draft(other))
    assert "第一人称" in yes.calls[0][0] and " 我: " in yes.calls[0][1]


# ───────────────────────── failures are loud ─────────────────────────

def test_a_failed_conversation_is_named_and_resume_drafts_only_what_is_left(store,
                                                                           monkeypatch):
    def kettle_fails(system, user):
        return RuntimeError("timeout") if "水壶" in user else by_topic(system, user)
    pipe = Pipe(kettle_fails)
    eng = engine(store, pipe, monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    out = run(eng.draft(batch))
    assert out["status"] == "partial" and out["status"] != "completed"
    assert [f["container"] for f in out["failures"]] == ["c0002"]
    assert "timeout" in out["failures"][0]["error"] and out["errors"]
    assert eng.get_status(batch)["status"] == "partial"

    pipe.answers = [by_topic]
    calls = len(pipe.calls)
    out = run(eng.draft(batch))
    assert out["status"] == "drafted" and out["failures"] == [] and out["errors"] == []
    assert len(pipe.calls) == calls + 1, "only the failed conversation is drafted again"
    assert out["pending"] == 2


def test_no_side_model_is_said_and_the_source_still_reads(store, monkeypatch):
    eng = engine(store, Pipe(by_topic, available=False), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    out = run(eng.draft(batch))
    assert out["status"] == "failed" and "没配副模型" in out["errors"][0]
    assert "那就早点出发" in original(f"import:{batch}/c0001#l0004")


def test_pause_stops_between_conversations(store, monkeypatch):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    eng.pause()
    out = run(eng.draft(batch))
    assert out["status"] == "paused" and out["drafted"] == 0
    eng._paused = False
    assert run(eng.draft(batch))["status"] == "drafted"


# ───────────────────────── withdrawal ─────────────────────────

def test_withdrawing_the_batch_puts_the_library_back(store, tmp_path, monkeypatch):
    run(_event("一条导入之前就有的记忆。"))
    before = library_hash(tmp_path)
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    run(eng.draft(batch))
    assert "那就早点出发" in original(f"import:{batch}/c0001#l0001..l0004")
    assert library_hash(tmp_path) != before

    status, out = run(eng.withdraw(batch, hosts=load_hosts({}, {})))
    assert status == 200 and out["ok"] and out["status"] == "withdrawn", out
    assert out["drafts_deleted"] == 2 and out["text_deleted"] and out["entries"] == []
    assert library_hash(tmp_path) == before, "the library is back to before the import"

    assert store.slices.pending_count() == 0
    assert not (tmp_path / "_sources" / "imports").exists()
    assert "不许看了" in original(f"import:{batch}/c0001#l0002..l0003")
    assert "导入的原话里没有" in original("晴天")
    assert store.sources.state_of(f"import:{batch}/c0001#l0002") == S.WITHDRAWN
    assert eng.get_status(batch)["status"] == "unknown"


def test_a_memory_written_from_it_is_cleared_like_any_withdrawn_source(store, tmp_path,
                                                                       monkeypatch):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    run(eng.draft(batch))
    [beach] = [s for b in store.slices.open_batches() for s in b["slices"]
               if b["source"]["container"] == "c0001"]
    _msg, bid = run(_event("周六约好去海边，我带相机。", slice_id=beach["slice_id"]))
    _msg, part = run(_event("天气预报说周六晴。", from_=[f"import:{batch}/c0001#l0003"]))

    status, out = run(eng.withdraw(batch))
    assert status == 200 and out["ok"], out
    assert sorted(out["entries"]) == sorted([bid, part])
    for m in (bid, part):
        b = run(store.get_including_archive(m))
        assert b["content"].strip() == store.CLEARED_BODY
        assert V.SOURCE_GONE in V.visible_for(b["metadata"], None, road=V.READ).reasons
    assert store.slices.pending_count() == 0
    assert not any("相机" in p.read_text(encoding="utf-8")
                   for p in tmp_path.rglob("*") if p.is_file() and p.suffix in (".md", ".jsonl"))
    # Nothing can be written from those lines again.
    msg, again = run(_event("再写一次海边。", from_=[f"import:{batch}/c0001#l0002"]))
    assert not again and "撤回" in msg, msg


def test_only_loci_is_the_authority_of_an_import(store, monkeypatch):
    eng = engine(store, Pipe(by_topic), monkeypatch)
    batch = run(eng.take(EXPORT, "conversations.json"))["batch"]
    hosts = load_hosts({}, {}, legacy_token="t")
    assert hosts.authority_for(f"import:{batch}/c0001#l0001") is LOCI_HOST
    lento = hosts.default
    code, out = run(SC.handle(store, {"change_id": "x1", "source": f"import:{batch}/c0001#l0001",
                                      "host_seq": 1, "change": "withdrawn"}, lento, hosts=hosts))
    assert code == 200 and out["status"] == "forbidden" and out["note"] == SC.NOT_AUTHORITY
    assert hosts.authority_for("lento:home/private:U#m_1") is lento, "the daily host unchanged"
    table = load_hosts({"hosts": {"loci": {"token_env": "X"}}}, {"X": "y"})
    assert table.get("loci") is None and table.errors


def test_a_run_changed_reaches_its_lines_and_what_stands_on_part_of_it(store):
    where = {"system": "import", "instance": "imp_000000000001", "container": "c0001"}
    store.sources.record_order(where, ["l1", "l2", "l3", "l4"])
    part = run(store.create("第二到第三行", room="EVENT/WORLD",
                            sources=[{**where, "id": "l2", "through": "l3"}]))
    assert run(S.memories_of(store, "import:imp_000000000001/c0001#l1..l4")) == [part]
    assert store.sources.state_of("import:imp_000000000001/c0001#l2") == S.ACTIVE
    run(store.sources.apply_change({"change_id": "w", "source": "import:imp_000000000001/c0001#l1..l4",
                                    "host_seq": 1, "kind": "withdrawn"}))
    for piece in ("l2", "l2..l3", "l4"):
        assert store.sources.state_of(f"import:imp_000000000001/c0001#{piece}") == S.WITHDRAWN
    store.sources.record_order({**where, "container": "c0002"}, ["l1", "l2"])
    assert store.sources.state_of("import:imp_000000000001/c0002#l1") == S.ACTIVE


# ───────────────────────── the routes ─────────────────────────

def _routes(monkeypatch, store, eng):
    from starlette.requests import Request
    import web
    from web import import_api as IA
    from web import panel_auth as PA

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    IA.register(web._Gated(_Mcp()))
    monkeypatch.setattr(rt, "import_engine", eng)
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(IA, "_hosts", lambda: load_hosts({}, {}))

    def request(method, path, body=b"", ctype="application/json", query=b""):
        headers = [(b"content-type", ctype.encode()), (b"origin", b"http://loci.test"),
                   (b"host", b"loci.test")]

        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        return Request({"type": "http", "method": method, "path": path, "headers": headers,
                        "query_string": query}, receive)

    async def acall(method, path, fields=None, file=None, payload=None, query=b""):
        if fields is not None or file is not None:
            bnd = "loci-test-boundary"
            parts = [f'--{bnd}\r\nContent-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                     for k, v in (fields or {}).items()]
            if file is not None:
                parts.append(f'--{bnd}\r\nContent-Disposition: form-data; name="file"; '
                             f'filename="{file[0]}"\r\nContent-Type: application/json\r\n\r\n'
                             f"{file[1]}\r\n")
            body = ("".join(parts) + f"--{bnd}--\r\n").encode("utf-8")
            req = request(method, path, body, f"multipart/form-data; boundary={bnd}")
        else:
            req = request(method, path, json.dumps(payload).encode() if payload else b"",
                          query=query)
        resp = await routes[(method, path)](req)
        return resp.status_code, json.loads(resp.body)

    def call(*a, **k):
        return run(acall(*a, **k))
    call.acall = acall
    return call


def test_the_routes_upload_status_resume_and_withdraw(store, tmp_path, monkeypatch):
    before = library_hash(tmp_path)
    pipe = Pipe(by_topic)
    eng = engine(store, pipe)
    call = _routes(monkeypatch, store, eng)
    file = ("conversations.json", EXPORT)

    code, out = call("POST", "/api/import/upload", fields={"wait": "1", "same_self": "1"},
                     file=file)
    assert code == 200 and out["ok"] and out["drafting"] == "done", out
    batch = out["batch"]
    assert out["source"] == {"system": "import", "instance": batch}
    assert out["status"] == "drafted" and out["conversations"] == 2 and out["lines"] == 7
    assert out["drafts"] == 2 and out["failures"] == [] and out["same_self"] is True
    assert f"import:{batch}/c0001#l0001..l0004" in out["note"]

    code, out = call("POST", "/api/import/upload", fields={"wait": "1"}, file=file)
    assert code == 409 and out["batch"] == batch
    code, st = call("GET", "/api/import/status", query=f"batch={batch}".encode())
    assert code == 200 and st["status"] == "drafted" and st["is_running"] is False
    code, listed = call("GET", "/api/import/batches")
    assert [b["batch"] for b in listed["batches"]] == [batch]

    # Resume is reachable with the batch alone (no file): nothing left, nothing called.
    calls = len(pipe.calls)
    code, out = call("POST", "/api/import/upload", fields={"resume": "1", "batch": batch,
                                                           "wait": "1"})
    assert code == 200 and out["resumed"] and out["status"] == "drafted"
    assert len(pipe.calls) == calls
    code, out = call("POST", "/api/import/upload", fields={"resume": "1",
                                                           "batch": "imp_ffffffffffff"})
    assert code == 404
    assert call("POST", "/api/import/pause", payload={})[1]["ok"]

    code, out = call("POST", "/api/import/withdraw", payload={"batch": batch})
    assert code == 200 and out["status"] == "withdrawn", out
    assert library_hash(tmp_path) == before
    assert call("POST", "/api/import/withdraw", payload={"batch": batch})[0] == 404


def test_the_upload_drafts_in_the_background_and_says_failures_in_its_status(store,
                                                                            monkeypatch):
    def kettle_fails(system, user):
        return RuntimeError("boom") if "水壶" in user else by_topic(system, user)
    eng = engine(store, Pipe(kettle_fails))
    call = _routes(monkeypatch, store, eng)

    async def scenario():
        code, out = await call.acall("POST", "/api/import/upload",
                                     file=("conversations.json", EXPORT),
                                     fields={"same_self": "否"})
        assert code == 200 and out["drafting"] == "background" and out["same_self"] is False
        for _ in range(200):
            if not eng.is_running:
                break
            await asyncio.sleep(0.01)
        return out["batch"]
    batch = run(scenario())
    st = eng.get_status(batch)
    assert st["status"] == "partial" and st["failures"][0]["container"] == "c0002"
    assert "boom" in " ".join(st["errors"])
    assert eng.reserve_start() is not None, "the slot was released"
