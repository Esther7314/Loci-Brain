# -*- coding: utf-8 -*-
"""
tests/test_ledger.py — the ledger: one number per line, whoever writes, and no text.

Several processes append to one `events.jsonl`. Every append holds the ledger's lease and
takes its number from the `.seq` file beside it, so threads and separate processes writing
at once get distinct, gapless numbers and lose nothing; a file that drifted from `.seq`
(a crash between the two writes, a hand edit) is read once and set right. A memory's line
holds names and identities, never its text; old lines carrying whole metadata are read
down to the same shape by `/changes` (`public_row`) and rewritten only for what a source
change clears. `/changes` leaves out what was merely touched and, for a host with a
ceiling, everything its credential does not reach.
"""

import asyncio
import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from core import _ledger as L
from core import _sources as S
from core.bucket_manager import BucketManager
from core.scope import Host
from core.ledger_mirror import LedgerMirror
from utils import WAS_DERIVED_FROM

SRC = Path(__file__).resolve().parent.parent / "src"


def run(coro):
    return asyncio.run(coro)


def _append(ledger, n, tag):
    for i in range(n):
        ledger.append_event(event_type="TraceCreated", trace_id=f"{tag}{i}",
                            trace_kind="dynamic", payload={}, body=str(i))


def test_threads_writing_at_once_get_distinct_gapless_numbers(tmp_path):
    ledger = LedgerMirror(tmp_path / "events.jsonl")
    threads = [threading.Thread(target=_append, args=(LedgerMirror(ledger.path), 25, f"t{k}"))
               for k in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    seqs = [e["seq"] for e in ledger.iter_events()]
    assert sorted(seqs) == list(range(1, 201)) and seqs == sorted(seqs)


_CHILD = """
import sys
sys.path.insert(0, {src!r})
from core.ledger_mirror import LedgerMirror
ledger = LedgerMirror({path!r})
for i in range({n}):
    ledger.append_event(event_type="TraceCreated", trace_id="p{k}-%d" % i,
                        trace_kind="dynamic", payload={{}}, body=str(i))
"""


def test_processes_writing_at_once_get_distinct_gapless_numbers(tmp_path):
    path = str(tmp_path / "events.jsonl")
    procs = [subprocess.Popen([sys.executable, "-c",
                               _CHILD.format(src=str(SRC), path=path, n=30, k=k)])
             for k in range(5)]
    for p in procs:
        assert p.wait(timeout=120) == 0
    events = list(LedgerMirror(path).iter_events())
    assert sorted(e["seq"] for e in events) == list(range(1, 151))
    assert len({e["trace_id"] for e in events}) == 150, "no line lost"


def test_the_next_number_does_not_reread_the_file(tmp_path, monkeypatch):
    ledger = LedgerMirror(tmp_path / "events.jsonl")
    _append(ledger, 3, "a")
    calls = {"n": 0}
    real = LedgerMirror.latest_seq

    def counted(self):
        calls["n"] += 1
        return real(self)
    monkeypatch.setattr(LedgerMirror, "latest_seq", counted)
    _append(ledger, 5, "b")
    assert calls["n"] == 0
    # Another writer's line, whatever its number, is in the tail the next append reads.
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"seq": 40, "event_type": "X", "trace_id": "z"}) + "\n")
    assert ledger.append_event(event_type="X", trace_id="y", trace_kind="k")["seq"] == 41
    assert calls["n"] == 0
    # A new writer reads the whole file once, on its first append: a number further back
    # than the tail still counts.
    with ledger.path.open("a", encoding="utf-8") as f:
        for i in range(2000):
            f.write(json.dumps({"seq": 5, "event_type": "X", "trace_id": f"pad{i}",
                                "payload": {"pad": "x" * 40}}) + "\n")
    fresh = LedgerMirror(ledger.path)
    assert fresh.append_event(event_type="X", trace_id="w", trace_kind="k")["seq"] == 42
    _append(fresh, 2, "c")
    assert calls["n"] == 1


def test_a_torn_last_line_is_skipped_and_the_next_starts_fresh(tmp_path):
    ledger = LedgerMirror(tmp_path / "events.jsonl")
    _append(ledger, 2, "a")
    with ledger.path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 3, "event_type": "Trace')
    event = ledger.append_event(event_type="TraceCreated", trace_id="b", trace_kind="k")
    assert event["seq"] == 3
    assert [e["seq"] for e in ledger.iter_events()] == [1, 2, 3]


def test_iter_since_reads_past_a_number(tmp_path):
    ledger = LedgerMirror(tmp_path / "events.jsonl")
    _append(ledger, 10, "a")
    assert [e["seq"] for e in ledger.iter_since(7)] == [8, 9, 10]
    assert list(ledger.iter_since(10)) == []


def test_a_memory_line_holds_names_and_identities_never_text(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    rec = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_1"}
    bid = run(store.create("小周说想吃青柠蛋糕。", name="青柠蛋糕", summary="想吃青柠蛋糕",
                           tags=["甜点"], sources=[rec], when="2026-10-04"))
    run(store.update(bid, name="青柠蛋糕二", pinned=False))
    raw = store.ledger_mirror.path.read_text(encoding="utf-8")
    assert "青柠" not in raw and "甜点" not in raw and "2026-10-04" not in raw
    created, updated = list(store.ledger_mirror.iter_events())[-2:]
    assert created["payload"]["sources"] == ["lento:home/private:U#m_1"]
    assert "name" in created["payload"]["fields"]
    assert updated["payload"]["changed_fields"] == ["name", "pinned"]
    assert updated["payload"]["flags"]["pinned"] is False


def test_an_old_line_is_read_without_its_text_and_rewritten_only_when_asked(tmp_path):
    ledger = LedgerMirror(tmp_path / "events.jsonl")
    ledger.append_event(event_type="TraceUpdated", trace_id="aaa", trace_kind="dynamic",
                        payload={"name": "青柠", "summary": "青柠蛋糕", "changed_fields": ["name"],
                                 "sources": [{"system": "s", "instance": "i",
                                              "container": "c", "id": "1", "use": "私密"}]})
    ledger.append_event(event_type="TraceUpdated", trace_id="bbb", trace_kind="dynamic",
                        payload={"name": "别的"})
    row = L.public_row(next(ledger.iter_events()))
    assert row == {"seq": 1, "type": "TraceUpdated", "recorded_at": row["recorded_at"],
                   "id": "aaa", "kind": "dynamic", "body_hash": row["body_hash"],
                   "changed_fields": ["name"], "sources": ["s:i/c#1"]}
    assert L.scrub_traces(ledger, ["aaa"]) == 1
    text = ledger.path.read_text(encoding="utf-8")
    assert "青柠" not in text and "私密" not in text and "别的" in text
    assert [e["seq"] for e in ledger.iter_events()] == [1, 2]
    assert ledger.append_event(event_type="X", trace_id="c", trace_kind="k")["seq"] == 3
    assert L.scrub_traces(ledger, ["aaa"]) == 0, "nothing left to rewrite"


def test_changes_leaves_out_touches_and_what_the_credential_does_not_reach(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    tg = {"system": "telegram", "instance": "bot-a", "container": "g", "id": "1"}
    home = {"system": "lento", "instance": "home", "container": "p", "id": "1"}
    mine = run(store.create("群里说的事。", sources=[tg]))
    derived = run(store.create("由群里那件事想到的。",
                               prov=[{"rel": WAS_DERIVED_FROM, "target": mine}]))
    theirs = run(store.create("家里的事。", sources=[home]))
    bare = run(store.create("没有来源的老记忆。"))
    run(store.touch(mine))
    run(store.update(mine, importance=7))
    bot = Host("bot", max_grant=(S.Place("telegram", "bot-a"),))
    out = run(L.changes_since(store, bot, 0))
    ids = {r.get("id") for r in out["changes"]}
    assert ids == {mine, derived}
    assert all(r["type"] != "TraceTouched" for r in out["changes"])
    life = Host("life", scope_mode="open")
    every = run(L.changes_since(store, life, 0))
    assert {mine, derived, theirs, bare} <= {r.get("id") for r in every["changes"]}
    assert "群里" not in json.dumps(every, ensure_ascii=False)
    page = run(L.changes_since(store, life, 0, limit=2))
    assert len(page["changes"]) == 2 and page["more"]
    rest = run(L.changes_since(store, life, page["next"]))
    assert [r["seq"] for r in page["changes"] + rest["changes"]] == \
        [r["seq"] for r in every["changes"]]
    assert run(L.changes_since(store, None, 0))["changes"] == []


# ── a host with a ceiling reads by cursor (the other team's 10-02 ruling) ──

def test_a_host_with_a_ceiling_never_sees_the_ledgers_numbers(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    tg = {"system": "telegram", "instance": "bot-a", "container": "g", "id": "1"}
    home = {"system": "lento", "instance": "home", "container": "p", "id": "1"}
    run(store.create("群里说的事。", sources=[tg]))
    for i in range(5):                                   # lines the bot may not see
        run(store.create(f"家里的事 {i}。", sources=[home]))
    mine = run(store.create("群里又说了一件。", sources=[tg]))
    bot = Host("bot", max_grant=(S.Place("telegram", "bot-a"),))
    out = run(L.changes_since(store, bot))
    assert "since" not in out and out["cursor"] is None
    assert all("seq" not in r and r["cursor"].startswith("c1") for r in out["changes"])
    text = json.dumps(out)
    for n in range(1, 12):
        assert f'"seq": {n}' not in text
    # Paging past what it may not see: the lines between are skipped, never counted.
    first = run(L.changes_since(store, bot, limit=1))
    assert first["more"] and len(first["changes"]) == 1
    second = run(L.changes_since(store, bot, limit=1, cursor=first["next"]))
    assert [r["id"] for r in second["changes"]] == [mine]
    caught_up = run(L.changes_since(store, bot, cursor=second["next"]))
    assert caught_up["changes"] == [] and not caught_up["more"]
    # A page whose every line is filtered out still moves the cursor on.
    run(store.create("家里又有一件事。", sources=[home]))
    hidden = run(L.changes_since(store, bot, cursor=caught_up["next"]))
    assert hidden["changes"] == [] and hidden["next"] != caught_up["next"]
    assert run(L.changes_since(store, bot, cursor=hidden["next"]))["next"] == hidden["next"]


def test_a_cursor_says_nothing_of_the_seq_and_is_the_hosts_own(tmp_path):
    a = LedgerMirror(tmp_path / "a" / "_ledger" / "events.jsonl")
    b = LedgerMirror(tmp_path / "b" / "_ledger" / "events.jsonl")
    tokens = [L.cursor_of(a, n, "bot") for n in range(1, 50)]
    assert len(set(tokens)) == len(tokens) and tokens != sorted(tokens)
    assert L.cursor_of(b, 7, "bot") != L.cursor_of(a, 7, "bot"), "keyed per library"
    assert all(L.seq_of_cursor(a, t, "bot") == n for n, t in enumerate(tokens, start=1))
    with pytest.raises(L.CursorError):
        L.seq_of_cursor(a, tokens[3], "other-host")
    with pytest.raises(L.CursorError):
        L.seq_of_cursor(b, tokens[3], "bot")
    with pytest.raises(L.CursorError):
        L.seq_of_cursor(a, "c1" + "0" * 32, "bot")
    assert L.cursor_of(LedgerMirror(a.path), 7, "bot") == L.cursor_of(a, 7, "bot"), "kept"


def test_an_open_host_keeps_the_global_seq(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    run(store.create("一件事。"))
    out = run(L.changes_since(store, Host("life", scope_mode="open"), 0))
    assert out["since"] == 0 and isinstance(out["next"], int)
    assert all(isinstance(r["seq"], int) for r in out["changes"])
