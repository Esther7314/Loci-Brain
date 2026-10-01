# -*- coding: utf-8 -*-
"""
tests/test_source_registry.py — each source's own state, and the file it lives in.

The registry is the one record of what the host said about its material: withdrawn,
deleted, unreadable, restored, revised, a new permission. That cannot be read back from
the memories, so its truth is the append-only `_sources/changes.jsonl` and everything
here is asserted twice where it matters: on the live registry, and on a fresh one built
from the same file (a restart). Order is host_seq's alone; a resend is a duplicate, a
reused change_id or host_seq is a conflict that touches nothing.
"""

import asyncio
import json

import pytest

from core import _sources as S
from core.bucket_manager import BucketManager

SRC = "lento:home/private:U#m_0142"
REC = {"system": "lento", "instance": "home", "container": "private:U", "id": "m_0142"}


def run(coro):
    return asyncio.run(coro)


@pytest.fixture
def reg(tmp_path):
    return S.SourceRegistry(tmp_path)


def change(cid, kind, seq, **kw):
    return {"change_id": cid, "source": SRC, "kind": kind, "host_seq": seq, **kw}


def apply(reg, *args, may_restore=False, **kw):
    return run(reg.apply_change(change(*args, **kw), may_restore=may_restore))


def test_a_source_never_heard_of_is_active(reg):
    assert reg.state_of(SRC) == "active"
    assert reg.describe(SRC) is None and reg.revisions_of(SRC) == []


@pytest.mark.parametrize("steps, final", [
    ([("unreadable", None)], "unreadable"),
    ([("unreadable", None), ("restored", None)], "active"),
    ([("unreadable", None), ("revised", {"revision": "2"})], "active"),
    ([("withdrawn", None), ("unreadable", None)], "withdrawn"),
    ([("deleted", None), ("unreadable", None)], "deleted"),
    ([("withdrawn", None), ("deleted", None)], "deleted"),
    ([("withdrawn", None), ("revised", {"revision": "2"})], "withdrawn"),
    ([("use_changed", {"use": "quote-only"})], "active"),
])
def test_the_state_table(reg, steps, final):
    for seq, (kind, extra) in enumerate(steps, start=1):
        out = apply(reg, f"c{seq}", kind, seq, **(extra or {}))
        assert out["outcome"] in ("applied", "unknown_source"), out
    assert reg.state_of(SRC) == final
    assert S.SourceRegistry(reg.base_dir).state_of(SRC) == final, "rebuilt from the file"


def test_unreadable_cannot_mask_withdrawn_and_says_so(reg):
    apply(reg, "c1", "withdrawn", 1)
    out = apply(reg, "c2", "unreadable", 2)
    assert out["state"] == "withdrawn" and out.get("note") == "unreadable_cannot_mask"


def test_restoring_withdrawn_needs_the_credential(reg):
    apply(reg, "c1", "withdrawn", 1)
    out = apply(reg, "c2", "restored", 2)
    assert out["outcome"] == "forbidden" and reg.state_of(SRC) == "withdrawn"
    # A forbidden change wrote nothing, so the same change with the credential goes in.
    out = apply(reg, "c2", "restored", 2, may_restore=True)
    assert out["outcome"] in ("applied", "unknown_source") and reg.state_of(SRC) == "active"


def test_restoring_deleted_returns_the_state_but_not_the_body(reg):
    apply(reg, "c1", "deleted", 1)
    out = apply(reg, "c2", "restored", 2, may_restore=True)
    assert out["state"] == "active" and out.get("note") == "redeliver"


def test_older_host_seq_is_stale_whatever_the_arrival_order(reg):
    apply(reg, "c5", "withdrawn", 5)
    out = apply(reg, "c3", "restored", 3, may_restore=True)
    assert out["outcome"] == "stale" and reg.state_of(SRC) == "withdrawn"
    assert out["applied_seq"] is None


def test_a_resend_is_a_duplicate_with_the_first_result(reg):
    first = apply(reg, "c1", "withdrawn", 1)
    again = apply(reg, "c1", "withdrawn", 1)
    assert again["outcome"] == "duplicate"
    assert again["applied_seq"] == first["applied_seq"]
    assert again["result"] == first["outcome"] and again["state"] == "withdrawn"
    assert len(reg.changes_path.read_text(encoding="utf-8").splitlines()) == 1


def test_a_reused_change_id_with_other_content_is_a_conflict(reg):
    apply(reg, "c1", "withdrawn", 1)
    out = apply(reg, "c1", "deleted", 1)
    assert out["outcome"] == "conflict" and out["conflict_with"]["kind"] == "withdrawn"
    assert reg.state_of(SRC) == "withdrawn"


def test_a_reused_host_seq_under_another_change_is_a_conflict(reg):
    apply(reg, "c1", "withdrawn", 4)
    out = apply(reg, "c9", "unreadable", 4)
    assert out["outcome"] == "conflict" and out["conflict_with"]["change_id"] == "c1"
    assert len(reg.changes_path.read_text(encoding="utf-8").splitlines()) == 1


def test_revisions_chain_by_host_seq_and_use_is_recorded(reg):
    apply(reg, "c1", "revised", 1, revision="2", fingerprint="sha256:b2")
    apply(reg, "c2", "use_changed", 2, use="quote-only")
    apply(reg, "c3", "revised", 3, revision="3")
    found = S.SourceRegistry(reg.base_dir).describe(SRC)
    assert [r["revision"] for r in found["revisions"]] == ["2", "3"]
    assert found["use"] == "quote-only" and found["host_seq"] == 3


def test_a_revised_change_must_say_what_it_revised_to(reg):
    with pytest.raises(ValueError):
        apply(reg, "c1", "revised", 1)


@pytest.mark.parametrize("bad", [
    {"change_id": "", "source": SRC, "kind": "withdrawn", "host_seq": 1},
    {"change_id": "c1", "source": SRC, "kind": "gone", "host_seq": 1},
    {"change_id": "c1", "source": SRC, "kind": "withdrawn", "host_seq": -1},
    {"change_id": "c1", "source": SRC, "kind": "withdrawn", "host_seq": True},
    {"change_id": "c1", "source": "m_0142", "kind": "withdrawn", "host_seq": 1},
])
def test_a_malformed_change_raises(reg, bad):
    with pytest.raises(ValueError):
        run(reg.apply_change(bad))


def test_the_source_may_come_as_fields_or_a_record(reg):
    run(reg.apply_change({"change_id": "c1", "kind": "withdrawn", "host_seq": 1, **REC}))
    run(reg.apply_change({"change_id": "c2", "kind": "deleted", "host_seq": 2,
                          "source": REC}))
    assert reg.state_of(REC) == "deleted"


def test_applied_seq_is_the_line_number_and_survives_a_restart(reg):
    seqs = [apply(reg, f"c{i}", "unreadable" if i % 2 else "restored", i)["applied_seq"]
            for i in range(1, 5)]
    assert seqs == [1, 2, 3, 4]
    rows = [json.loads(x) for x in reg.changes_path.read_text(encoding="utf-8").splitlines()]
    assert [r["seq"] for r in rows] == seqs
    other = {**change("d1", "withdrawn", 1), "source": "lento:home/private:U#m_9"}
    assert run(S.SourceRegistry(reg.base_dir).apply_change(other))["applied_seq"] == 5


def test_a_torn_last_line_is_skipped_and_the_next_starts_fresh(reg):
    apply(reg, "c1", "unreadable", 1)
    with reg.changes_path.open("a", encoding="utf-8") as f:
        f.write('{"seq": 2, "change_id": "half')
    fresh = S.SourceRegistry(reg.base_dir)
    out = run(fresh.apply_change(change("c2", "withdrawn", 2)))
    assert out["applied_seq"] == 2 and fresh.state_of(SRC) == "withdrawn"
    assert S.SourceRegistry(reg.base_dir).state_of(SRC) == "withdrawn"


def test_another_writer_appending_is_seen(reg):
    other = S.SourceRegistry(reg.base_dir)
    assert reg.state_of(SRC) == "active"
    apply(other, "c1", "withdrawn", 1)
    assert reg.state_of(SRC) == "withdrawn"


def test_unknown_source_is_recorded_and_applied_names_the_memories(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    out = run(store.sources.apply_change(change("c1", "unreadable", 1)))
    assert out["outcome"] == "unknown_source" and out["applied_seq"] == 1
    assert store.sources.state_of(SRC) == "unreadable", "the real state, recorded anyway"
    bid = run(store.create("A line formed from that message.", sources=[REC]))
    assert run(S.memories_of(store, SRC)) == [bid]
    out = run(store.sources.apply_change(change("c2", "withdrawn", 2)))
    assert out["outcome"] == "applied" and out["memories"] == [bid]


def test_the_write_check(reg):
    [rec] = S.normalize_sources(REC)
    other = {**rec, "id": "m_0143"}
    assert reg.check_writable([rec, other]) == ("", [])
    refusal, _ = reg.check_writable([rec, other], grant=[SRC])
    assert "m_0143" in refusal and "不在这一轮" in refusal
    assert reg.check_writable([rec], grant=[SRC + "@4"]) == ("", [])
    apply(reg, "c1", "unreadable", 1)
    refusal, notes = reg.check_writable([rec])
    assert refusal == "" and SRC in notes[0]
    apply(reg, "c2", "withdrawn", 2)
    refusal, _ = reg.check_writable([rec])
    assert SRC in refusal and "撤回" in refusal
    apply(reg, "c3", "deleted", 3)
    assert "删除" in reg.check_writable([rec])[0]


RUN = {**REC, "through": "m_0160"}
RUN_SRC = SRC + "..m_0160"


def test_a_run_is_as_bad_as_its_worst_line(reg):
    [run_rec] = S.normalize_sources(RUN)
    assert reg.describe(RUN_SRC) is None and reg.check_writable([run_rec]) == ("", [])
    apply(reg, "c1", "withdrawn", 1)                       # the first line, as the host says it
    assert reg.state_of(RUN_SRC) == "withdrawn"
    refusal, _ = reg.check_writable([run_rec])
    assert RUN_SRC in refusal and "撤回" in refusal
    # Restoring the run's own identity does not lift a line inside it.
    run(reg.apply_change({"change_id": "c2", "source": RUN_SRC, "kind": "restored",
                          "host_seq": 1}, may_restore=True))
    assert reg.state_of(RUN_SRC) == "withdrawn" and reg.state_of(SRC) == "withdrawn"
    # The last line reaches it too, even with the order never handed over.
    apply(reg, "c3", "restored", 2, may_restore=True)
    assert reg.state_of(RUN_SRC) == "active"
    run(reg.apply_change({"change_id": "c4", "source": "lento:home/private:U#m_0160",
                          "kind": "deleted", "host_seq": 1}))
    assert reg.state_of(RUN_SRC) == "deleted"


def test_a_line_inside_a_run_reaches_it_once_the_order_is_known(reg):
    [run_rec] = S.normalize_sources(RUN)
    inner = "lento:home/private:U#m_0150"
    run(reg.apply_change({"change_id": "c1", "source": inner, "kind": "withdrawn",
                          "host_seq": 1}))
    assert reg.state_of(RUN_SRC) == "active", "without the order, an inner line is unknown"
    reg.record_order({"system": "lento", "instance": "home", "container": "private:U"},
                     [f"m_{i:04d}" for i in range(140, 170)], batch_id="b1")
    assert reg.members_of(RUN_SRC)[0] == "m_0142" and reg.members_of(RUN_SRC)[-1] == "m_0160"
    assert reg.state_of(RUN_SRC) == "withdrawn"
    assert "撤回" in reg.check_writable([run_rec])[0]
    assert S.SourceRegistry(reg.base_dir).state_of(RUN_SRC) == "withdrawn", "kept on disk"
    # A run starting past the line is not touched.
    assert reg.state_of("lento:home/private:U#m_0151..m_0160") == "active"


def test_an_inner_lines_use_narrows_the_run(reg):
    [run_rec] = S.normalize_sources({**RUN, "use": {"venues": ["private", "group"]}})
    reg.record_order({"system": "lento", "instance": "home", "container": "private:U"},
                     [f"m_{i:04d}" for i in range(142, 161)])
    assert reg.uses_of(run_rec) == [{"venues": ["group", "private"]}]
    run(reg.apply_change({"change_id": "c1", "source": "lento:home/private:U#m_0155",
                          "kind": "use_changed", "host_seq": 1,
                          "use": {"venues": ["private"]}}))
    assert {"venues": ["private"]} in reg.uses_of(run_rec)


def test_two_hosts_own_their_change_ids_and_host_seqs(reg):
    first = run(reg.apply_change(change("c1", "unreadable", 5), host="a"))
    other = run(reg.apply_change(change("c1", "withdrawn", 1), host="b"))
    assert first["outcome"] in ("applied", "unknown_source")
    assert other["outcome"] in ("applied", "unknown_source"), "another host's id and seq"
    assert reg.state_of(SRC) == "withdrawn"
    again = run(reg.apply_change(change("c1", "withdrawn", 1), host="b"))
    assert again["outcome"] == "duplicate"
    stale = run(reg.apply_change(change("c2", "restored", 3), host="a", may_restore=True))
    assert stale["outcome"] == "stale", "host a's own order"
    assert S.SourceRegistry(reg.base_dir).state_of(SRC) == "withdrawn"


def test_a_run_is_granted_by_its_container_or_itself_never_by_its_first_line(reg):
    [run_rec] = S.normalize_sources(RUN)
    assert "不在这一轮" in reg.check_writable([run_rec], grant=[SRC])[0]
    assert reg.check_writable([run_rec], grant=[RUN_SRC]) == ("", [])
    whole = {"system": "lento", "instance": "home", "container": "private:U"}
    assert reg.check_writable([run_rec], grant=[whole]) == ("", [])
    refusal, _ = reg.check_writable([run_rec], grant=["lento:home/private:U#m_0160"])
    assert "不在这一轮" in refusal


def test_memories_of_a_line_finds_every_run_holding_it(tmp_path):
    store = BucketManager({"buckets_dir": str(tmp_path)})
    bid = run(store.create("A talk formed from twenty lines.", sources=[RUN]))
    assert run(S.memories_of(store, SRC)) == [bid]
    assert run(S.memories_of(store, RUN_SRC)) == [bid]
    assert run(S.memories_of(store, SRC + "..m_0150")) == []
    assert run(S.memories_of(store, "lento:home/private:U#m_0160")) == [bid], "its last line"
    assert run(S.memories_of(store, "lento:home/private:U#m_0150")) == [], "order unknown"
    store.sources.record_order({"system": "lento", "instance": "home", "container": "private:U"},
                               [f"m_{i:04d}" for i in range(142, 161)])
    assert run(S.memories_of(store, "lento:home/private:U#m_0150")) == [bid]
    assert run(S.memories_of(store, "lento:home/private:U#m_0170")) == []


def test_the_state_machine_alone():
    assert S.next_state("active", "restored", False) == ("active", "")
    assert S.next_state("withdrawn", "restored", False) == (None, "may_restore_required")
    assert S.next_state("deleted", "use_changed", False) == ("deleted", "")
