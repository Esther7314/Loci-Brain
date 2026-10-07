# -*- coding: utf-8 -*-
"""
tests/test_export_originals.py — 「导出原话」: the words as they were said, and nothing else.

Imported conversations come out one Markdown file each; sunk originals (one file per entry
in archive/原文/) are merged into one Markdown file per local day the entry was written.
The text of a withdrawn or deleted source never comes out — not a withdrawn imported line,
not the sunk original of a deleted entry, one standing on a withdrawn source or one derived
from it, even when a file of it is still on disk. The ZIP holds nothing but those two
kinds and a README: no model-written text, so no compressed text either.
"""

import asyncio
import io
import json
import zipfile
from pathlib import Path

import frontmatter
import pytest
from starlette.requests import Request

from core import _source_change as SC
from core import _sources as S
from core import _when as _w
from core import export_originals as EO
from core import import_memory as IM
from core import runtime as rt
from core.bucket_manager import BucketManager
from core.scope import LOCI_HOST
from utils import WAS_DERIVED_FROM

GONE_LINE = "撤回的那句是蓝色雨伞"
GONE_SUNK = "站在撤回来源上的那段长谈"
CHILD_SUNK = "从撤回的那段长出来的想法"
DELETED_SUNK = "删掉的那条说的是旧钥匙"
SUMMARY = "这是模型写的摘要"

CHAT = json.dumps([
    {"role": "user", "content": "周日去爬山吧", "timestamp": "2026-09-06T09:00:00"},
    {"role": "assistant", "content": GONE_LINE, "timestamp": "2026-09-06T09:01:00"},
    {"role": "user", "content": "好，我带水", "timestamp": "2026-09-06T09:02:00"},
], ensure_ascii=False)


def run(coro):
    return asyncio.run(coro)


def _set_created(store, bid: str, stamp: str) -> None:
    path = Path(store._find_bucket_file(bid))
    post = frontmatter.load(path)
    post["created"] = stamp
    path.write_text(frontmatter.dumps(post), encoding="utf-8")


async def _sunk(store, body: str, **kw) -> str:
    bid = await store.create(body, room="EVENT/SELF", summary=SUMMARY, **kw)
    assert await store.sink_bucket(bid)
    return bid


@pytest.fixture
def lib(tmp_path, monkeypatch):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(tmp_path / "aliases.yaml"))
    importer = IM.ImportEngine({"buckets_dir": str(tmp_path)}, store, None)
    batch = run(importer.take(CHAT, "chat.json"))["batch"]
    ids = {}

    async def go():
        ids["early"] = await _sunk(store, "九月一号深夜那一大段原话。")
        ids["early2"] = await _sunk(store, "同一天早上又说了一段原话。")
        ids["later"] = await _sunk(store, "十月的原话。")
        ids["gone"] = await _sunk(store, GONE_SUNK, sources=[{
            "system": "import", "instance": batch, "container": "c0001", "id": "l0002"}])
        ids["child"] = await _sunk(store, CHILD_SUNK, prov=[
            {"rel": WAS_DERIVED_FROM, "target": ids["gone"]}])
        ids["deleted"] = await _sunk(store, DELETED_SUNK)
        await store.delete(ids["deleted"])
        ids["awake"] = await store.create("没有沉下去的正文。", room="EVENT/SELF")
    run(go())
    # 23:30 UTC on 09-01 is 09-02 in the default zone; the second is the same local day.
    _set_created(store, ids["early"], "2026-09-01T23:30:00+00:00")
    _set_created(store, ids["early2"], "2026-09-02T09:00:00+08:00")
    _set_created(store, ids["later"], "2026-10-05T12:00:00+08:00")

    status, out = run(SC.handle(store, {
        "change_id": "w-l0002", "source": f"import:{batch}/c0001#l0002",
        "host_seq": 1, "change": "withdrawn"}, LOCI_HOST))
    assert status == 200 and out["state"] == S.WITHDRAWN, out
    # Whatever the clearing did, the files of what is left out are on disk again: the
    # export's own line is what is tested.
    for key, text in (("gone", GONE_SUNK), ("child", CHILD_SUNK), ("deleted", DELETED_SUNK)):
        orig = Path(store._sunk_orig_path(ids[key]))
        orig.parent.mkdir(parents=True, exist_ok=True)
        orig.write_text(text, encoding="utf-8")
    return {"store": store, "ids": ids, "batch": batch, "root": tmp_path}


def _members(path: str) -> dict[str, str]:
    with zipfile.ZipFile(path) as z:
        return {n: z.read(n).decode("utf-8") for n in z.namelist()}


def _day(stamp: str) -> str:
    return _w.parse_stamp(stamp).date().isoformat()


def test_one_file_per_conversation_and_one_per_day(lib):
    path, counts = run(EO.build(lib["store"]))
    members = _members(path)
    Path(path).unlink()
    convs = [n for n in members if n.startswith(EO.IMPORTS_FOLDER + "/")]
    assert convs == [f"{EO.IMPORTS_FOLDER}/{lib['batch']}/c0001.md"]
    chat = members[convs[0]]
    assert "周日去爬山吧" in chat and "好，我带水" in chat
    assert chat.index("周日去爬山吧") < chat.index("好，我带水")
    assert "（这里有 1 行的来源撤回或删除了，不导出）" in chat

    early_day = _day("2026-09-01T23:30:00+00:00")
    days = sorted(n for n in members if n.startswith(EO.SUNK_FOLDER + "/"))
    expected = {f"{EO.SUNK_FOLDER}/{early_day}.md", f"{EO.SUNK_FOLDER}/2026-10-05.md"}
    assert expected <= set(days)
    merged = members[f"{EO.SUNK_FOLDER}/{early_day}.md"]
    assert "九月一号深夜那一大段原话。" in merged and "同一天早上又说了一段原话。" in merged
    assert merged.index("九月一号深夜") < merged.index("同一天早上")
    assert "十月的原话。" in members[f"{EO.SUNK_FOLDER}/2026-10-05.md"]
    assert counts["conversations"] == 1 and counts["lines"] == 2
    assert counts["lines_withheld"] == 1 and counts["sunk"] == 3
    assert counts["sunk_left_out"] == 3


def test_nothing_of_a_withdrawn_or_deleted_source_comes_out(lib):
    path, _counts = run(EO.build(lib["store"]))
    raw = Path(path).read_bytes()
    members = _members(path)
    Path(path).unlink()
    for phrase in (GONE_LINE, GONE_SUNK, CHILD_SUNK, DELETED_SUNK):
        assert phrase not in "".join(members.values()), phrase
        assert phrase.encode("utf-8") not in raw
    for key in ("gone", "child", "deleted"):
        assert lib["ids"][key] not in "".join(members.values())


def test_only_originals_and_the_readme_are_in_it(lib):
    # The text a model wrote — a summary, a draft, a host's self-compression — is not an
    # original: no member carries anything but the two kinds.
    path, _counts = run(EO.build(lib["store"]))
    members = _members(path)
    Path(path).unlink()
    for name in members:
        assert name == EO.README or (
            name.endswith(".md") and name.split("/", 1)[0] in (EO.IMPORTS_FOLDER,
                                                                EO.SUNK_FOLDER)), name
    assert SUMMARY not in "".join(members.values())
    assert "没有沉下去的正文" not in "".join(members.values())
    assert "撤回或删除" in members[EO.README]


def test_a_batch_being_withdrawn_is_left_out(lib):
    imports = IM.ImportStore(str(lib["root"]))
    meta = imports.meta(lib["batch"])
    meta["status"] = IM.WITHDRAWING
    imports.save_meta(meta)
    path, counts = run(EO.build(lib["store"]))
    members = _members(path)
    Path(path).unlink()
    assert not any(n.startswith(EO.IMPORTS_FOLDER + "/") for n in members)
    assert counts["conversations"] == 0


def test_the_route_hands_the_zip_to_its_own_page_only(lib, monkeypatch):
    from server_app import OriginCSRFGuardMiddleware
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(_Mcp())
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(sh, "bucket_mgr", lib["store"])

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    req = Request({"type": "http", "method": "GET", "path": "/api/loci/export/originals",
                   "query_string": b"", "headers": [(b"host", b"127.0.0.1:8000")]}, receive)
    resp = run(routes[("GET", "/api/loci/export/originals")](req))
    assert resp.status_code == 200 and resp.media_type == "application/zip"
    assert resp.headers["x-loci-conversations"] == "1"
    assert resp.headers["x-loci-withheld"] == "4"
    data = Path(resp.path).read_bytes()
    Path(resp.path).unlink()
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        assert EO.README in z.namelist()

    reached = []

    async def app(scope, rcv, send):
        reached.append(1)
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"", "more_body": False})
    mw = OriginCSRFGuardMiddleware(app, mcp_path_matcher=lambda p: p == "/mcp")
    sent = []

    async def send(msg):
        sent.append(msg)
    scope = {"type": "http", "method": "GET", "path": "/api/loci/export/originals",
             "scheme": "http", "client": ("127.0.0.1", 1),
             "headers": [(b"host", b"127.0.0.1:18001"), (b"origin", b"https://evil.example")]}
    run(mw(scope, receive, send))
    assert sent[0]["status"] == 403 and not reached
