# -*- coding: utf-8 -*-
"""
tests/test_export_filters_bite.py — the export's own filters, checked on what lands inside
the package rather than against themselves.

  · withdrawn: a source the registry holds as withdrawn or deleted keeps the entry resting
    on it home — and everything derived from that entry — even when no `source_gone`
    record was written on them (the change recorded, its clearing never run); a held
    source travels held
  · the card ledger: a card about an entry left out is not in `_cue/ledger.jsonl`
  · the ledger behind /changes: a line written the old way (whole metadata) travels with
    its names cut and marked `redacted`; a source line travels whole — its source, change,
    state and entries are ids and states, never text
"""

import asyncio
import json
import zipfile
from pathlib import Path

from core import _ledger
from core import _sources as S
from core import export_package as EP
from core.bucket_manager import BucketManager
from utils import WAS_DERIVED_FROM

M_GONE = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0001"}
M_KEEP = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0002"}
GONE_PHRASE = "撤回的那句是绿色帐篷"
OLD_NAME = "旧写法里的名字是橘子汽水"


def run(coro):
    return asyncio.run(coro)


def _registry(tmp_path) -> S.SourceRegistry:
    return S.SourceRegistry(tmp_path)


def test_the_registry_alone_decides_a_withdrawn_or_deleted_source(tmp_path):
    reg = _registry(tmp_path)
    on_gone = {"id": "a" * 12, "sources": [M_GONE]}
    quoting = {"id": "b" * 12, "sources": [],
               "prov": [{"rel": "wasQuotedFrom", "target": "lento:home/private:U#m_0003"}]}
    held = {"id": "c" * 12, "sources": [{**M_GONE, "id": "m_0004"}]}
    assert not any(EP.withdrawn(m, reg) for m in (on_gone, quoting, held))
    run(reg.apply_change({"change_id": "w", "source": "lento:home/private:U#m_0001",
                          "kind": "withdrawn", "host_seq": 1}))
    run(reg.apply_change({"change_id": "d", "source": "lento:home/private:U#m_0003",
                          "kind": "deleted", "host_seq": 1}))
    reg.hold("lento:home/private:U#m_0004", "withdrawn", "life")
    assert EP.withdrawn(on_gone, reg), "no source_gone record: the registry says it"
    assert EP.withdrawn(quoting, reg), "a quoted line is a source too"
    assert not EP.withdrawn(held, reg), "held travels held"
    assert not EP.withdrawn(on_gone, None), "without a registry only the records speak"


def _library(root: Path) -> tuple[BucketManager, dict]:
    store = BucketManager({"buckets_dir": str(root)})
    ids = {}

    async def go():
        ids["gone"] = await store.create(f"{GONE_PHRASE}。", room="EVENT/WORLD", sources=[M_GONE])
        ids["child"] = await store.create(
            "从帐篷那件事想到的。", room="MIND/VIEWS",
            prov=[{"rel": WAS_DERIVED_FROM, "target": ids["gone"]}])
        ids["grandchild"] = await store.create(
            "再往下想一层。", room="MIND/VIEWS",
            prov=[{"rel": WAS_DERIVED_FROM, "target": ids["child"]}])
        ids["keep"] = await store.create("周六去海边。", room="EVENT/SELF", sources=[M_KEEP])
        # The change is recorded in the registry; its clearing never ran.
        await store.sources.apply_change({"change_id": "w", "kind": "withdrawn",
                                          "source": "lento:home/private:U#m_0001",
                                          "host_seq": 1})
    run(go())
    return store, ids


def _export(store, root: Path) -> tuple[str, dict]:
    return run(EP.build_package(
        store, embedding_db_path=str(root / "embeddings.db"),
        export_meta={"exported_at": "2026-10-03T00:00:00Z", "version": "test",
                     "embedding": {"model": "fake-embed", "dim": 3, "backend": "api"}},
        alias_path=str(root / "aliases.yaml")))


def _member(zip_path: str, name: str) -> list[dict]:
    with zipfile.ZipFile(zip_path) as z:
        return [json.loads(ln) for ln in z.read(name).decode("utf-8").splitlines() if ln.strip()]


def test_what_stands_on_a_source_the_registry_withdrew_stays_home(tmp_path):
    store, ids = _library(tmp_path)
    zip_path, manifest = _export(store, tmp_path)
    assert sorted(manifest["package"]["filtered"]["withdrawn"]) == sorted(
        [ids["gone"], ids["child"], ids["grandchild"]])
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
        blob = b"".join(z.read(n) for n in names)
    assert GONE_PHRASE.encode("utf-8") not in blob
    assert any(ids["keep"] in n for n in names)
    assert not [n for n in names if any(ids[k] in n for k in ("gone", "child", "grandchild"))]


def test_the_card_ledger_in_the_package_names_no_entry_left_out(tmp_path):
    store, ids = _library(tmp_path)
    store.cues.open_window("life", "w1", [f"{ids['gone']}@v1", f"{ids['keep']}@v1"])
    store.cues.offer("life", "w1", "t1", [
        {"card": f"{ids['gone']}@v1", "id": ids["gone"], "kind": "memory", "why": "帐篷"},
        {"card": f"{ids['keep']}@v1", "id": ids["keep"], "kind": "memory", "why": "海边"}])
    zip_path, _manifest = _export(store, tmp_path)
    text = json.dumps(_member(zip_path, EP.STATE_PREFIX + "_cue/ledger.jsonl"), ensure_ascii=False)
    assert ids["keep"] in text, "the card about an entry that travels is there"
    assert ids["gone"] not in text


def test_the_ledger_in_the_package_is_cut_to_what_a_line_may_hold(tmp_path):
    store, ids = _library(tmp_path)
    store.ledger_mirror.append_event(event_type="TraceUpdated", trace_id=ids["keep"],
                                     trace_kind="dynamic",
                                     payload={"name": OLD_NAME, "changed_fields": ["name"]},
                                     body="x")
    store.ledger_mirror.append_event(
        event_type=_ledger.SOURCE_CHANGED, trace_id="", trace_kind="source",
        payload={"source": "lento:home/private:U#m_0001", "change": "withdrawn",
                 "change_id": "w", "host": "life", "host_seq": 1, "state": "withdrawn",
                 "previous": "active"})
    store.ledger_mirror.append_event(
        event_type=_ledger.SOURCE_CLEARED, trace_id="", trace_kind="source",
        payload={"source": "lento:home/private:U#m_0001", "change_id": "w",
                 "place": "body", "entries": [ids["gone"]]})
    zip_path, _manifest = _export(store, tmp_path)
    rows = _member(zip_path, EP.STATE_PREFIX + "_ledger/events.jsonl")
    assert OLD_NAME not in json.dumps(rows, ensure_ascii=False)
    [old] = [r for r in rows if r.get("trace_id") == ids["keep"]
             and r.get("event_type") == "TraceUpdated"]
    assert old["payload"]["redacted"] is True and old["payload"]["changed_fields"] == ["name"]
    [changed] = [r for r in rows if r["event_type"] == _ledger.SOURCE_CHANGED]
    assert changed["payload"] == {"source": "lento:home/private:U#m_0001",
                                  "change": "withdrawn", "change_id": "w", "host": "life",
                                  "host_seq": 1, "state": "withdrawn", "previous": "active"}
    [cleared] = [r for r in rows if r["event_type"] == _ledger.SOURCE_CLEARED]
    assert cleared["payload"]["place"] == "body" and "redacted" not in cleared["payload"]
