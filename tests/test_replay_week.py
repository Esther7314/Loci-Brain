# -*- coding: utf-8 -*-
"""
tests/test_replay_week.py — the week's chat fixture replays, and the replay's stand-in
keeps the real key to itself

scripts/replay_week.py pushes scripts/fixtures/week_chat.json through a dev gateway. What
is checked here needs no gateway: the fixture passes the replay's own checks and keeps to
the sample library's people, the plan has a night after every day and the away stretch,
and the stand-in answers a scripted turn itself while everything else goes upstream with
the real key and model and none of the caller's credential — or, as the stub, is answered
in the shape its caller reads; a request whose caller left while it waited for a slot is
never answered.
"""

import json
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
try:
    import replay_week as R
finally:
    sys.path.remove(str(ROOT / "scripts"))


@pytest.fixture(scope="module")
def fx():
    return R.load_fixture()


def test_the_fixture_replays(fx):
    assert R.problems(fx) == []
    assert (fx["owner"], fx["ai"]) == ("阿青", "小满")
    turns = R.turns_of(fx)
    assert len(fx["days"]) == 7 and len(turns) > 60
    # a late night: a line of one day said after midnight
    assert any(t.user_at.date() != R.date.fromisoformat(fx["days"][t.day]["date"]) for t in turns)


def test_the_sample_names_are_the_libraries(fx):
    """The chat calls the two the way the seeded name page says they call each other."""
    page = (ROOT / "scripts" / "dev_panel.py").read_text(encoding="utf-8")
    assert "我叫小满。对方叫阿青，我叫阿青「青青」，阿青叫我「满满」。" in page
    said = {"user": [], "assistant": []}
    for d in fx["days"]:
        for m in d["messages"]:
            said[m["role"]].append(m["content"])
    assert any("青青" in t for t in said["assistant"]) and any("满满" in t for t in said["user"])


def test_the_checks_bite(fx):
    broken = json.loads(json.dumps(fx))
    msgs = broken["days"][0]["messages"]
    msgs[1]["role"] = "user"
    msgs[3]["at"] = msgs[2]["at"]
    said = " ".join(R.problems(broken))
    assert "two user messages in a row" in said and "is not after" in said


def test_the_plan_closes_every_day(fx):
    plan = R.build_plan(fx, clock=True)
    nights = [s for s in plan.steps if s.kind == "night"]
    assert [s.day.isoformat() for s in nights] == [d["date"] for d in fx["days"]]
    assert all(s.at.hour == 4 and s.at.minute == 35 for s in nights)
    assert [s.kind for s in plan.steps].count("away") == len(fx["away"])
    stamps = [s.at for s in plan.steps]
    assert stamps == sorted(stamps)
    flat = R.build_plan(fx, clock=False)
    assert [s.day.isoformat() for s in flat.steps if s.kind == "report_now"] == [d["date"] for d in fx["days"]]
    assert not any(s.kind in ("night", "away") for s in flat.steps)


def test_a_shifted_plan_moves_whole_days(fx):
    plan = R.build_plan(fx, clock=True, shift=timedelta(days=7))
    first = next(s for s in plan.steps if s.kind == "turn")
    assert first.turn.user_at == R.turns_of(fx)[0].user_at + timedelta(days=7)


class _Upstream(BaseHTTPRequestHandler):
    seen = []

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        _Upstream.seen.append({"headers": dict(self.headers), "body": body, "path": self.path})
        data = json.dumps({"choices": [{"index": 0, "message": {"role": "assistant", "content": "上游"}}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def _post(port, headers, body):
    req = urllib.request.Request(f"http://127.0.0.1:{port}/v1/chat/completions", method="POST",
                                 data=json.dumps(body).encode(), headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read())


def test_the_stand_in_answers_scripted_turns_and_swaps_the_key(fx):
    up = ThreadingHTTPServer(("127.0.0.1", 0), _Upstream)
    threading.Thread(target=up.serve_forever, daemon=True).start()
    lines = []
    stand_in = R.StandIn(R.turns_of(fx), replies="scripted", key="sk-real-secret", model="cheap-1",
                         upstream=f"http://127.0.0.1:{up.server_address[1]}/v1", listen=0,
                         log=lines.append).start()
    try:
        _Upstream.seen.clear()
        got = _post(stand_in.port, {"Authorization": f"Bearer {R.PLACEHOLDER_KEY}", R.TURN_HEADER: "0"},
                    {"model": "x", "messages": [{"role": "user", "content": "满满早"}]})
        assert got["choices"][0]["message"]["content"] == R.turns_of(fx)[0].reply
        assert _Upstream.seen == []

        got = _post(stand_in.port, {"Authorization": f"Bearer {R.PLACEHOLDER_KEY}", "x-api-key": "client"},
                    {"model": "stand-in", "messages": [{"role": "user", "content": "日报"}]})
        assert got["choices"][0]["message"]["content"] == "上游"
        (seen,) = _Upstream.seen
        assert seen["path"] == "/v1/chat/completions"
        assert seen["body"]["model"] == "cheap-1"
        heads = {k.lower(): v for k, v in seen["headers"].items()}
        assert heads["authorization"] == "Bearer sk-real-secret"
        assert "x-api-key" not in heads
        assert not any("sk-real-secret" in line for line in lines)
    finally:
        stand_in.stop()
        up.shutdown()
        up.server_close()


def test_without_a_key_nothing_is_sent(fx):
    stand_in = R.StandIn(R.turns_of(fx), replies="scripted", key="", model="m",
                         upstream="http://127.0.0.1:9/v1", listen=0, log=lambda _l: None).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            _post(stand_in.port, {}, {"model": "m", "messages": [{"role": "user", "content": "x"}]})
        assert e.value.code == 503
        assert stand_in.counts["refused"] == 1 and stand_in.counts["forwarded"] == 0
    finally:
        stand_in.stop()


def _side(system, user):
    return {"model": "stand-in", "messages": [{"role": "system", "content": system},
                                              {"role": "user", "content": user}]}


def test_the_stub_answers_each_side_call_in_the_shape_its_caller_reads():
    from datetime import date
    from core import _dream, _slicer
    from core import dehydrator as DH
    from core.import_memory import IMPORT_DRAFT_PROMPT

    user = "下面是 3 行聊天原文，行号在方括号里：\n[1] 早\n[2] 早啊\n[3] 吃了吗"
    assert _slicer.parse_slices(R.stub_answer(_side(_slicer.SLICER_PROMPT, user)), 3) == [(1, 3, "一段日常聊天")]
    drafted = json.loads(R.stub_answer(_side(IMPORT_DRAFT_PROMPT, user)))
    assert drafted["slices"][0]["draft"]

    system, ask = DH.backfill_request("阿青说牙又疼了。\n约了周六。",
                                      {"room": "EVENT/SELF", "created_day": date(2026, 10, 14)})
    answer = DH.parse_backfill(R.stub_answer(_side(system, ask)), "阿青说牙又疼了。")
    assert answer is not None and answer.summary == "阿青说牙又疼了。"

    dream = _dream.parse_dream(R.stub_answer(_side(_dream.DREAM_PROMPT, "素材")))
    assert dream["完整"] and dream["碎片"]
    assert json.loads(R.stub_answer(_side(DH.CUT_PROMPT, "x"))) == {"cuts": []}


def test_the_stubs_own_turn_grows_once_then_talks():
    tools = [{"type": "function", "function": {"name": "loci__grow"}},
             {"type": "function", "function": {"name": "loci__recall"}}]
    first = {"messages": [{"role": "user", "content": "【写日报】"}], "tools": tools}
    call = R._stub_grow_call(first)
    assert call["function"]["name"] == "loci__grow"
    assert json.loads(call["function"]["arguments"])["kind"] == "event"
    after = {"messages": first["messages"] + [{"role": "assistant", "tool_calls": [call]},
                                              {"role": "tool", "content": "ok"}], "tools": tools}
    assert R._stub_grow_call(after) is None and R.stub_answer(after)
    pack = {"messages": [{"role": "user", "content": "收进【窗口摘要】…【/窗口摘要】"}],
            "tools": tools[1:]}
    assert R._stub_grow_call(pack) is None
    said = R.stub_answer(pack)
    assert "【窗口摘要】" in said and "【/窗口摘要】" in said


def test_a_stub_stand_in_answers_without_a_key_and_counts(fx):
    stand_in = R.StandIn(R.turns_of(fx), replies="scripted", key="", model="", upstream="",
                         stub=True, listen=0, log=lambda _l: None).start()
    try:
        got = _post(stand_in.port, {}, _side("你是记忆系统的回填器。", "【正文】\n阿青考完了。"))
        assert json.loads(got["choices"][0]["message"]["content"])["summary"] == "阿青考完了。"
        assert stand_in.counts["stubbed"] == 1 and stand_in.counts["refused"] == 0
        assert stand_in.kinds == {R.request_kind(_side("你是记忆系统的回填器。", "")): 1}
    finally:
        stand_in.stop()


def test_a_request_whose_caller_left_while_queued_is_not_answered(fx):
    """One slot, a slow answer in it: a second caller that gives up while it waits for the
    slot is dropped — upstream it would still be paid for, and nobody would read it."""
    stand_in = R.StandIn(R.turns_of(fx), replies="scripted", key="", model="", upstream="",
                         stub=True, stub_delay=1.0, concurrency=1, listen=0,
                         log=lambda _l: None).start()

    def ask(timeout):
        req = urllib.request.Request(f"http://127.0.0.1:{stand_in.port}/v1/chat/completions",
                                     method="POST", data=json.dumps(_side("x", "y")).encode(),
                                     headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.status
        except OSError:
            return None

    try:
        first = threading.Thread(target=ask, args=(10,))
        first.start()
        time.sleep(0.2)
        assert ask(0.3) is None
        first.join()
        time.sleep(0.3)
        assert stand_in.counts["stubbed"] == 1 and stand_in.counts["abandoned"] == 1
    finally:
        stand_in.stop()
