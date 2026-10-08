# -*- coding: utf-8 -*-
"""
tests/test_panel_dreams.py — the panel's copy of each dream (core/_dream_archive.py) and
the page that reads it (GET /api/loci/dreams).

A dream is forgotten for real: the whole text goes at waking, the file when it has faded.
The panel keeps its own copy for three natural days — whole text, state, the thread
candidates and whether one was kept — and nothing the model or a host reads reaches it.
A source withdrawn clears the copy's words as it clears the dream's.

Real store on a temp dir; the weaver and the host are stand-ins.
"""

import asyncio
import io
import json
import os
import re
import zipfile
from datetime import timedelta
from pathlib import Path

import pytest

import tools.grow as grow_mod
from _panel_kit import make_store, routes
from core import _dream as D
from core import _dream_archive as A
from core import _source_change as SC
from core import _when as W
from core import runtime as rt
from core.scope import Host
from tools.grow import rooms_path

PHRASE = "青柠汽水在发光"
FRAGMENT = "海。门。"
MEET = "汽水瓶"
ELSEWHERE = "海在屋子里面"
WHERE = {"system": "lento", "instance": "home", "container": "private:U"}
M_STR = "lento:home/private:U#m_0003"
OPEN_HOST = Host("life", scope_mode="open", may_restore=True)
WITHDRAW = {"change_id": "c-1", "source": M_STR, "host_seq": 1, "change": "withdrawn"}
SRC = Path(__file__).resolve().parent.parent / "src"


def run(coro):
    return asyncio.run(coro)


class _Engine:
    async def ensure_started(self):
        pass


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = make_store(tmp_path, monkeypatch)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(grow_mod, "_sweep_started", True)
    table = tmp_path / "aliases.yaml"
    table.write_text("", encoding="utf-8")
    monkeypatch.setenv("LOCI_ALIAS_TABLE", str(table))
    from core import names as N
    monkeypatch.setattr(N, "_cache", None)

    async def weaver(ingredients, c):
        return D.parse_dream(json.dumps({
            "完整": f"梦里{PHRASE}，{ELSEWHERE}，门一直开着。", "碎片": FRAGMENT,
            "v": 0.4, "a": 0.6, "线索": [{"碰到": MEET, "想起": "答应小周的事"}]},
            ensure_ascii=False))
    monkeypatch.setattr(D, "call_model", weaver)
    return mgr


def _want(store):
    return run(store.create("答应小周：周六去海边，一直没去成。", tags=["t"], room="EVENT/SELF",
                            direction_of_fit="telic", weight=0.9, valence=0.4, arousal=0.6,
                            sources=[{**WHERE, "id": "m_0003"}]))


def _weave(store):
    want = _want(store)
    out = run(D.weave(force=True))
    assert out is not None
    return want, out["id"]


def _fade_out(dream_id):
    """Wake, then let the fragment and the one line run out, then sweep."""
    assert D.degrade_on_wake() == [dream_id]
    rec = next(r for r in D.load_dreams() if r["id"] == dream_id)
    rec["起算点"] = (W.now() - timedelta(hours=3)).isoformat(timespec="seconds")
    D.save_record(rec)
    assert dream_id in run(D.sweep_expired())["删了"]
    assert D.load_dreams() == []


def _files_holding(root, needle: str) -> list[str]:
    raw = needle.encode("utf-8")
    hits = []
    for dirpath, _dirs, files in os.walk(root):
        for f in files:
            with open(os.path.join(dirpath, f), "rb") as fh:
                if raw in fh.read():
                    hits.append(os.path.relpath(os.path.join(dirpath, f), root))
    return hits


# ───────────────────────── the copy, through the lifecycle ─────────────────────────

def test_the_copy_keeps_the_whole_text_through_waking_and_fading(store, monkeypatch):
    _want_id, did = _weave(store)
    call = routes(monkeypatch)
    page = call("GET", "/api/loci/dreams").json
    row = page["items"][0]
    assert row["id"] == did and row["state"] == "waiting" and row["state_words"] == "还没送到"
    assert PHRASE in row["text"] and row["whole"] is True
    assert row["night"] == W.now().date().isoformat()
    assert row["kept"] == [{"meet": MEET, "recall": "答应小周的事", "kept": False, "ids": []}]
    assert page["total"] == 1 and page["next_offset"] is None and page["scope"]

    D.degrade_on_wake()
    assert "完整" not in D.load_dreams()[0]           # the dream itself lost it
    row = call("GET", "/api/loci/dreams").json["items"][0]
    assert row["state"] == "fading" and row["state_words"] == "送到了·在散"
    assert PHRASE in row["text"] and row["degraded_at"]

    rec = D.load_dreams()[0]
    rec["起算点"] = (W.now() - timedelta(hours=3)).isoformat(timespec="seconds")
    D.save_record(rec)
    run(D.sweep_expired())
    assert D.load_dreams() == []                      # the file is gone
    row = call("GET", "/api/loci/dreams").json["items"][0]
    assert row["state"] == "gone" and row["state_words"] == "散了" and row["gone_at"]
    assert PHRASE in row["text"]


def test_a_candidate_is_kept_once_a_cue_saying_it_is_written_after_the_dream(store, monkeypatch):
    want, _did = _weave(store)
    call = routes(monkeypatch)
    assert call("GET", "/api/loci/dreams").json["items"][0]["kept"][0]["kept"] is False
    assert run(store.update(want, cue={"condition": MEET, "phrasings": []}))
    kept = call("GET", "/api/loci/dreams").json["items"][0]["kept"][0]
    # The entry carrying the thread is what the panel opens.
    assert kept["kept"] is True and kept["ids"] == [want]


class _View:
    """A request's view as the read gate asks it: everything in scope, and the registry
    saying a source behind `blocked` is withdrawn."""

    def __init__(self, blocked=()):
        self.blocked = set(blocked)

    def permits(self, meta):
        return True

    def source_blocked(self, meta):
        return meta.get("id") in self.blocked


def test_a_kept_thread_names_the_entries_carrying_it_and_never_one_on_a_withdrawn_source():
    rec = {"id": "d1", "woven_at": (W.now() - timedelta(hours=1)).isoformat(),
           "candidates": [{"meet": MEET, "recall": "x"}, {"meet": "别的", "recall": "y"}]}
    events = [{"event_type": "TraceUpdated", "trace_id": t, "recorded_at": W.now().isoformat(),
               "payload": {"changed_fields": ["cue"]}} for t in ("e1", "e2")]
    lib = [{"id": "e1", "metadata": {"id": "e1", "created": "2026-10-01T10:00:00+08:00",
                                     "cue": {"condition": MEET}}},
           {"id": "e2", "metadata": {"id": "e2", "created": "2026-10-02T10:00:00+08:00",
                                     "cue": {"condition": f"又看到{MEET}"}}}]
    kept, other = A.kept_candidates(rec, lib, events, _View())
    assert kept["kept"] is True and kept["ids"] == ["e2", "e1"]     # newest written first
    assert other == {"meet": "别的", "recall": "y", "kept": False, "ids": []}
    # The registry says what e2 rests on is withdrawn: not opened, not counted.
    kept, _ = A.kept_candidates(rec, lib, events, _View(blocked={"e2"}))
    assert kept["ids"] == ["e1"]
    kept, _ = A.kept_candidates(rec, lib, events, _View(blocked={"e1", "e2"}))
    assert kept["kept"] is False and kept["ids"] == []


def test_a_cue_written_before_the_dream_does_not_count_as_kept(store):
    rec = {"id": "d1", "woven_at": (W.now() + timedelta(hours=1)).isoformat(),
           "candidates": [{"meet": MEET, "recall": "x"}]}
    events = [{"event_type": "TraceUpdated", "trace_id": "e1",
               "recorded_at": W.now().isoformat(), "payload": {"changed_fields": ["cue"]}}]
    lib = [{"id": "e1", "metadata": {"id": "e1", "cue": {"condition": MEET}}}]
    assert A.kept_candidates(rec, lib, events)[0]["kept"] is False
    rec["woven_at"] = (W.now() - timedelta(hours=1)).isoformat()
    assert A.kept_candidates(rec, lib, events)[0]["kept"] is True


# ───────────────────────── three natural days ─────────────────────────

def _copy_from(days_ago: int, did: str):
    woven = (W.now() - timedelta(days=days_ago)).replace(hour=1, minute=0, second=0)
    A.keep({"id": did, "织于": woven.isoformat(timespec="seconds"), "完整": f"{did} 的梦",
            "碎片": "x"}, state=A.GONE, at=woven)


def test_a_copy_is_shown_for_three_natural_days_then_deleted(store, monkeypatch, tmp_path):
    for days in (0, 1, 2, 3):
        _copy_from(days, f"day{days}")
    call = routes(monkeypatch)
    shown = [r["id"] for r in call("GET", "/api/loci/dreams").json["items"]]
    assert shown == ["day0", "day1", "day2"]           # today, yesterday, the day before
    assert {r["id"] for r in A.load()} == {"day0", "day1", "day2", "day3"}
    assert A.sweep() == ["day3"]
    assert {r["id"] for r in A.load()} == {"day0", "day1", "day2"}
    # A day later the day before yesterday has aged out too.
    assert A.sweep(now=W.now() + timedelta(days=1)) == ["day2"]


def test_the_daily_decay_cycle_and_the_dream_sweep_both_delete_aged_copies(store):
    from core.decay_engine import DecayEngine
    _copy_from(3, "old1")
    run(DecayEngine({}, store).run_decay_cycle())
    assert A.load() == []
    _copy_from(4, "old2")
    run(D.sweep_expired())
    assert A.load() == []


def test_the_page_pages_and_cuts_at_as_of(store, monkeypatch):
    for days in (0, 1, 2):
        _copy_from(days, f"day{days}")
    call = routes(monkeypatch)
    first = call("GET", "/api/loci/dreams", "limit=2").json
    assert [r["id"] for r in first["items"]] == ["day0", "day1"] and first["next_offset"] == 2
    before = (W.now() - timedelta(days=1)).replace(hour=12).isoformat(timespec="seconds")
    cut = call("GET", "/api/loci/dreams", f"as_of={before.replace('+', '%2B')}").json
    assert [r["id"] for r in cut["items"]] == ["day1", "day2"]
    assert call("GET", "/api/loci/dreams", "limit=x").status == 400


# ───────────────────────── no road of the model, no host, reaches it ─────────────────────────

# The modules allowed to name the copy: the engine writes it, the source change clears it,
# the decay cycle sweeps it, the panel's route reads it; the route table and the export's
# left-behind list name it in words.
_ALLOWED = {"core/_dream_archive.py", "core/_dream.py", "core/_source_change.py",
            "core/decay_engine.py", "web/loci_activity.py", "web/loci.py",
            "core/export_package.py"}


def test_only_the_panel_and_the_dream_lifecycle_name_the_copy():
    named = set()
    for p in SRC.rglob("*.py"):
        if re.search(r"dream_archive", p.read_text(encoding="utf-8")):
            named.add(p.relative_to(SRC).as_posix())
    assert named <= _ALLOWED, named - _ALLOWED
    # The dream engine writes the copy and never reads it; only the panel's route does.
    engine = (SRC / "core" / "_dream.py").read_text(encoding="utf-8")
    assert "_archive.load" not in engine and "panel_view" not in engine
    readers = {p for p in named if "_da.load(" in (SRC / p).read_text(encoding="utf-8")
               or "A.load(" in (SRC / p).read_text(encoding="utf-8")}
    assert readers == {"web/loci_activity.py"}


def test_no_road_of_the_model_and_no_host_route_reaches_the_copy(store, monkeypatch, tmp_path):
    from core import export_package
    from tools.muse import dispatch as muse
    from tools.recall import core as R
    from web import panel_auth as PA

    want, did = _weave(store)
    _fade_out(did)
    assert PHRASE in json.dumps(A.load(), ensure_ascii=False), "the copy holds the text"

    seen = []
    # The dream engine's own readers: the dream is gone for him.
    assert run(D.current_dream()) is None
    assert run(D.from_a_dream(f"梦里{PHRASE}，{ELSEWHERE}，门一直开着。", [])) is False
    # The tools.
    # Asked with the dream's other words (an echoed query is not a leak): the copy's
    # PHRASE and candidate must not come back.
    seen.append(run(R.recall_core(when="", room="", tag="", query=ELSEWHERE)))
    seen.append(run(R.recall_core(when="7d", room="", tag="", query="")))
    seen.append(run(muse()))
    # The host routes, with the host's credential.
    call = routes(monkeypatch)
    for method, path, query, body in (
            ("GET", "/api/loci/poke", "", None),
            ("GET", "/api/dream/current", "", None),
            ("POST", "/api/loci/dream/wake", "", {}),
            ("GET", "/api/v2/breath", "", None),
            ("GET", "/api/v2/breath", "format=json", None),
            ("POST", "/api/v2/cue", "", {"text": ELSEWHERE, "window": "w1", "turn": 1}),
            ("GET", "/api/v2/changes", "since=0", None),
            ("GET", "/api/muse/pending", "", None)):
        reply = call(method, path, query, body, key="s3cret")
        assert reply.status < 500, (path, reply.raw)
        seen.append(reply.raw.decode("utf-8", "replace"))
    for text in seen:
        assert PHRASE not in text and MEET not in text
    # The export package carries no file of it and names it as left behind.
    zpath, manifest = run(export_package.build_package(
        store, embedding_db_path=str(tmp_path / "embeddings.db"), export_meta={}))
    try:
        with zipfile.ZipFile(zpath) as z:
            for name in z.namelist():
                assert "dream_archive" not in name
                assert PHRASE.encode("utf-8") not in z.read(name), name
    finally:
        os.unlink(zpath)
    left = json.dumps(manifest, ensure_ascii=False)
    assert "_state/dream_archive/*" in left
    # The page itself is the panel's alone: never a hook path, refused to a host's key.
    assert "/api/loci/dreams" not in PA.HOOK_PATHS
    locked = routes(monkeypatch, locked=True)
    assert locked("GET", "/api/loci/dreams", key="s3cret").status == 401


# ───────────────────────── a withdrawn source clears the copy ─────────────────────────

def _withdraw(store):
    status, out = run(SC.handle(store, dict(WITHDRAW), OPEN_HOST))
    assert status == 200 and out["status"] == "applied", out
    return out


def test_a_withdrawal_clears_the_words_of_the_copy_and_of_the_dream(store, tmp_path,
                                                                     monkeypatch):
    _want_id, did = _weave(store)
    out = _withdraw(store)
    assert out["cleanup"]["dream_records"] == "done"
    assert D.load_dreams() == []
    assert _files_holding(tmp_path, PHRASE) == [] and _files_holding(tmp_path, MEET) == []
    row = routes(monkeypatch)("GET", "/api/loci/dreams").json["items"][0]
    assert row["id"] == did and row["cleared"] is True and row["text"] is None
    assert row["kept"] == [] and row["state"] == "gone"
    assert row["state_words"] == A.CLEARED_WORDS


def test_a_withdrawal_after_the_dream_faded_still_clears_the_copy_and_says_done(store,
                                                                                tmp_path):
    _want_id, did = _weave(store)
    _fade_out(did)
    out = _withdraw(store)
    # Only the copy held anything: the receipt still says something was cleared.
    assert out["cleanup"]["dream_records"] == "done"
    assert _files_holding(tmp_path, PHRASE) == []
    assert A.load()[0]["cleared"] is True


def test_a_withdrawal_that_reaches_no_dream_says_none(store, tmp_path):
    _copy_from(0, "other")
    _want(store)
    out = _withdraw(store)
    assert out["cleanup"]["dream_records"] == "none"
    assert A.load()[0]["cleared"] is False and A.load()[0]["text"] == "other 的梦"
