# -*- coding: utf-8 -*-
"""
The whole-turn runner (exam/turn.py) against a stub host that calls no model.

It pins what the runner itself owes every host: each event field reaches the host,
a window is opened once, and input checks read the request they are pointed at and
never pass on a partial read-back.
"""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from exam.host import Message, ModelCall, Turn  # noqa: E402
from exam.judge import ManualJudge  # noqa: E402
from exam.turn import _input_check, run_once  # noqa: E402


class StubHost:
    name = "stub"

    def __init__(self):
        self.opened, self.events = [], []

    def capabilities(self):
        return {"model_input", "audience", "withdraw", "withdraw_midturn", "sync"}

    def describe(self):
        return {"host": self.name}

    async def open(self, loci):
        pass

    async def open_window(self, window, at):
        self.opened.append(window.window_id)

    async def deliver(self, event):
        self.events.append(event)
        if event.kind != "say":
            return []
        first = ModelCall([Message("user", event.text)], complete=True)
        second = ModelCall([Message("user", event.text),
                            Message("tool", "old background")], complete=True)
        return [Turn(event.window_id, model_calls=[first, second], reply="ok")]

    async def close(self):
        pass


ITEM = {
    "id": "T1",
    "start": "2026-10-01T20:00:00+08:00",
    "setup": [],
    "windows": {"A": {"entry": "private:U"}, "B": {"entry": "group:X"}},
    "events": [
        {"at": "2026-10-01T20:00:00+08:00", "kind": "say", "window": "A", "text": "hi"},
        {"at": "2026-10-01T20:01:00+08:00", "kind": "audience", "window": "A",
         "audience": ["U", "V"], "grant": ["group:X"]},
        {"at": "2026-10-01T20:02:00+08:00", "kind": "say", "window": "B", "text": "yo"},
        {"at": "2026-10-01T20:03:00+08:00", "kind": "sync", "window": "B",
         "source_window": "A", "fail": True},
        {"at": "2026-10-01T20:04:00+08:00", "kind": "withdraw", "window": "A",
         "ref": "chat:1", "midturn": True},
        {"at": "2026-10-01T20:05:00+08:00", "kind": "say", "window": "A", "text": "back"},
    ],
    "checks": [
        {"segment": "input", "desc": "first request lacks the background",
         "input_lacks": "old background", "call": 1},
        {"segment": "input", "desc": "some request has it", "input_contains": "old background"},
        {"segment": "use", "desc": "graded", "judge": "anything", "turn": 2},
    ],
}


def _run():
    host = StubHost()
    results, transcript, causes = asyncio.run(run_once(ITEM, host, ManualJudge(), False, 1))
    return host, results, causes


def test_every_event_field_reaches_the_host():
    host, _, _ = _run()
    by_kind = {e.kind: e for e in host.events}
    assert by_kind["audience"].audience == ["U", "V"]
    assert by_kind["audience"].grant == ["group:X"]
    assert by_kind["sync"].source_window == "A" and by_kind["sync"].fail is True
    assert by_kind["withdraw"].ref == "chat:1" and by_kind["withdraw"].midturn is True


def test_a_window_is_opened_once():
    host, _, _ = _run()
    assert host.opened == ["A", "B"]
    assert [e.window_id for e in host.events if e.kind == "say"] == ["A", "B", "A"]


def test_checks_read_the_turn_they_name_and_its_cause():
    _, results, causes = _run()
    assert causes == ["hi", "yo", "back"]
    assert results[0].ok is True        # request #1 does not have it
    assert results[1].ok is True        # request #2 does


def _turn(*calls):
    return Turn("A", model_calls=list(calls))


def test_call_picks_one_request():
    first = ModelCall([Message("user", "q")], complete=True)
    second = ModelCall([Message("tool", "P")], complete=True)
    assert _input_check({"input_contains": "P", "call": 1}, _turn(first, second)).ok is False
    assert _input_check({"input_contains": "P", "call": -1}, _turn(first, second)).ok is True
    assert _input_check({"input_contains": "P", "call": 3}, _turn(first, second)).ok is False


def test_partial_read_back_never_passes_but_a_seen_leak_fails():
    partial = ModelCall([Message("user", "P")], complete=False)
    assert _input_check({"input_contains": "P"}, _turn(partial)).ok is None
    assert _input_check({"input_contains": "Q"}, _turn(partial)).ok is None
    assert _input_check({"input_lacks": "P"}, _turn(partial)).ok is False
    assert _input_check({"input_lacks": "Q"}, _turn(partial)).ok is None


def test_entries_are_found_by_what_they_are():
    from exam.runner import Run, library

    item = {"id": "T2", "start": "2026-10-01T20:00:00+08:00", "setup": [
        {"id": "e10001000000", "at": "2026-09-01T10:00:00+08:00", "room": "EVENT/WORLD",
         "text": "she said her sister mailed a box of snacks"},
        {"id": "e20001000000", "at": "2026-09-02T10:00:00+08:00", "room": "MIND/TRAITS",
         "text": "the sister: sharp tongue, soft heart", "subjects": ["sister"],
         "from": ["e10001000000"], "fields": {"status": "want"}},
    ]}

    async def go():
        async with library(item, keep=False) as L:
            run = Run(L.lib, L.clock_file, session=None)
            run.setup_ids = {"e10001000000"}
            j = run._judge
            assert j({"entry": {"room": "MIND", "subjects_has": "sis", "from_room": "EVENT"}})[0]
            assert j({"entry": {"room": "MIND", "new": True, "from_has": ["e10001000000"]}})[0]
            assert not j({"entry": {"room": "EVENT", "new": True}})[0]
            assert j({"no_entry": {"room": "MIND/VIEWS"}})[0]
            assert not j({"no_entry": {"body_any": ["snacks", "nothing"]}})[0]
            assert j({"entry": {"body_has": "snacks", "fields": {"when": ""}}})[0]
            assert j({"entry": {"fields": {"status": "want"}}, "min": 0, "max": 1})[0]
            assert j({"any_of": [{"no_entry": {"room": "EVENT"}},
                                 {"all_of": [{"entry": {"room": "EVENT"}},
                                             {"entry": {"room": "MIND"}}]}]})[0]

    asyncio.run(go())
