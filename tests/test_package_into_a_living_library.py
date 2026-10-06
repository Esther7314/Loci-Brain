# -*- coding: utf-8 -*-
"""
tests/test_package_into_a_living_library.py — a package brought back into a library that
has lived on since it was exported.

  · what the receiving library has withdrawn stays withdrawn: an entry standing on a source
    its registry withdrew, deleted or holds, and whatever is derived from one, is not
    written — not over the cleared entry, not as a copy, not into another library that
    only knows the withdrawal;
  · the package's registry never settles a source the receiving library's own entries
    stand on (a withdrawal, a hold, a run over one of their lines);
  · keep_both copies get ids in the library's shape, and every link that named the
    package's entry names the copy;
  · an entry already there byte for byte is imported, not archived again; meaning vectors
    travel with the content vectors;
  · an import cut off half way is shown after a restart and carried on by a second run of
    the same package, in the first run's mode;
  · a library past the old 9000-member cap exports and parses.
"""

import json
import zipfile
from pathlib import Path

import pytest

from core import _invalidation as I
from core import _source_change as SC
from core import export_package as EP
from core import schema
from core import visibility as V
from core.bucket_manager import BucketManager
from core.package_import import JOB_FILE, MigrateEngine
from test_export_package import (HOST, _build_library, _FakeEmbedding, _point_runtime,
                                 _vector, run)
from utils import WAS_DERIVED_FROM, is_bucket_id

S_GONE = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0100"}
S_GONE_STR = "lento:home/private:U#m_0100"
PHRASE = "那把红色的伞放在门后"
DERIVED_PHRASE = "从红伞想到要买新伞"


def _library(root: Path, monkeypatch) -> BucketManager:
    root.mkdir(parents=True, exist_ok=True)
    schema.stamp_new_library(root)
    store = BucketManager({"buckets_dir": str(root)})
    _point_runtime(monkeypatch, root, store)
    _FakeEmbedding(root / "embeddings.db")
    return store


def _export(store, root: Path) -> str:
    path, _manifest = run(EP.build_package(
        store, embedding_db_path=str(root / "embeddings.db"),
        export_meta={"exported_at": "2026-10-03T00:00:00Z", "version": "test",
                     "embedding": {"model": "fake-embed", "dim": 3, "backend": "api"}},
        alias_path=str(root / "aliases.yaml")))
    return path


def _import(zip_path: str, store, root: Path, monkeypatch, *, default="skip",
            decisions=None, engine=None):
    _point_runtime(monkeypatch, root, store)
    engine = engine or MigrateEngine({"buckets_dir": str(root)}, store,
                                     _FakeEmbedding(root / "embeddings.db"))

    async def go():
        parsed = await engine.parse_zip_file(zip_path)
        assert parsed["ok"], parsed
        chosen = {c["bucket_id"]: default for c in parsed["conflicts"]}
        chosen.update(decisions or {})
        await engine.apply(chosen)
        return parsed, engine.get_status()
    return run(go())


def _text_on_disk(root: Path, phrase: str) -> list[str]:
    hits = []
    for p in root.rglob("*"):
        if p.is_file() and "_backups" not in p.parts:
            if phrase.encode("utf-8") in p.read_bytes():
                hits.append(p.relative_to(root).as_posix())
    return hits


@pytest.fixture
def withdrawn_after_export(tmp_path, monkeypatch):
    """A library exported, then a source withdrawn in it: the entry standing on it is
    cleared, the one derived from it blocked."""
    root = tmp_path / "lib"
    store = _library(root, monkeypatch)
    ids = {}

    async def go():
        ids["on"] = await store.create(f"{PHRASE}。", room="EVENT/SELF", sources=[S_GONE])
        ids["child"] = await store.create(
            f"{DERIVED_PHRASE}。", room="MIND/VIEWS",
            prov=[{"rel": WAS_DERIVED_FROM, "target": ids["on"]}])
        ids["other"] = await store.create("另一件不相干的事。", room="EVENT/SELF")
    run(go())
    zip_path = _export(store, root)
    status, out = run(SC.handle(store, {"change_id": "w-1", "source": S_GONE_STR,
                                        "host_seq": 1, "change": "withdrawn"}, HOST))
    assert status == 200 and out["status"] == "applied", out
    assert not _text_on_disk(root, PHRASE)
    return {"root": root, "store": store, "ids": ids, "zip": zip_path, "tmp": tmp_path}


@pytest.mark.parametrize("default", ["overwrite", "keep_both"])
def test_a_backup_brought_back_after_a_withdrawal_stays_withdrawn(withdrawn_after_export,
                                                                  monkeypatch, default):
    w = withdrawn_after_export
    store, ids, root = w["store"], w["ids"], w["root"]
    _parsed, st = _import(w["zip"], store, root, monkeypatch, default=default)
    assert st["phase"] == "done", st
    refused = {r["bucket_id"]: r["why"] for r in st["refused"]}
    assert set(refused) == {ids["on"], ids["child"]} and refused[ids["on"]] == "withdrawn"
    # The library's own copy of the derived entry is blocked: it is neither overwritten
    # nor copied, whichever of the two reasons names it first.
    assert refused[ids["child"]] in ("derived", "blocked_here"), refused
    assert st["result"]["refused"] == 2 and all(r["say"] for r in st["refused"])
    assert not _text_on_disk(root, PHRASE), "the cleared text came back"
    on = run(store.get_including_archive(ids["on"]))
    assert on["content"].strip() == store.CLEARED_BODY
    child = run(store.get_including_archive(ids["child"]))["metadata"]
    assert V.source_gone(child)
    copies = [b for b in run(store.list_all(include_archive=True))
              if DERIVED_PHRASE in str(b.get("content") or "")]
    assert [b["id"] for b in copies] == [ids["child"]], "no readable copy of the derived"


def test_a_library_that_only_knows_the_withdrawal_takes_none_of_it(withdrawn_after_export,
                                                                   monkeypatch):
    w = withdrawn_after_export
    other_root = w["tmp"] / "other"
    other = _library(other_root, monkeypatch)
    run(other.create("这个库自己的一条。", room="EVENT/SELF"))
    run(SC.handle(other, {"change_id": "w-9", "source": S_GONE_STR, "host_seq": 1,
                          "change": "withdrawn"}, HOST))
    _parsed, st = _import(w["zip"], other, other_root, monkeypatch)
    refused = {r["bucket_id"]: r["why"] for r in st["refused"]}
    assert refused == {w["ids"]["on"]: "withdrawn", w["ids"]["child"]: "derived"}
    assert not _text_on_disk(other_root, PHRASE)
    assert not _text_on_disk(other_root, DERIVED_PHRASE)
    assert run(other.get(w["ids"]["other"])) is not None


def test_a_source_held_here_keeps_what_stands_on_it_out(withdrawn_after_export, monkeypatch):
    w = withdrawn_after_export
    held_root = w["tmp"] / "held"
    held = _library(held_root, monkeypatch)
    run(held.create("这个库自己的一条。", room="EVENT/SELF"))
    run(SC.hold(held, S_GONE_STR, "withdrawn", "life"))
    _parsed, st = _import(w["zip"], held, held_root, monkeypatch)
    refused = {r["bucket_id"]: r["why"] for r in st["refused"]}
    assert refused == {w["ids"]["on"]: "held", w["ids"]["child"]: "derived"}
    assert not _text_on_disk(held_root, PHRASE)


# ───────────────────────── the package's registry and the library's ground ─────────────────────────

S_OWN = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0200"}
RUN = {"system": "lento", "instance": "home", "container": "chat:G", "id": "m_0010",
       "through": "m_0012"}


def test_a_package_never_settles_a_source_the_library_stands_on(tmp_path, monkeypatch):
    # The package's library withdrew a source and a run, and holds another; the receiving
    # library's own entries stand on each of them (one on a line inside the run).
    pkg_root = tmp_path / "pkg"
    pkg = _library(pkg_root, monkeypatch)
    run(pkg.create("包那边的一条。", room="EVENT/SELF"))
    run(pkg.create("包那边依据那段群聊的。", room="EVENT/SELF", sources=[RUN]))
    status, out = run(SC.handle_lines(pkg, {"source": RUN, "revision": "w-1",
                                            "lines": ["m_0010", "m_0011", "m_0012"]}, HOST))
    assert status == 200, out
    for cid, source in (("p-1", "lento:home/private:U#m_0200"),
                        ("p-2", "lento:home/chat:G#m_0010..m_0012")):
        status, out = run(SC.handle(pkg, {"change_id": cid, "source": source,
                                          "host_seq": 1, "change": "withdrawn"}, HOST))
        assert out["state"] == "withdrawn", out
    run(SC.hold(pkg, "lento:home/private:U#m_0300", "deleted", "life"))
    zip_path = _export(pkg, pkg_root)

    root = tmp_path / "recv"
    store = _library(root, monkeypatch)
    mine = {}

    async def own():
        mine["a"] = await store.create("我这边依据 m_0200 的。", room="EVENT/SELF",
                                       sources=[S_OWN])
        mine["b"] = await store.create("我这边依据群聊里一行的。", room="EVENT/SELF",
                                       sources=[{**RUN, "id": "m_0011", "through": None}])
        mine["c"] = await store.create("我这边依据 m_0300 的。", room="EVENT/SELF",
                                       sources=[{**S_OWN, "id": "m_0300"}])
    run(own())
    _parsed, st = _import(zip_path, store, root, monkeypatch)
    assert st["phase"] == "done", st
    merged = st["library_state"]["merged"]["_sources"]
    assert set(merged["refused_stood_on"]) == {
        "lento:home/private:U#m_0200", "lento:home/chat:G#m_0010..m_0012",
        "lento:home/private:U#m_0300"}, merged
    registry = store.sources
    registry.rebuild_index()
    assert registry.state_of("lento:home/private:U#m_0200") == "active"
    assert registry.state_of("lento:home/chat:G#m_0011") == "active"
    assert registry.held_of("lento:home/private:U#m_0300") is None
    for bid in mine.values():
        meta = run(store.get(bid))["metadata"]
        assert not EP.withdrawn(meta, registry) and not I.open_records(meta), bid


# ───────────────────────── keep_both copies ─────────────────────────

def test_keep_both_copies_get_library_ids_and_their_links_follow(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    src = _library(src_root, monkeypatch)
    ids = {}

    async def go():
        ids["a"] = await src.create("A：周一去了海边。", room="EVENT/SELF")
        ids["b"] = await src.create("B：从海边那天想到的。", room="MIND/VIEWS",
                                    prov=[{"rel": WAS_DERIVED_FROM, "target": ids["a"]}])
        ids["c"] = await src.create("C：这周的概括。", room="EVENT/SELF")
        await src.update(ids["c"], cover=[ids["a"]])
        await src.update(ids["a"], covered_by=ids["c"])
    run(go())
    media = src_root / "_media" / ids["a"]
    media.mkdir(parents=True)
    (media / "sea.jpg").write_bytes(b"\xff\xd8 sea")
    import frontmatter
    path = Path(src._find_bucket_file(ids["a"]))
    post = frontmatter.load(path)
    post["media"] = [{"path": f"_media/{ids['a']}/sea.jpg", "type": "image/jpeg"}]
    path.write_text(frontmatter.dumps(post), encoding="utf-8")
    zip_path = _export(src, src_root)

    root = tmp_path / "recv"
    store = _library(root, monkeypatch)
    run(store.create("撞了号的另一条。", room="EVENT/SELF", bucket_id_override=ids["a"]))
    _parsed, st = _import(zip_path, store, root, monkeypatch, default="keep_both")
    assert st["phase"] == "done" and not st["refused"], st
    texts = {str(b["content"]).strip(): b for b in run(store.list_all(include_archive=True))}
    copy = texts["A：周一去了海边。"]
    new = copy["id"]
    assert new != ids["a"] and is_bucket_id(new) and len(new) == 12
    assert run(store.get(new))["content"].strip() == "A：周一去了海边。"
    assert run(store.get(ids["a"]))["content"].strip() == "撞了号的另一条。"
    b = run(store.get(ids["b"]))["metadata"]
    assert [p["target"] for p in b["prov"]] == [new]
    c = run(store.get(ids["c"]))["metadata"]
    assert c["cover"] == [new] or c["cover"] == new
    assert copy["metadata"]["covered_by"] == ids["c"]
    assert copy["metadata"]["media"][0]["path"] == f"_media/{new}/sea.jpg"
    assert (root / "_media" / new / "sea.jpg").read_bytes() == b"\xff\xd8 sea"
    assert not (root / "_media" / ids["a"]).exists()


# ───────────────────────── identical entries, meaning vectors ─────────────────────────

def test_a_self_restore_archives_nothing_and_meaning_vectors_travel(tmp_path, monkeypatch):
    root = tmp_path / "lib"
    store = _library(root, monkeypatch)
    ids = [run(store.create(f"第{i}条，记了点事。", room="EVENT/SELF")) for i in range(4)]
    for i, bid in enumerate(ids):
        _vector(root / "embeddings.db", bid, [0.1 * (i + 1), 0.2, 0.3])
    import sqlite3
    from contextlib import closing
    with closing(sqlite3.connect(root / "embeddings.db")) as c:
        c.execute("UPDATE embeddings SET meaning_embedding = ? WHERE bucket_id = ?",
                  (json.dumps([0.9, 0.8, 0.7]), ids[0]))
        c.commit()
    zip_path = _export(store, root)
    before = len(run(store.list_all(include_archive=True)))

    _parsed, st = _import(zip_path, store, root, monkeypatch, default="overwrite")
    assert st["phase"] == "done", st
    assert st["result"]["imported"] == 4 and st["result"]["skipped"] == 0
    assert len(run(store.list_all(include_archive=True))) == before, "archived copies"

    dst_root = tmp_path / "dst"
    dst = _library(dst_root, monkeypatch)
    _import(zip_path, dst, dst_root, monkeypatch)
    a = EP.library_snapshot(root, alias_path=str(root / "aliases.yaml"))
    b = EP.library_snapshot(dst_root, alias_path=str(dst_root / "aliases.yaml"))
    assert b["meaning_vectors"] == {ids[0]: [0.9, 0.8, 0.7]}
    assert a["meaning_vectors"] == b["meaning_vectors"] and a["vectors"] == b["vectors"]


def test_an_overwritten_entry_keeps_its_old_version_under_a_library_id(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    src = _library(src_root, monkeypatch)
    bid = run(src.create("包里的版本。", room="EVENT/SELF"))
    zip_path = _export(src, src_root)
    root = tmp_path / "recv"
    store = _library(root, monkeypatch)
    run(store.create("库里原来的版本。", room="EVENT/SELF", bucket_id_override=bid))
    _import(zip_path, store, root, monkeypatch, default="overwrite")
    old = [b for b in run(store.list_all(include_archive=True))
           if str(b["content"]).strip() == "库里原来的版本。"]
    assert len(old) == 1 and is_bucket_id(old[0]["id"]) and old[0]["id"] != bid
    assert run(store.get_including_archive(old[0]["id"])) is not None


# ───────────────────────── an import cut off half way ─────────────────────────

class _Killed(BaseException):
    """The process dying under the import: nothing after it runs."""


def test_an_interrupted_import_shows_after_a_restart_and_carries_on(tmp_path, monkeypatch):
    src_root = tmp_path / "src"
    src_root.mkdir()
    lib = _build_library(src_root, monkeypatch)
    zip_path = _export(lib["store"], src_root)
    with zipfile.ZipFile(zip_path) as z:
        manifest = json.loads(z.read("backup_manifest.json"))
    left_out = set(manifest["package"]["filtered"]["deleted"]) | set(
        manifest["package"]["filtered"]["withdrawn"])
    want = EP.library_snapshot(src_root, alias_path=str(src_root / "aliases.yaml"),
                               left_out=left_out)

    root = tmp_path / "dst"
    store = _library(root, monkeypatch)
    engine = MigrateEngine({"buckets_dir": str(root)}, store,
                           _FakeEmbedding(root / "embeddings.db"))
    real = engine._apply_one_bucket
    calls = {"n": 0}

    async def dies_half_way(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] > 5:
            raise _Killed()
        return await real(*args, **kwargs)
    monkeypatch.setattr(engine, "_apply_one_bucket", dies_half_way)
    with pytest.raises(_Killed):
        _import(zip_path, store, root, monkeypatch, engine=engine)
    assert (root / JOB_FILE).is_file()

    # A restart: a new engine reads the job off disk.
    store = BucketManager({"buckets_dir": str(root)})
    restarted = MigrateEngine({"buckets_dir": str(root)}, store,
                              _FakeEmbedding(root / "embeddings.db"))
    job = restarted.get_status()["job"]
    assert job["phase"] == "interrupted" and job["mode"] == "fresh", job
    assert job["say"]

    parsed, st = _import(zip_path, store, root, monkeypatch, engine=restarted)
    assert len(parsed["conflicts"]) == 5
    assert st["phase"] == "done" and st["resumed"], st
    assert st["library_state"]["mode"] == "fresh" and not st["library_state"]["not_merged"]
    assert st["result"]["imported"] == parsed["total_buckets"], st["result"]
    assert st["apply_errors"] == [], st["apply_errors"]
    got = EP.library_snapshot(root, alias_path=str(root / "aliases.yaml"))
    for section in ("entries", "originals", "media", "vectors", "state", "registry",
                    "names"):
        assert got[section] == want[section], section
    assert restarted.get_status()["job"]["phase"] == "done"


def test_a_package_into_a_library_with_entries_backs_the_library_up_first(
        withdrawn_after_export, monkeypatch):
    w = withdrawn_after_export
    _parsed, st = _import(w["zip"], w["store"], w["root"], monkeypatch)
    assert st["backup"] and Path(st["backup"]).is_file()
    assert st["job"]["backup"] == st["backup"]


# ───────────────────────── past the old member cap ─────────────────────────

def test_a_package_past_nine_thousand_members_is_written_and_read():
    """The member cap counts entries, originals and attachments: a package of 9100 members
    is written by the export's writer and passes the importer's checks (a whole library of
    that size takes minutes to build here, so the two ends are driven directly)."""
    import io

    from core import package_import as ME
    from core import backup_archive as BA

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        writer = EP._Writer(archive)
        for n in range(9100):
            writer.data(f"buckets/dynamic/general/e_{n:012x}.md", b"---\nid: x\n---\nx\n")
    with zipfile.ZipFile(io.BytesIO(buffer.getvalue())) as archive:
        infos = BA._validate_infos(archive.infolist(), len(buffer.getvalue()),
                                   max_members=BA.MIGRATE_MAX_MEMBERS,
                                   max_total_bytes=BA.MIGRATE_MAX_TOTAL_UNCOMPRESSED_BYTES,
                                   member_limit=BA._migration_member_limit)
    assert len(infos) == 9100
    assert ME._MAX_EMBEDDING_ROWS >= BA.MIGRATE_MAX_MEMBERS
