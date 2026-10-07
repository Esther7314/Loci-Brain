# -*- coding: utf-8 -*-
"""
tests/test_panel_changes_recent.py — the panel's regrow/fold page

`GET /api/loci/changes/recent` reads the ledger and the entries as they are now
(core/changes_feed.recent): a new version (改了一版), N entries merged into one, a period
ringed, a pin put on the door, and an entry whose basis moved with how it was dealt with —
rewritten, put away, or looked at and kept. Newest first, paged, each row with the
ledger's seq as its id and the entry's id; nothing else of the ledger reaches the page.
"""

from datetime import timedelta
from urllib.parse import urlencode

import pytest

from _panel_kit import make_store, routes, run
from core import _when as W
from core import changes_feed as CF
from core import scope as SC
from core import _fold as F
from tools.regrow import dispatch as regrow
from tools.trace import dispatch as trace

MARK = [{"kind": "overturn", "of": "a" * 12, "by": "b" * 12, "at": "2026-10-01T00:00:00"}]


@pytest.fixture
def store(tmp_path, monkeypatch):
    return make_store(tmp_path, monkeypatch)


def _rows(store):
    return CF.recent(list(store.ledger_mirror.iter_events()),
                     run(store.list_all(include_archive=True)))


def test_an_empty_ledger_is_an_empty_page(store, monkeypatch):
    r = routes(monkeypatch)("GET", "/api/loci/changes/recent")
    assert r.status == 200 and r.json["items"] == [] and r.json["total"] == 0
    assert r.json["next_offset"] is None and r.json["scope"] == SC.OPEN_LINE


def test_each_kind_newest_first_with_seq_and_entry_ids(store, tmp_path, monkeypatch):
    async def seed():
        plain = await store.create("Saturday we went up the hill.", room="EVENT/SELF", name="hill")
        a = await store.create("one", room="EVENT/SELF")
        b = await store.create("two", room="EVENT/SELF")
        c = await store.create("three", room="EVENT/SELF")
        gist, _ = await F.save_gist("Three walks in one week.", "MIND/TRAITS", 0.5, 0.3,
                                    [a, b, c])
        period = await store.create("The exam weeks.", room="EVENT/SELF", name="exams",
                                    tags=["__大event__"], when="2026-09-15..2026-09-21")
        pin = await store.create("Rest before arguing.", room="MIND/VIEWS", name="rest")
        await store.update(pin, pinned=True)
        unpinned = await store.create("Not a rule after all.", room="MIND/VIEWS")
        await store.update(unpinned, pinned=False)
        return plain, gist, period, pin

    plain, gist, period, pin = run(seed())
    out = run(regrow(bucket_id=plain, mode="supplement", text="Saturday we went up the hill, "
                     "and down again.", v=0.5, a=0.3))
    newer =[b for b in run(store.list_all()) if b["metadata"].get("supersedes") == plain]
    assert newer, out
    newer_id = newer[0]["metadata"]["id"]

    rows = _rows(store)
    kinds = [(r["kind"], r["entry"]["id"]) for r in rows]
    # Criterion: one row per change, newest first; plain creations, the unpin and the
    # merely touched are not on the page.
    assert kinds == [("revised", newer_id), ("pinned", pin), ("period", period),
                     ("merged", gist)]
    seqs = [r["id"] for r in rows]
    assert all(isinstance(s, int) for s in seqs) and seqs == sorted(seqs, reverse=True)
    by_kind = {r["kind"]: r for r in rows}
    assert by_kind["revised"]["words"] == "改了一版" and by_kind["revised"]["of"] == plain
    assert by_kind["merged"]["words"] == "3 条合成一条" and by_kind["merged"]["count"] == 3
    assert by_kind["period"]["words"] == "圈了一段日子" and by_kind["period"]["span"]
    assert by_kind["pinned"]["words"] == "钉上门口"
    merged = by_kind["merged"]["entry"]
    assert (merged["id"], merged["short"]) == (gist, gist[:6])
    assert merged["text"].startswith("Three walks")
    assert all(r["at"] for r in rows)

    call = routes(monkeypatch)
    page = call("GET", "/api/loci/changes/recent", "limit=3").json
    assert [r["id"] for r in page["items"]] == seqs[:3] and page["next_offset"] == 3
    assert page["total"] == 4
    rest = call("GET", "/api/loci/changes/recent",
                "offset=3&" + urlencode({"as_of": page["as_of"]})).json
    assert [r["id"] for r in rest["items"]] == seqs[3:] and rest["next_offset"] is None
    earlier = (W.now() - timedelta(hours=1)).isoformat(timespec="seconds")
    assert call("GET", "/api/loci/changes/recent",
                urlencode({"as_of": earlier})).json["total"] == 0


def test_a_moved_basis_rewritten_put_away_or_kept(store, tmp_path, monkeypatch):
    async def seed():
        ids = []
        for text in ("Rewrite me.", "Put me away.", "Keep me."):
            bid = await store.create(text, room="MIND/VIEWS", name=text)
            assert await store.update(bid, invalidation=MARK)
            ids.append(bid)
        quiet = await store.create("Nothing moved under me.", room="EVENT/SELF")
        return ids, quiet
    (rewrite, away, keep), quiet = run(seed())
    run(regrow(bucket_id=rewrite, mode="supplement", text="Rewritten.", v=0.5, a=0.3))
    run(trace(bucket_id=away, delete=True))
    assert "确认照留" in run(trace(bucket_id=keep, invalidation="confirmed"))
    run(trace(bucket_id=quiet, delete=True))

    rows = [r for r in _rows(store) if r["kind"] == "invalidation"]
    how = {r["how"]: r for r in rows}
    # Criterion: one row each, and the rewrite is not told a second time as "revised".
    assert [r["how"] for r in rows] == ["kept", "archive", "rewrite"]
    assert how["kept"]["entry"]["id"] == keep and how["kept"]["words"] == "依据变了，看过照留"
    assert how["archive"]["entry"]["id"] == away and how["archive"]["words"] == "依据变了，收起来了"
    assert how["rewrite"]["of"] == rewrite and how["rewrite"]["words"] == "依据变了，重写了一版"
    assert not [r for r in _rows(store) if r["kind"] == "revised"]
    assert quiet not in {r["entry"]["id"] for r in _rows(store)}


def test_bad_paging_is_refused_and_the_page_is_the_panel_s(store, monkeypatch):
    assert routes(monkeypatch)("GET", "/api/loci/changes/recent", "limit=0").status == 400
    assert routes(monkeypatch, locked=True)("GET", "/api/loci/changes/recent").status == 401
