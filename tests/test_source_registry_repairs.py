# -*- coding: utf-8 -*-
"""
tests/test_source_registry_repairs.py — what reaches a run reaches its lines, both ways.

A `use_changed` on a run narrows its lines and the runs overlapping them, as a withdrawal
does; a hold on a run holds its lines; registering lines is one turn at a time, so two
registrations racing never both land with contradicting revisions; a write key past the
length limit keeps telling two writes apart.
"""

import asyncio
import threading
import time

from core import _sources as S

WHERE = {"system": "telegram", "instance": "bot-a", "container": "group:G"}
RUN = "telegram:bot-a/group:G#m_1..m_4"


def run(coro):
    return asyncio.run(coro)


def _rec(id_, through=None, **kw):
    r = {**WHERE, "id": id_, **kw}
    if through:
        r["through"] = through
    return r


def test_a_use_changed_on_a_run_narrows_its_lines_and_overlapping_runs(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    reg.record_order(WHERE, ["m_1", "m_2", "m_3", "m_4", "m_5"])
    narrow = {"venues": ["private"]}
    run(reg.apply_change({"change_id": "u", "source": RUN, "kind": "use_changed",
                          "host_seq": 1, "use": narrow}))
    assert narrow in reg.uses_of(_rec("m_2")), "a line inside the run"
    assert narrow in reg.uses_of(_rec("m_3", "m_5")), "a run overlapping it"
    assert reg.uses_of(_rec("m_5")) == [], "a line outside it"


def test_a_hold_on_a_run_holds_its_lines(tmp_path):
    reg = S.SourceRegistry(tmp_path)
    reg.record_order(WHERE, ["m_1", "m_2", "m_3", "m_4", "m_5"])
    reg.hold(RUN, "withdrawn", "bot")
    assert reg.state_of(RUN) == S.HELD
    assert reg.state_of("telegram:bot-a/group:G#m_2") == S.HELD
    assert reg.state_of("telegram:bot-a/group:G#m_3..m_5") == S.HELD
    assert reg.state_of("telegram:bot-a/group:G#m_5") == S.ACTIVE
    # Settled by the ordered change for the run: the lines are free again.
    run(reg.apply_change({"change_id": "r", "source": RUN, "kind": "restored",
                          "host_seq": 1}))
    assert reg.state_of("telegram:bot-a/group:G#m_2") == S.ACTIVE


def test_two_registrations_racing_never_both_land(tmp_path, monkeypatch):
    reg = S.SourceRegistry(tmp_path)
    real_now = S._now

    def slow_now():
        time.sleep(0.3)
        return real_now()
    monkeypatch.setattr(S, "_now", slow_now)
    got = []

    def register(rev):
        mine = S.SourceRegistry(tmp_path)           # another process's registry
        got.append(mine.record_order(WHERE, ["m_1", "m_2"], revision="w-1",
                                     revisions={"m_1": rev, "m_2": None}))
    threads = [threading.Thread(target=register, args=(r,)) for r in ("e1", "e2")]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(g == S.RECORDED for g in got) == [False, True], got
    rows = S._read_lines(reg.orders_path)
    assert len(rows) == 1


def test_a_write_key_past_the_limit_keeps_two_writes_apart(tmp_path):
    host, turn = "h" * 64, "t" * 200
    keys = []
    for ordinal in (1, 2):
        with S.write_key_scope(S.write_key(turn, ordinal, host)):
            keys.append(S.current_write_key())
    assert keys[0] != keys[1] and all(len(k) <= S.WRITE_KEY_MAX for k in keys)
    reg = S.SourceRegistry(tmp_path)
    replies = []
    for ordinal in (1, 2):
        key = S.write_key(turn, ordinal, host)

        async def do(n=ordinal):
            S.note_written(f"id{n}")
            return f"wrote {n}"
        replies.append(run(reg.run_once(key, do)))
    assert replies == ["wrote 1", "wrote 2"]
