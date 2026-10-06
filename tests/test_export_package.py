# -*- coding: utf-8 -*-
"""
tests/test_export_package.py — export a library, import it into an empty one, compare.

The acceptance of plan part five: a small library that holds every kind of state — a
version chain, a cover, a hold on a standing promise, a cue, a name card and the names
table, sources (one withdrawn, one held, a run with its line order, a revised one), a sunk
entry with its original, an attachment, vectors, the usage log, the card ledger, a dream,
a pending slice, the ledger — is exported and brought back into an empty library, and the
two are compared field by field. What the package must not carry (the withdrawn and the
deleted, their text anywhere, secrets) is checked byte by byte in the ZIP.

A second library that already has entries and a registry of its own takes the same
package by merging: collisions go by the importer's decisions, a source it never heard
of comes in with its chain, one it knows keeps its own, and the state that names the
other library's entries is reported, not merged.
"""

import asyncio
import json
import sqlite3
import zipfile
from contextlib import closing
from datetime import date
from pathlib import Path

import frontmatter
import pytest

from core import _case_recall
from core import _dream
from core import _invalidation as I
from core import _ledger
from core import _slicer as SL
from core import _source_change as SC
from core import export_package as EP
from core import fields as F
from core import schema
from core import visibility as V
from core.bucket_manager import BucketManager
from core.import_memory import ImportEngine, ImportStore
from core.package_import import MigrateEngine
from core.scope import Host
from utils import WAS_DERIVED_FROM, WAS_REVISION_OF

GONE_PHRASE = "撤回的那句是蓝色雨伞"
DELETED_PHRASE = "删掉的那条说的是旧钥匙"
SECRET = "sk-test-not-a-real-key-123456"
HOST = Host("life", scope_mode="open", may_restore=True)

M_KEEP = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0001"}
M_GONE = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0002"}
M_HELD = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0003"}
M_REV = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0004"}
RUN = {"system": "lento", "instance": "home", "container": "chat:G", "id": "m_0010",
       "through": "m_0012"}

NAMES_YAML = """小周:
  aliases: [周周, 阿周]
  instance_of: 人
  member_of: [读书会]
读书会:
  instance_of: 群
"""


def run(coro):
    return asyncio.run(coro)


class _FakeEmbedding:
    """What the importer reads of an embedding engine: the model and the vector store."""

    def __init__(self, db_path: Path, model="fake-embed"):
        self.model = model
        self.db_path = str(db_path)
        self._backend = None
        self.enabled = False
        with closing(sqlite3.connect(db_path)) as c:
            c.execute("CREATE TABLE IF NOT EXISTS embeddings (bucket_id TEXT PRIMARY KEY, "
                      "embedding TEXT NOT NULL, updated_at TEXT NOT NULL, "
                      "content_hash TEXT NOT NULL DEFAULT '', meaning_embedding TEXT)")
            c.execute("CREATE TABLE IF NOT EXISTS embeddings_meta "
                      "(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            c.commit()


def _vector(db: Path, bid: str, vec):
    with closing(sqlite3.connect(db)) as c:
        c.execute("INSERT OR REPLACE INTO embeddings VALUES (?, ?, '2026-10-01', 'h', NULL)",
                  (bid, json.dumps(vec)))
        c.commit()


def _point_runtime(monkeypatch, root: Path, store):
    from tools import _runtime as rt
    monkeypatch.setattr(rt, "bucket_mgr", store)
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(root)})
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(root / "aliases.yaml"))


def _build_library(root: Path, monkeypatch) -> dict:
    schema.stamp_new_library(root)      # what server start does for a new library
    store = BucketManager({"buckets_dir": str(root)})
    _point_runtime(monkeypatch, root, store)
    (root / "aliases.yaml").write_text(NAMES_YAML, encoding="utf-8")
    (root / "config.yaml").write_text(f"dehydration:\n  api_key: {SECRET}\n",
                                      encoding="utf-8")
    ids = {}

    async def go():
        ids["keep"] = await store.create("周六和小周去海边，带了西瓜。", room="EVENT/SELF",
                                         sources=[M_KEEP], summary="海边", tags=["海边"])
        ids["gone"] = await store.create(f"{GONE_PHRASE}。", room="EVENT/WORLD",
                                         sources=[M_GONE])
        ids["gone_child"] = await store.create(
            "从那件事想到的。", room="MIND/VIEWS",
            prov=[{"rel": WAS_DERIVED_FROM, "target": ids["gone"]}])
        ids["held"] = await store.create("宿主说可能撤回的那段。", room="EVENT/WORLD",
                                         sources=[M_HELD])
        ids["revised"] = await store.create("后来改过的那句话。", room="EVENT/WORLD",
                                            sources=[{**M_REV, "revision": "r1"}])
        ids["run"] = await store.create("群里那一段讨论。", room="EVENT/WORLD",
                                        sources=[RUN])
        # A version chain.
        ids["v1"] = await store.create("小周喜欢冬天。", room="MIND/TRAITS")
        ids["v2"] = await store.create(
            "小周喜欢冬天，也喜欢下雪。", room="MIND/TRAITS", subjects=["小周"],
            prov=[{"rel": WAS_REVISION_OF, "target": ids["v1"]}])
        await store.update(ids["v2"], supersedes=ids["v1"])
        await store.update(ids["v1"], superseded_by=ids["v2"], dont_surface=True)
        # A name card.
        ids["card"] = await store.create("小周是读书会的组织者。", room="MIND/TRAITS",
                                         card_of="小周")
        # A cover.
        ids["a"] = await store.create("周一读了第一章。", room="EVENT/SELF")
        ids["b"] = await store.create("周三读了第二章。", room="EVENT/SELF")
        ids["gist"] = await store.create("这周读完了两章。", room="EVENT/SELF",
                                         when="2026-09-28")
        await store.update(ids["gist"], cover=[ids["a"], ids["b"]])
        for x in ("a", "b"):
            await store.update(ids[x], covered_by=ids["gist"])
        # A standing promise with a cue, and a hold hung on it.
        ids["promise"] = await store.create(
            "答应小周下次烤蛋糕。", room="EVENT/SELF", direction_of_fit="telic",
            bound=["我"], cue={"condition": "下次烤东西的时候", "phrasings": ["烤蛋糕"]},
            weight=0.7)
        await store.update(ids["promise"], status="active")
        ids["hold"] = await store.create(
            "这周先别催烤蛋糕。", room="EVENT/SELF", direction_of_fit="telic",
            exception_of=ids["promise"], hold="defer", review_after="2026-10-10")
        # Sunk, with its original; an attachment.
        ids["sunk"] = await store.create("很久以前的一次长谈，说了很多话。",
                                         room="EVENT/SELF", summary="一次长谈")
        await store.sink_bucket(ids["sunk"])
        ids["deleted"] = await store.create(f"{DELETED_PHRASE}。", room="EVENT/SELF")
        await store.delete(ids["deleted"])
    run(go())

    media = root / "_media" / ids["keep"]
    media.mkdir(parents=True)
    (media / "photo.jpg").write_bytes(b"\xff\xd8 a photo")
    run(store.update(ids["keep"], media=None))
    meta_path = Path(store._find_bucket_file(ids["keep"]))
    post = frontmatter.load(meta_path)
    post["media"] = [{"path": f"_media/{ids['keep']}/photo.jpg", "type": "image/jpeg",
                      "title": "海边"}]
    meta_path.write_text(frontmatter.dumps(post), encoding="utf-8")

    # Sources: a run's line order; a revision; a withdrawal; a hold.
    status, out = run(SC.handle_lines(store, {
        "source": RUN, "revision": "w-1",
        "lines": ["m_0010", {"id": "m_0011", "revision": "e1"}, "m_0012"]}, HOST))
    assert status == 200 and out["status"] == "recorded", out
    for cid, kind, seq, source, extra in (
            ("c-1", "revised", 1, "lento:home/private:U#m_0004", {"revision": "r2"}),
            ("c-2", "withdrawn", 2, "lento:home/private:U#m_0002", {})):
        status, out = run(SC.handle(store, {"change_id": cid, "source": source,
                                            "host_seq": seq, "change": kind, **extra},
                                    HOST))
        assert status == 200 and out["status"] == "applied", out
    held = run(SC.hold(store, "lento:home/private:U#m_0003", "withdrawn", "life"))
    assert held == [ids["held"]]

    # Vectors, including the withdrawn and the deleted entry's.
    emb = _FakeEmbedding(root / "embeddings.db")
    for i, key in enumerate(("keep", "gone", "deleted", "v2", "promise")):
        _vector(root / "embeddings.db", ids[key], [0.1 * (i + 1), 0.2, 0.3])

    # The usage log, the card ledger, a dream, the dream clock, a scene word asked,
    # a pending slice, and a cursor key.
    store.usage.record("found", [ids["keep"], ids["deleted"]], "recall.search",
                       query=DELETED_PHRASE, gates={"when": ""})
    store.usage.record("shown", [ids["promise"]], "breath.prospective")
    store.cues.open_window("life", "w1", [])
    store.cues.offer("life", "w1", "t1", [{"card": f"{ids['promise']}@abc",
                                            "id": ids["promise"], "kind": "due",
                                            "why": "到点了"}])
    _dream.save_record({"id": "d00000000001", "完整": "梦见海边", "碎片": "海边",
                        "素材": {"压在心头": [ids["promise"]], "想不明白": []}}, str(root))
    _dream.save_record({"id": "d00000000002", "完整": f"梦见{DELETED_PHRASE}",
                        "碎片": "钥匙", "素材": {"压在心头": [ids["deleted"]]}}, str(root))
    (root / "_state").mkdir(exist_ok=True)
    (root / "_state" / "dream_state.json").write_text('{"last_woven": "2026-10-01"}',
                                                      encoding="utf-8")
    _case_recall.record_asked(str(root), "海边", date(2026, 10, 1))

    async def side_model(system, user):
        return json.dumps({"slices": [{"from": 1, "to": 2, "gist": "聊到读书"}]})
    run(SL.take_batch(store, {"source": {k: M_KEEP[k] for k in ("system", "instance",
                                                                  "container")},
                              "day": "2026-10-02",
                              "lines": [{"id": "m_0020", "text": "a"},
                                        {"id": "m_0021", "text": "b"}]},
                      model=side_model))
    _ledger.cursor_of(store.ledger_mirror, 1, "bot")
    assert (root / "_ledger" / _ledger.CURSOR_KEY_FILE).is_file()

    # An imported conversation: stored as a source Loci hosts, one draft waiting, and a
    # memory the main model wrote quoting it.
    importer = ImportEngine({"buckets_dir": str(root)}, store, _SidePipe())
    batch = run(importer.take(IMPORTED, "chat.json"))["batch"]
    drafted = run(importer.draft(batch))
    assert drafted["status"] == "drafted", drafted
    run_rec = {"system": "import", "instance": batch, "container": "c0001",
               "id": "l0001", "through": "l0002"}
    ids["quoted"] = run(store.create(
        "周日约好去爬山，我带水。", room="EVENT/SELF", sources=[run_rec],
        prov=[{"rel": "wasQuotedFrom", "target": f"import:{batch}/c0001#l0001..l0002"}]))
    return {"store": store, "ids": ids, "emb": emb, "batch": batch}


IMPORTED = json.dumps([{"role": "user", "content": "周日去爬山吧"},
                       {"role": "assistant", "content": "好，我带水"}], ensure_ascii=False)


class _SidePipe:
    """The side model drafting an import, faked: one stretch, one draft."""
    model = "fake-side"
    api_available = True

    def _require_api(self):
        pass

    async def _chat(self, system, user, *, max_tokens=None, temperature=None, model=None):
        return json.dumps({"slices": [{"from": 1, "to": 2, "gist": "约好去爬山",
                                       "draft": "约好周日去爬山，我带水。"}]},
                          ensure_ascii=False)


def _export(lib: dict, root: Path) -> tuple[str, dict]:
    return run(EP.build_package(
        lib["store"], embedding_db_path=str(root / "embeddings.db"),
        export_meta={"exported_at": "2026-10-03T00:00:00Z", "version": "test",
                     "embedding": {"model": "fake-embed", "dim": 3, "backend": "api"}},
        alias_path=str(root / "aliases.yaml")))


def _import(zip_path: str, root: Path, monkeypatch, decisions=None, store=None) -> tuple:
    schema.stamp_new_library(root)
    store = store or BucketManager({"buckets_dir": str(root)})
    _point_runtime(monkeypatch, root, store)
    emb = _FakeEmbedding(root / "embeddings.db")
    engine = MigrateEngine({"buckets_dir": str(root)}, store, emb)

    async def go():
        parsed = await engine.parse_zip_file(zip_path)
        assert parsed["ok"], parsed
        await engine.apply(decisions or {})
        return parsed, engine.get_status()
    parsed, status = run(go())
    return store, parsed, status


@pytest.fixture
def exported(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    lib = _build_library(src, monkeypatch)
    zip_path, manifest = _export(lib, src)
    return {"lib": lib, "src": src, "zip": zip_path, "manifest": manifest,
            "tmp": tmp_path}


def test_withdrawn_and_deleted_are_left_out_and_say_so(exported):
    ids = exported["lib"]["ids"]
    package = exported["manifest"]["package"]
    assert package["filtered"]["deleted"] == [ids["deleted"]]
    assert sorted(package["filtered"]["withdrawn"]) == sorted([ids["gone"],
                                                               ids["gone_child"]])
    with zipfile.ZipFile(exported["zip"]) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    for phrase in (GONE_PHRASE, DELETED_PHRASE, SECRET):
        assert phrase.encode("utf-8") not in blob, phrase
    assert not [n for n in names if ids["gone"] in n or ids["deleted"] in n]
    assert not [n for n in names if n.endswith(("config.yaml", _ledger.CURSOR_KEY_FILE,
                                                "write_keys.jsonl"))]
    # The held entry travels, and so does the hold on its source.
    assert any(ids["held"] in n for n in names)
    with zipfile.ZipFile(exported["zip"]) as z:
        con_path = exported["tmp"] / "snap.db"
        con_path.write_bytes(z.read("embeddings.db"))
    with closing(sqlite3.connect(con_path)) as c:
        have = {r[0] for r in c.execute("SELECT bucket_id FROM embeddings")}
    assert have == {ids["keep"], ids["v2"], ids["promise"]}


def test_the_manifest_lists_what_could_not_travel_and_what_was_left_behind(exported):
    ids = exported["lib"]["ids"]
    package = exported["manifest"]["package"]
    missing = package["missing"]
    hosts = {(h["system"], h["instance"]): h["sources"] for h in missing["host_material"]}
    assert hosts == {("lento", "home"): 4}
    assert missing["vectors"]["entries"] == package["entries"] - 3
    left = {r["path"]: r["reason"] for r in package["not_included"]}
    assert "config.yaml*" in left and "credentials" in left["config.yaml*"]
    assert f"_ledger/{_ledger.CURSOR_KEY_FILE}" in left
    assert package["sections"]["originals"] == 1 and package["sections"]["media"] == 1
    assert ids["sunk"] not in missing["sunk_originals"]
    state = set(package["sections"]["state"])
    for rel in ("_sources/changes.jsonl", "_sources/held.jsonl",
                "_sources/line_orders.jsonl", "_sources/pending_slices.jsonl",
                "_ledger/events.jsonl", "_cue/ledger.jsonl", "_usage/usage.jsonl",
                "_state/dream_state.json", "_state/case_recall_asked.json",
                "aliases.yaml"):
        assert rel in state, rel
    batch = exported["lib"]["batch"]
    assert {f"_sources/imports/{batch}/batch.json",
            f"_sources/imports/{batch}/c0001.jsonl"} <= state
    assert missing["import_batches"] == []
    # Every member is in the manifest with its hash, and the hashes hold.
    with zipfile.ZipFile(exported["zip"]) as z:
        listed = {f["path"]: f["sha256"] for f in exported["manifest"]["files"]}
        assert set(listed) == set(z.namelist()) - {"backup_manifest.json"}
        import hashlib
        for name, digest in listed.items():
            assert hashlib.sha256(z.read(name)).hexdigest() == digest, name


def test_the_schema_note_is_generated_from_the_field_table(exported):
    with zipfile.ZipFile(exported["zip"]) as z:
        doc = json.loads(z.read("schema.json"))
        note = z.read("SCHEMA.md").decode("utf-8")
        meta = json.loads(z.read("export_meta.json"))
    assert doc["library_schema_version"] == schema.CURRENT_VERSION == meta[
        "library_schema_version"]
    assert [f["name"] for f in doc["fields"]] == [f.name for f in F.FIELDS]
    counts = {f["name"]: f["entries"] for f in doc["fields"]}
    assert counts["cue"] == 1 and counts["exception_of"] == 1 and counts["card_of"] == 1
    assert doc["undescribed"] == []
    for f in F.FIELDS:
        assert f"`{f.name}`" in note


def test_round_trip_into_an_empty_library_is_identical_field_by_field(exported, monkeypatch):
    ids = exported["lib"]["ids"]
    src = exported["src"]
    left_out = set(exported["manifest"]["package"]["filtered"]["deleted"]) | set(
        exported["manifest"]["package"]["filtered"]["withdrawn"])
    before = EP.library_snapshot(src, alias_path=str(src / "aliases.yaml"),
                                 left_out=left_out)

    dst = exported["tmp"] / "dst"
    dst.mkdir()
    store, parsed, status = _import(exported["zip"], dst, monkeypatch)
    assert parsed["package"]["format"] == EP.PACKAGE_FORMAT
    assert status["phase"] == "done", status
    assert status["apply_errors"] == []
    assert status["library_state"]["mode"] == "fresh"
    assert status["library_state"]["errors"] == []
    after = EP.library_snapshot(dst, alias_path=str(dst / "aliases.yaml"))

    for section in ("entries", "originals", "media", "vectors", "state", "registry",
                    "names"):
        assert after[section] == before[section], section
    assert set(after["entries"]) == {v for k, v in ids.items()
                                     if k not in ("gone", "gone_child", "deleted")}
    # Byte for byte, at the same path.
    for bid in after["entries"]:
        a = Path(exported["lib"]["store"]._find_bucket_file(bid))
        b = Path(store._find_bucket_file(bid))
        assert a.relative_to(src) == b.relative_to(dst)
        assert a.read_bytes() == b.read_bytes()

    # The restored library behaves as the old one did.
    assert store.sources.state_of("lento:home/private:U#m_0003") == "held"
    assert store.sources.state_of("lento:home/private:U#m_0002") == "withdrawn"
    assert store.sources.members_of("lento:home/chat:G#m_0010..m_0012") == [
        "m_0010", "m_0011", "m_0012"]
    held_meta = run(store.get(ids["held"]))["metadata"]
    assert V.source_gone(held_meta) and I.gone_records(held_meta)[0]["kind"] == I.SOURCE_HELD
    from tools import _subjects as S
    assert S.record_of("阿周").name == "小周" and S.kind_of("读书会") == "群"
    assert run(store.get(ids["sunk"]))["metadata"]["decay_stage"] == "sunk"
    assert Path(store._sunk_orig_path(ids["sunk"])).is_file()
    assert not (dst / "config.yaml").exists()
    assert not (dst / "_ledger" / _ledger.CURSOR_KEY_FILE).exists()
    # The imported conversation is readable again, its draft waits, its memory stands on it.
    batch = exported["lib"]["batch"]
    assert [r["text"] for r in ImportStore(dst).lines(batch, "c0001")] == [
        "周日去爬山吧", "好，我带水"]
    drafts = [b for b in store.slices.open_batches() if b.get("import")]
    assert drafts and drafts[0]["import"]["batch"] == batch
    assert store.sources.members_of(f"import:{batch}/c0001#l0001..l0002") == [
        "l0001", "l0002"]
    assert not V.source_gone(run(store.get(ids["quoted"]))["metadata"])


def test_an_old_library_exports_under_its_own_version(tmp_path, monkeypatch):
    old = tmp_path / "old"
    old.mkdir()
    store = BucketManager({"buckets_dir": str(old)})
    _point_runtime(monkeypatch, old, store)
    run(store.create("一条老库里的记忆。", room="EVENT/SELF"))
    assert schema.library_version(old) == 1     # memories and no version file: 1.4.0
    zip_path, manifest = run(EP.build_package(
        store, embedding_db_path=str(old / "embeddings.db"), export_meta={},
        alias_path=str(old / "aliases.yaml")))
    assert manifest["package"]["library_schema_version"] == 1
    with zipfile.ZipFile(zip_path) as z:
        assert "the code that exported it reads version" in z.read("SCHEMA.md").decode()
    Path(zip_path).unlink()


def test_a_package_of_another_library_version_is_refused(exported, monkeypatch):
    bad = exported["tmp"] / "old.zip"
    with zipfile.ZipFile(exported["zip"]) as z, zipfile.ZipFile(bad, "w") as out:
        for name in z.namelist():
            data = z.read(name)
            if name == "export_meta.json":
                meta = json.loads(data)
                meta["library_schema_version"] = schema.CURRENT_VERSION - 1
                data = json.dumps(meta).encode()
            if name != "backup_manifest.json":
                out.writestr(name, data)
    dst = exported["tmp"] / "dst2"
    dst.mkdir()
    store = BucketManager({"buckets_dir": str(dst)})
    engine = MigrateEngine({"buckets_dir": str(dst)}, store, _FakeEmbedding(
        dst / "embeddings.db"))
    out = run(engine.parse_zip_file(str(bad)))
    assert not out["ok"] and "第" in out["error"] and "版" in out["error"]


def test_merging_into_a_library_that_has_its_own(exported, monkeypatch):
    ids = exported["lib"]["ids"]
    dst = exported["tmp"] / "busy"
    dst.mkdir()
    store = BucketManager({"buckets_dir": str(dst)})
    _point_runtime(monkeypatch, dst, store)
    (dst / "aliases.yaml").write_text("阿明:\n  aliases: [明明]\n", encoding="utf-8")

    async def own():
        mine = await store.create("这个库自己的一条。", room="EVENT/SELF",
                                  sources=[M_HELD])
        # The same id as one in the package: a collision.
        clash = await store.create("撞了号的一条。", room="EVENT/SELF",
                                   bucket_id_override=ids["keep"])
        return mine, clash
    mine, clash = run(own())
    status, out = run(SC.handle(store, {"change_id": "x-1",
                                        "source": "lento:home/private:U#m_0003",
                                        "host_seq": 9, "change": "restored"}, HOST))
    assert status == 200, out

    store, parsed, st = _import(exported["zip"], dst, monkeypatch,
                                decisions={ids["keep"]: "keep_both"}, store=store)
    assert [c["bucket_id"] for c in parsed["conflicts"]] == [ids["keep"]]
    assert st["phase"] == "done" and st["library_state"]["mode"] == "merge", st
    report = st["library_state"]
    # The library's own word on a source it knows stands; the package's other sources come in.
    merged = report["merged"]["_sources"]
    assert "lento:home/private:U#m_0003" in merged["kept_own"]
    assert store.sources.state_of("lento:home/private:U#m_0003") == "active"
    assert store.sources.state_of("lento:home/private:U#m_0002") == "withdrawn"
    assert store.sources.members_of("lento:home/chat:G#m_0010..m_0012") == [
        "m_0010", "m_0011", "m_0012"]
    # Names: the package's come in beside the library's.
    from tools import _subjects as S
    assert S.record_of("明明").name == "阿明" and S.record_of("周周").name == "小周"
    # An imported conversation batch the library does not have joins it.
    assert ImportStore(dst).meta(exported["lib"]["batch"]) is not None
    # What names the other library's entries and windows is reported, not merged.
    not_merged = {r["path"] for r in report["not_merged"]}
    assert {"_usage/usage.jsonl", "_cue/ledger.jsonl", "_ledger/events.jsonl"} <= not_merged
    # keep_both: the package's entry is there under a new id, the library's untouched.
    assert run(store.get(clash))["content"].strip() == "撞了号的一条。"
    assert st["result"]["imported"] == parsed["total_buckets"]
    texts = [b["content"].strip() for b in run(store.list_all(include_archive=True))]
    assert "周六和小周去海边，带了西瓜。" in texts


def test_the_panel_routes_export_and_bring_a_package_back(exported, monkeypatch):
    from starlette.requests import Request
    from web import _shared as sh
    from web import library_api as L
    from web import loci as Wb
    from web import panel_auth as PA

    ids = exported["lib"]["ids"]
    src = exported["src"]
    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(_Mcp())
    monkeypatch.setattr(PA, "gate_needed", lambda: False)
    monkeypatch.setattr(sh, "config", {"buckets_dir": str(src)})
    monkeypatch.setattr(sh, "bucket_mgr", exported["lib"]["store"])
    monkeypatch.setattr(sh, "embedding_engine", exported["lib"]["emb"])
    monkeypatch.setattr(sh, "version", "test")

    def request(method, path, body=b"", content_type="application/json"):
        async def receive():
            return {"type": "http.request", "body": body, "more_body": False}
        return Request({"type": "http", "method": method, "path": path, "query_string": b"",
                        "headers": [(b"host", b"127.0.0.1:8000"),
                                    (b"origin", b"http://127.0.0.1:8000"),
                                    (b"content-type", content_type.encode())]}, receive)

    async def go():
        resp = await routes[("GET", "/api/loci/export")](request("GET", "/api/loci/export"))
        assert resp.status_code == 200 and resp.media_type == "application/zip"
        data = Path(resp.path).read_bytes()
        assert resp.headers["x-loci-filtered"] == "3"
        Path(resp.path).unlink()

        dst = exported["tmp"] / "panel-dst"
        dst.mkdir()
        target = BucketManager({"buckets_dir": str(dst)})
        _point_runtime(monkeypatch, dst, target)
        monkeypatch.setattr(sh, "migrate_engine", MigrateEngine(
            {"buckets_dir": str(dst)}, target, _FakeEmbedding(dst / "embeddings.db")))

        boundary = "loci-test-boundary"
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; "
                f"filename=\"p.zip\"\r\nContent-Type: application/zip\r\n\r\n").encode()
        body += data + f"\r\n--{boundary}--\r\n".encode()
        post = routes[("POST", "/api/loci/import-package")]
        resp = await post(request("POST", "/api/loci/import-package", body,
                                  f"multipart/form-data; boundary={boundary}"))
        parsed = json.loads(resp.body)
        assert resp.status_code == 200 and parsed["phase"] == "parsed", parsed
        assert parsed["package"]["library_schema_version"] == schema.CURRENT_VERSION

        # Without the same origin, nothing is accepted.
        bare = Request({"type": "http", "method": "POST", "path": "/", "query_string": b"",
                        "headers": [(b"content-type", b"application/json")]},
                       request("POST", "/").receive)
        assert (await post(bare)).status_code == 403

        resp = await post(request("POST", "/api/loci/import-package",
                                  json.dumps({"job_id": parsed["job_id"]}).encode()))
        assert resp.status_code == 202, resp.body
        for task in list(L._running):
            await task
        resp = await routes[("GET", "/api/loci/import-package")](
            request("GET", "/api/loci/import-package"))
        st = json.loads(resp.body)
        assert st["phase"] == "done" and st["library_state"]["mode"] == "fresh", st
        assert (await target.get(ids["promise"]))["metadata"]["cue"]["condition"]
    asyncio.run(go())


def test_an_imported_line_withdrawn_on_its_own_does_not_travel():
    class _Registry:
        def state_of(self, sid):
            return "withdrawn" if sid.id == "l0002" else "active"
    rows = b'{"id": "l0001", "text": "a"}\n{"id": "l0002", "text": "gone"}\n'
    ctx = EP._Scrub(frozenset(), _Registry(), "_sources/imports/imp_0123456789ab/c0001.jsonl")
    out = EP._scrub_import_lines(rows, ctx)
    assert b"gone" not in out and b"l0001" in out
