# -*- coding: utf-8 -*-
"""
tests/test_panel_similar_keep.py — 疑似重复's 「留着」 is remembered

POST /api/loci/similar/action {action: "keep", a, b} keeps the pair in
`<buckets>/_state/similar_kept.json` with both entries' version markers
(core/similar_kept.py); GET /api/loci/similar then leaves it out — across reloads, since
nothing about it lives in the page — until either entry's text changes, when it is judged
again. The file holds ids and markers, never a memory's text. A pair whose entry left the
page is not listed and its record goes the next time a pair is kept.
"""

import json
import sqlite3

import pytest

from _panel_kit import make_store, routes, run
from core import similar_kept as K
from core import similarity as SIM

SECRET = "words that must never reach the kept file"


@pytest.fixture
def lib(tmp_path, monkeypatch):
    store = make_store(tmp_path, monkeypatch)
    monkeypatch.setattr(SIM, "_REV_TTL", 0.0)     # every read sees the folder as it is now
    SIM.invalidate()

    async def seed():
        a = await store.create(f"Walked the dog by the river. {SECRET}", room="EVENT/SELF",
                               name="dog walk")
        b = await store.create(f"Took the dog along the river. {SECRET}", room="EVENT/SELF",
                               name="river walk")
        c = await store.create("Fixed the kitchen tap.", room="EVENT/SELF", name="tap")
        d = await store.create("Repaired the kitchen faucet.", room="EVENT/SELF",
                               name="faucet")
        return a, b, c, d
    a, b, c, d = run(seed())
    one, two = [1.0] + [0.0] * 7, [0.0, 1.0] + [0.0] * 6
    con = sqlite3.connect(str(tmp_path / "embeddings.db"))
    con.execute("create table embeddings (bucket_id text primary key, embedding text)")
    con.executemany("insert into embeddings values (?, ?)",
                    [(a, json.dumps(one)), (b, json.dumps(one)),
                     (c, json.dumps(two)), (d, json.dumps(two))])
    con.commit()
    con.close()
    yield {"store": store, "call": routes(monkeypatch), "ids": (a, b, c, d),
           "file": tmp_path / "_state" / K.FILE}
    SIM.invalidate()


def _listed(call) -> set:
    out = call("GET", "/api/loci/similar").json
    return {frozenset((p["a"]["id"], p["b"]["id"])) for p in out["pairs"]}


def _keep(call, a, b):
    return call("POST", "/api/loci/similar/action", body={"action": "keep", "a": a, "b": b})


def test_a_kept_pair_is_not_listed_again_and_the_file_holds_no_text(lib):
    # Criterion: after 留着 the pair is gone from every later GET, and what was written is
    # the two ids with their markers only.
    call, (a, b, c, d) = lib["call"], lib["ids"]
    assert _listed(call) == {frozenset((a, b)), frozenset((c, d))}
    r = _keep(call, a, b)
    assert r.status == 200 and r.json == {"ok": True, "action": "keep", "a": a, "b": b}
    out = call("GET", "/api/loci/similar").json
    assert {frozenset((p["a"]["id"], p["b"]["id"])) for p in out["pairs"]} == {
        frozenset((c, d))}
    assert out["matched"] == 1 and out["kept"] == 1
    assert _listed(call) == {frozenset((c, d))}            # a second load: still left out
    raw = lib["file"].read_text(encoding="utf-8")
    assert SECRET not in raw and "dog" not in raw and "river" not in raw
    [record] = json.loads(raw)["pairs"].values()
    assert set(record["marks"]) == {a, b} and set(record) == {"marks", "at"}


def test_a_change_to_either_entry_brings_the_pair_back(lib):
    # Criterion: the markers are compared with the entries now; a new text on one end is a
    # different pair to judge.
    store, call, (a, b, c, d) = lib["store"], lib["call"], lib["ids"]
    _keep(call, a, b)
    _keep(call, c, d)
    assert _listed(call) == set()
    run(store.update(b, content="Took the dog along the river, and it rained."))
    assert _listed(call) == {frozenset((a, b))}
    # A metadata-only edit is not a change to what the pair was judged on.
    run(store.update(c, name="the tap again"))
    assert frozenset((c, d)) not in _listed(call)


def test_a_pair_whose_entry_left_the_page_drops_out(lib):
    store, call, (a, b, c, d) = lib["store"], lib["call"], lib["ids"]
    _keep(call, a, b)
    run(store.delete(b))
    assert frozenset((a, b)) not in _listed(call)
    assert call("GET", "/api/loci/similar").json["kept"] == 0
    _keep(call, c, d)                                      # the next keep tidies the file
    pairs = json.loads(lib["file"].read_text(encoding="utf-8"))["pairs"]
    assert list(pairs) == [K.key(c, d)]


def test_keep_takes_only_a_pair_the_page_computed(lib):
    call, (a, b, c, d) = lib["call"], lib["ids"]
    assert _keep(call, a, "").status == 400
    assert _keep(call, a, a).status == 400
    assert _keep(call, a, c).status == 409                 # both on the page, not a pair
    assert _keep(call, a, "f" * 12).status == 409
    assert not lib["file"].exists()


def test_an_export_package_carries_only_pairs_whose_entries_travel(lib):
    from core import export_package as EP
    call, (a, b, c, d) = lib["call"], lib["ids"]
    _keep(call, a, b)
    _keep(call, c, d)
    data = lib["file"].read_bytes()
    out = EP._scrub_similar_kept(data, EP._Scrub(frozenset({b})))
    assert list(json.loads(out)["pairs"]) == [K.key(c, d)]
    assert EP._scrub_similar_kept(data, EP._Scrub(frozenset({b, d}))) is None
    assert any(s.path == f"_state/{K.FILE}" for s in EP.STATE_FILES)
