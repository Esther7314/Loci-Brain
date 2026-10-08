# -*- coding: utf-8 -*-
"""
tests/test_slices.py — attaching sources after the fact: a day's raw lines, sliced.

The side model is always a stub here: it answers with the slicing the test wants (or
fails the way the test wants), so nothing leaves the machine. Through take_batch and the
tools' dispatch against a real BucketManager, read back from the files: a day's lines
become slices; one is written as a new memory, one appended to a memory already there,
one dropped; each slice that was kept is one source record, first..last, and nothing is
pending. Then the edges: a resend replaces, a failing side model writes nothing, spans
are repaired or refused, a re-cut moves the record and recomputes its fingerprint, a
closed slice is refused by name, and
the two HTTP routes answer through the real handlers and the hook lock.
"""

import asyncio
import hashlib
import json

import frontmatter
import pytest

import tools.grow as grow_mod
from core import _slicer as SL
from core import _sources as S
from core import _when as W
from core.bucket_manager import BucketManager
from core import runtime as rt
from tools.grow import dispatch as grow
from tools.grow import rooms_path
from tools.recall import core as R
from tools.trace import dispatch as trace

SOURCE = {"system": "lento", "instance": "home", "container": "private:U"}
TEXTS = [
    "Morning! Slept well?", "Yes, finally.", "Good.", "ok",
    "Want to go to the beach on Saturday?", "Only if it is warm.",
    "The forecast says sunny.", "Then yes, let us go early.",
    "The kettle broke again.", "Buy a new one?", "Tomorrow.", "Fine.",
]
IDS = [f"m_{i:04d}" for i in range(1, len(TEXTS) + 1)]


def run(coro):
    return asyncio.run(coro)


class _Engine:
    async def ensure_started(self):
        pass


class _Log:
    def _n(self, *a, **k):
        pass
    warning = info = debug = error = _n


class _Vectors:
    """A stand-in embedding engine: every memory in `among` scores by shared words."""
    enabled = True

    def __init__(self, store):
        self.store = store

    async def search_similar(self, query, top_k=10, among=None):
        out = []
        for bid in among or []:
            b = await self.store.get(bid)
            words = set(b["content"].lower().split())
            hit = len(words & set(query.lower().split())) / max(1, len(query.split()))
            out.append((bid, hit))
        return sorted(out, key=lambda p: -p[1])[:top_k]


@pytest.fixture
def store(tmp_path, monkeypatch):
    mgr = BucketManager({"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(rt, "bucket_mgr", mgr)
    monkeypatch.setattr(rt, "decay_engine", _Engine())
    monkeypatch.setattr(rt, "logger", _Log())
    monkeypatch.setattr(rt, "config", {"buckets_dir": str(tmp_path)})
    monkeypatch.setattr(grow_mod, "_sweep_started", True)

    async def no_backfill(pairs):
        pass
    monkeypatch.setattr(rooms_path, "_backfill_batch", no_backfill)
    return mgr


def stub(*answers, calls=None):
    """A side model answering each call with the next answer (an exception is raised)."""
    queue = list(answers)

    async def model(system, user):
        if calls is not None:
            calls.append(user)
        answer = queue.pop(0) if len(queue) > 1 else queue[0]
        if isinstance(answer, Exception):
            raise answer
        return answer if isinstance(answer, str) else json.dumps({"slices": answer})
    model.model_name = "stub"
    return model


THREE = [{"from": 1, "to": 4, "gist": "Morning greeting, slept well"},
         {"from": 5, "to": 8, "gist": "beach on Saturday if sunny"},
         {"from": 9, "to": 12, "gist": "The kettle broke"}]


def body(day=None, texts=TEXTS, ids=IDS, **extra):
    return {"source": dict(SOURCE), "day": day or W.today().date().isoformat(),
            "lines": [{"id": i, "text": t, "speaker": "A" if n % 2 else "B"}
                      for n, (i, t) in enumerate(zip(ids, texts))], **extra}


def take(store, model, **kw):
    return run(SL.take_batch(store, kw.pop("b", None) or body(), model=model, **kw))


def _disk(tmp_path, bid) -> dict:
    [path] = [p for p in tmp_path.rglob(f"*{bid}*.md")]
    return dict(frontmatter.load(path).metadata)


def _runs(meta) -> list[tuple]:
    """Each stored record as (first line, last line or None)."""
    return [(r["id"], r.get("through")) for r in meta.get("sources") or []]


def _fp(text) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


async def _event(text, **kw):
    out = await grow(kind="event", items=[{"room": "EVENT/WORLD", "text": text,
                                           "v": 0.6, "a": 0.3}], **kw)
    return out, (out.split("📝", 1)[1].split()[0] if "📝" in out else "")


# ───────────────────────── the story ─────────────────────────

def test_a_day_sliced_then_written_appended_and_dropped(store, tmp_path, monkeypatch):
    _out, beach = run(_event("We planned the beach on Saturday if it is sunny."))
    monkeypatch.setattr(store, "embedding_engine", _Vectors(store))
    got = take(store, stub(THREE))
    monkeypatch.setattr(store, "embedding_engine", None)
    a, b, c = got["slices"]
    assert [s["span"] for s in got["slices"]] == [
        {"first": "m_0001", "last": "m_0004", "count": 4},
        {"first": "m_0005", "last": "m_0008", "count": 4},
        {"first": "m_0009", "last": "m_0012", "count": 4}]
    assert b["guesses"][0]["id"] == beach and b["guesses"][0]["short"] == beach[:6]
    assert got["unsliced"] == 0 and store.slices.pending_count() == 3
    shown = run(R.recall_core(when="", room="", tag="", query="", view="slices"))
    assert a["slice_id"] in shown and "m_0005..m_0008（4 行）" in shown and beach[:6] in shown

    # Not recorded yet: written with grow, the slice's lines become its sources.
    out, new = run(_event("Slept well for once.", slice_id=a["slice_id"]))
    assert new and "挂上了" in out, out
    meta = _disk(tmp_path, new)
    assert _runs(meta) == [("m_0001", "m_0004")], "one record per slice"
    [rec] = meta["sources"]
    assert rec["fingerprint"] == SL.slice_fingerprint([_fp(t) for t in TEXTS[0:4]])
    assert rec["fingerprint"] == "sha256:" + hashlib.sha256(
        "\n".join(_fp(t) for t in TEXTS[0:4]).encode("utf-8")).hexdigest()
    assert rec["fingerprint_by"] == "loci"
    assert "lento:home/private:U#m_0001..m_0004" in [ln["target"] for ln in meta["prov"]]
    shown = run(R.recall_core(when="", room="", tag="", query=new))
    assert "lento:home/private:U#m_0001..m_0004（4 行）" in shown.split("来源:", 1)[1]

    # Already recorded: the lines are appended to that memory.
    out = run(trace(bucket_id=beach[:6], slice_id=b["slice_id"]))
    assert out.startswith("已修改记忆桶") and "挂上了" in out, out
    assert _runs(_disk(tmp_path, beach)) == [("m_0005", "m_0008")]

    # Sliced wrong / nothing to keep: dropped.
    out = run(trace(slice_id=c["slice_id"], drop_slice=True))
    assert "丢掉了" in out, out

    assert store.slices.pending_count() == 0
    assert run(R.recall_core(when="", room="", tag="", query="",
                             view="slices")) == "没有待认领的切片。"
    on_disk = store.slices.path.read_text(encoding="utf-8")
    assert not any(t in on_disk for t in TEXTS if len(t) > 4), "raw text was kept"


def test_guesses_only_look_at_the_day_and_the_line(store, monkeypatch):
    run(_event("We planned the beach on Saturday if it is sunny."))
    monkeypatch.setattr(store, "embedding_engine", _Vectors(store))
    yesterday = (W.today().date().fromordinal(W.today().date().toordinal() - 1)).isoformat()
    got = take(store, stub(THREE), b=body(day=yesterday))
    assert all(s["guesses"] == [] for s in got["slices"])
    got = take(store, stub(THREE), threshold=0.99)
    assert all(s["guesses"] == [] for s in got["slices"])


def test_append_to_an_existing_memory_keeps_what_it_had(store, tmp_path):
    rec = {**SOURCE, "id": "m_0900"}
    _out, bid = run(_event("Something said last week.", sources=[rec]))
    sid = take(store, stub(THREE))["slices"][1]["slice_id"]
    run(trace(bucket_id=bid, slice_id=sid))
    assert _runs(_disk(tmp_path, bid)) == [("m_0900", None), ("m_0005", "m_0008")]


# ───────────────────────── resend, failure ─────────────────────────

def test_the_same_batch_resent_replaces_its_pending_slices(store):
    first = take(store, stub(THREE))
    again = take(store, stub(THREE[:2]))
    assert again["batch_id"] == first["batch_id"] and again["replaced"] == 3
    assert store.slices.pending_count() == 2
    old = first["slices"][0]["slice_id"]
    out = run(trace(slice_id=old, drop_slice=True))
    assert "换掉了" in out and store.slices.pending_count() == 2


def test_a_handled_slice_stays_handled_across_a_resend(store, tmp_path):
    first = take(store, stub(THREE))
    sid = first["slices"][0]["slice_id"]
    _out, bid = run(_event("Slept well for once.", slice_id=sid))
    again = take(store, stub(THREE))
    assert again["replaced"] == 2
    out, _ = run(_event("Slept well, again.", slice_id=sid))
    assert f"写成了 {bid}" in out and "本次什么都没写" in out


@pytest.mark.parametrize("answer", [
    RuntimeError("connection reset"), "", "not json at all",
    [{"from": 1, "to": 40, "gist": "past the end"}],
    [{"from": 3, "to": 2, "gist": "backwards"}],
    [{"from": 1, "to": 4}],
    {"cuts": []},
])
def test_a_failing_side_model_fails_the_batch_and_writes_nothing(store, answer):
    model = stub(answer if not isinstance(answer, dict) else json.dumps(answer))
    with pytest.raises(SL.SlicerError):
        take(store, model)
    assert not store.slices.path.exists() and store.slices.pending_count() == 0


def test_a_malformed_batch_is_refused_before_the_side_model(store):
    calls = []
    model = stub(THREE, calls=calls)
    bad = [
        {**body(), "extra": 1},
        body(day="2026-02-30"),
        body(ids=["m_1", "m_1"], texts=["a", "b"]),
        body(ids=["m 1"], texts=["a"]),
        {**body(), "lines": [{"id": "m_1", "text": "a", "role": "user"}]},
        {**body(), "source": {"system": "lento"}},
        {**body(), "lines": []},
    ]
    for b in bad:
        with pytest.raises(SL.BatchError):
            take(store, model, b=b)
    with pytest.raises(SL.BatchError):
        take(store, model, max_lines=5)
    assert calls == []


# ───────────────────────── what the side model may get wrong ─────────────────────────

def test_spans_are_repaired_where_it_is_safe():
    raw = json.dumps({"slices": [
        {"from": 5, "to": 9, "gist": "b"}, {"from": 1, "to": 6, "gist": "a"},
        {"from": 2, "to": 3, "gist": "inside a"}, {"from": "11", "to": "12", "gist": "c"}]})
    assert SL.parse_slices(raw, 12) == [(1, 6, "a"), (7, 9, "b"), (11, 12, "c")]


def test_lines_in_no_slice_are_counted_and_gists_are_cut(store):
    long = "x" * 200
    got = take(store, stub([{"from": 2, "to": 3, "gist": long}]))
    [s] = got["slices"]
    assert got["unsliced"] == 10 and len(s["gist"]) == SL.GIST_MAX


def test_a_slice_of_a_hundred_lines_is_one_record(store, tmp_path):
    texts = [f"line {i}" for i in range(100)]
    ids = [f"m_{i:04d}" for i in range(100)]
    got = take(store, stub([{"from": 1, "to": 100, "gist": "one long talk"}]),
               b=body(texts=texts, ids=ids))
    [s] = got["slices"]
    assert s["span"] == {"first": "m_0000", "last": "m_0099", "count": 100}
    assert s["gist"] == "one long talk"
    _out, bid = run(_event("One long talk about everything.", slice_id=s["slice_id"]))
    meta = _disk(tmp_path, bid)
    assert _runs(meta) == [("m_0000", "m_0099")]
    assert meta["sources"][0]["fingerprint"] == SL.slice_fingerprint([_fp(t) for t in texts])


def test_a_big_batch_is_sliced_in_chunks(store, monkeypatch):
    monkeypatch.setattr(SL, "_CALL_MAX_LINES", 5)
    calls = []
    got = take(store, stub([{"from": 1, "to": 2, "gist": "start of a chunk"}], calls=calls))
    assert len(calls) == 3
    assert [s["span"]["first"] for s in got["slices"]] == ["m_0001", "m_0006", "m_0011"]


# ───────────────────────── handling ─────────────────────────

def test_a_recut_moves_the_record_recomputes_its_fingerprint_keeps_the_gist(store, tmp_path):
    s = take(store, stub(THREE))["slices"][1]
    before = store.slices.record_for(s["slice_id"])
    assert before["fingerprint"] == SL.slice_fingerprint([_fp(t) for t in TEXTS[4:8]])
    out = run(trace(slice_id=s["slice_id"], slice_span="m_0006..m_0007"))
    assert "m_0006..m_0007（2 行）" in out and s["gist"] in out, out
    shown = run(R.recall_core(when="", room="", tag="", query="", view="slices"))
    assert "改切过" in shown
    _out, bid = run(_event("Going to the beach early.", slice_id=s["slice_id"]))
    [rec] = _disk(tmp_path, bid)["sources"]
    assert (rec["id"], rec["through"]) == ("m_0006", "m_0007")
    assert rec["fingerprint"] == SL.slice_fingerprint([_fp(t) for t in TEXTS[5:7]])
    assert rec["fingerprint"] != before["fingerprint"]


@pytest.mark.parametrize("span, word", [
    ("m_0006..m_0999", "不在这片的那一批里"), ("m_0007..m_0006", "后面"), ("..", "前id..后id")])
def test_a_recut_outside_the_batch_or_backwards_is_refused(store, span, word):
    sid = take(store, stub(THREE))["slices"][1]["slice_id"]
    out = run(trace(slice_id=sid, slice_span=span))
    assert word in out and "什么都没改" in out, out
    assert store.slices.get(sid)["span"]["first"] == "m_0005"


def test_a_recut_of_any_length_is_taken_and_a_one_line_one_has_no_through(store):
    texts = [f"line {i}" for i in range(100)]
    ids = [f"m_{i:04d}" for i in range(100)]
    sid = take(store, stub([{"from": 1, "to": 3, "gist": "short"}]),
               b=body(texts=texts, ids=ids))["slices"][0]["slice_id"]
    out = run(trace(slice_id=sid, slice_span="m_0000..m_0080"))
    assert "m_0000..m_0080（81 行）" in out, out
    assert store.slices.record_for(sid)["through"] == "m_0080"
    run(trace(slice_id=sid, slice_span="m_0007"))
    rec = store.slices.record_for(sid)
    assert rec["id"] == "m_0007" and "through" not in rec
    assert rec["fingerprint"] == SL.slice_fingerprint([_fp("line 7")])


def test_a_closed_slice_is_refused_naming_what_handled_it(store, tmp_path):
    a, b, c = take(store, stub(THREE))["slices"]
    _out, bid = run(_event("Slept well for once.", slice_id=a["slice_id"]))
    out = run(trace(bucket_id=bid, slice_id=a["slice_id"]))
    assert f"写成了 {bid}" in out and "不能再用" in out
    run(trace(slice_id=c["slice_id"], drop_slice=True))
    out = run(trace(slice_id=c["slice_id"], slice_span="m_0009..m_0010"))
    assert "丢掉了" in out and "不能再用" in out
    assert "没有这片切片" in run(trace(slice_id="sl_nothing", drop_slice=True))


def test_a_refused_write_leaves_the_slice_open(store, tmp_path):
    sid = take(store, stub(THREE))["slices"][0]["slice_id"]
    out = run(grow(kind="event", items=[{"room": "NOWHERE", "text": "x", "v": 0.5, "a": 0.3}],
                   slice_id=sid))
    assert "挂上了" not in out
    assert store.slices.get(sid)["state"] == SL.OPEN and store.slices.pending_count() == 3


def test_a_withdrawn_first_line_refuses_the_write_and_the_slice_waits(store, tmp_path):
    sid = take(store, stub(THREE))["slices"][0]["slice_id"]
    run(store.sources.apply_change({"change_id": "w1", "kind": "withdrawn", "host_seq": 1,
                                    "source": "lento:home/private:U#m_0001"}))
    out, bid = run(_event("Slept well for once.", slice_id=sid))
    assert not bid and "撤回" in out
    assert store.slices.get(sid)["state"] == SL.OPEN


def test_slice_moves_that_do_not_fit_together_are_refused(store, tmp_path):
    _out, bid = run(_event("Something."))
    sid = take(store, stub(THREE))["slices"][0]["slice_id"]
    assert "单独用" in run(trace(bucket_id=bid, slice_id=sid, drop_slice=True))
    assert "单独用" in run(trace(slice_id=sid, drop_slice=True, name="renamed"))
    assert "二选一" in run(trace(slice_id=sid, drop_slice=True, slice_span="m_0001"))
    assert "要配 bucket_id" in run(trace(slice_id=sid))
    assert "一起用" in run(trace(bucket_id=bid, drop_slice=True))
    assert store.slices.pending_count() == 3


def test_the_slice_write_goes_through_the_write_key(store, tmp_path):
    sid = take(store, stub(THREE))["slices"][0]["slice_id"]
    with S.write_key_scope("turn-9#1"):
        first, bid = run(_event("Slept well for once.", slice_id=sid))
        again, _ = run(_event("Slept well for once.", slice_id=sid))
    assert again == first and store.sources.claimed("turn-9#1")["ids"] == [bid]


def test_the_pending_store_survives_a_restart(store, tmp_path, monkeypatch):
    sid = take(store, stub(THREE))["slices"][1]["slice_id"]
    run(trace(slice_id=sid, slice_span="m_0005..m_0006"))
    fresh = BucketManager({"buckets_dir": str(tmp_path)})
    assert fresh.slices.pending_count() == 3
    assert fresh.slices.get(sid)["span"] == {"first": "m_0005", "last": "m_0006", "count": 2}
    rec = fresh.slices.record_for(sid)
    assert (rec["id"], rec["through"]) == ("m_0005", "m_0006")


def test_view_slices_stands_alone(store):
    out = run(R.recall_core(when="today", room="", tag="", query="", view="slices"))
    assert "单独用" in out


# ───────────────────────── the two routes ─────────────────────────

def _routes(monkeypatch, store, model, locked=False):
    from starlette.requests import Request
    import web
    from web import _shared as sh
    from web import loci as Wb
    from web import panel_auth as PA

    routes = {}

    class _Mcp:
        def custom_route(self, path, methods=None, **kw):
            def keep(fn):
                routes[(methods[0], path)] = fn
                return fn
            return keep
    Wb.register(web._Gated(_Mcp()))
    monkeypatch.setattr(sh, "bucket_mgr", store)
    monkeypatch.setattr(sh, "config", {"slices": {"max_lines_per_batch": 100}})
    monkeypatch.setattr(sh, "dehydrator", None)
    monkeypatch.setattr(SL, "side_model", lambda dehydrator, config: model)
    monkeypatch.setattr(PA, "gate_needed", lambda: locked)
    monkeypatch.setattr(PA, "has_session", lambda r: False)
    monkeypatch.setattr(PA, "hook_token", lambda: "s3cret")

    def call(method, payload=None, key=None):
        raw = json.dumps(payload).encode() if payload is not None else b""
        headers = [(b"content-type", b"application/json")]
        if key:
            headers.append((b"x-loci-hook-token", key.encode()))

        async def receive():
            return {"type": "http.request", "body": raw, "more_body": False}
        req = Request({"type": "http", "method": method, "path": "/api/v2/slices",
                       "headers": headers, "query_string": b""}, receive)
        resp = asyncio.run(routes[(method, "/api/v2/slices")](req))
        return resp.status_code, json.loads(resp.body)
    return call


def test_the_intake_route_slices_and_the_read_route_lists(store, monkeypatch):
    call = _routes(monkeypatch, store, stub(THREE))
    status, out = call("POST", body())
    assert status == 200 and len(out["slices"]) == 3, out
    assert out["slices"][0]["span"] == {"first": "m_0001", "last": "m_0004", "count": 4}
    status, listed = call("GET")
    assert status == 200 and listed["pending"] == 3
    assert [s["slice_id"] for s in listed["batches"][0]["slices"]] == \
        [s["slice_id"] for s in out["slices"]]


def test_the_intake_route_answers_400_and_502(store, monkeypatch):
    call = _routes(monkeypatch, store, stub(RuntimeError("timeout")))
    status, out = call("POST", {**body(), "day": "yesterday"})
    assert status == 400
    status, out = call("POST", body(texts=["x"] * 101, ids=[f"m_{i}" for i in range(101)]))
    assert status == 400 and "100-line cap" in out["error"]
    status, out = call("POST", body())
    assert status == 502 and "nothing was stored" in out["error"]
    assert store.slices.pending_count() == 0


def test_a_line_withdrawn_while_slicing_answers_400_naming_it(store, monkeypatch):
    from core import _source_change as SC
    from core.scope import Host

    async def withdrawing(system, user):
        # The host's change lands while the side model is slicing.
        status, out = await SC.handle(store, {"change_id": "c-1", "host_seq": 1,
                                              "source": "lento:home/private:U#m_0006",
                                              "change": "withdrawn"},
                                      Host("life", scope_mode="open"))
        assert status == 200 and out["state"] == "withdrawn", out
        return json.dumps({"slices": THREE})
    call = _routes(monkeypatch, store, withdrawing)
    status, out = call("POST", body())
    assert status == 400, out
    assert out["note"] == "source_changed_while_slicing"
    assert out["lines"] == {"m_0006": "withdrawn"}
    assert "nothing was stored" in out["error"]
    assert store.slices.pending_count() == 0
    # Sent again as it is, the batch is refused before the side model.
    status, out = call("POST", body())
    assert status == 400 and "m_0006 is withdrawn" in out["error"] and "note" not in out


def _revise(store, line_id: str, revision: str, host_seq: int = 1, change_id: str = "c-r"):
    from core import _source_change as SC
    from core.scope import Host

    async def go():
        status, out = await SC.handle(store, {"change_id": change_id, "host_seq": host_seq,
                                              "source": f"lento:home/private:U#{line_id}",
                                              "change": "revised", "revision": revision},
                                      Host("life", scope_mode="open"))
        assert status == 200, out
    return go()


def test_a_line_revised_while_slicing_answers_400_and_nothing_is_kept(store, monkeypatch):
    async def revising(system, user):
        # The host announces r2 of one line while the side model slices r1.
        await _revise(store, "m_0006", "r2")
        return json.dumps({"slices": THREE})
    call = _routes(monkeypatch, store, revising)
    status, out = call("POST", body(revision="r1"))
    assert status == 400, out
    assert out["note"] == "source_changed_while_slicing"
    assert out["lines"] == {"m_0006": "revised"}
    assert "nothing was stored" in out["error"]
    assert store.slices.pending_count() == 0 and store.slices.batches() == []
    assert not store.sources.orders_path.exists(), "no line order is kept"
    # Sent again as it is, r1 is refused before the side model.
    status, out = call("POST", body(revision="r1"))
    assert status == 400 and "m_0006 is revised" in out["error"] and "note" not in out
    assert store.slices.pending_count() == 0


def test_the_new_version_of_a_revised_line_is_sliced(store, monkeypatch):
    run(_revise(store, "m_0006", "r2"))
    calls: list = []
    model = stub(THREE, calls=calls)
    # The line's own revision says which version it is, whatever the batch's watermark.
    b = body(revision="r1")
    b["lines"][5]["revision"] = "r2"
    out = take(store, model, b=b)
    assert len(out["slices"]) == 3 and store.slices.pending_count() == 3
    # And a batch delivering the old version is refused without calling the model.
    old = body(day="2026-01-01", revision="r1")
    with pytest.raises(SL.BatchError, match="m_0006 is revised"):
        take(store, model, b=old)
    assert len(calls) == 1


def test_a_revision_of_a_line_outside_the_batch_does_not_drop_it(store, monkeypatch):
    async def revising_elsewhere(system, user):
        await _revise(store, "m_0099", "r2")
        return json.dumps({"slices": THREE})
    out = take(store, revising_elsewhere, b=body(revision="r1"))
    assert len(out["slices"]) == 3 and store.slices.pending_count() == 3


# A `revised` change for a run reaches the lines it holds, the way a withdrawal does: the
# run's lines are registered (here m_0005..m_0008, inside the batch's twelve), and the
# announcement is for the run's key alone.
RUN = "lento:home/private:U#m_0005..m_0008"
RUN_LINES = IDS[4:8]


def _register_run(store, container="private:U", ids=RUN_LINES):
    assert store.sources.record_order({**SOURCE, "container": container}, ids) == S.RECORDED


def _revise_run(store, revision, run_=RUN, host_seq=1, change_id="c-run", **extra):
    from core import _source_change as SC
    from core.scope import Host

    async def go():
        status, out = await SC.handle(store, {"change_id": change_id, "host_seq": host_seq,
                                              "source": run_, "change": "revised",
                                              "revision": revision, **extra},
                                      Host("life", scope_mode="open"))
        assert status == 200, out
    return go()


def test_a_run_revised_before_the_batch_refuses_its_lines_at_the_door(store):
    _register_run(store)
    run(_revise_run(store, "r2"))
    calls: list = []
    with pytest.raises(SL.BatchError, match="m_0005 is revised") as refused:
        take(store, stub(THREE, calls=calls), b=body(revision="r1"))
    assert RUN in str(refused.value) and "nothing was stored" in str(refused.value)
    assert calls == [] and store.slices.pending_count() == 0
    # A line's own revision is what it is delivered at, whatever the watermark.
    b = body(revision="r2")
    b["lines"][6]["revision"] = "r1"
    with pytest.raises(SL.BatchError, match="m_0007 is revised"):
        take(store, stub(THREE, calls=calls), b=b)
    assert calls == []


def test_a_run_revised_while_slicing_answers_400_and_nothing_is_kept(store, monkeypatch):
    _register_run(store)
    orders = store.sources.orders_path.read_bytes()

    async def revising(system, user):
        # The host announces r2 of the whole run while the side model slices r1.
        await _revise_run(store, "r2")
        return json.dumps({"slices": THREE})
    call = _routes(monkeypatch, store, revising)
    status, out = call("POST", body(revision="r1"))
    assert status == 400, out
    assert out["note"] == "source_changed_while_slicing"
    assert out["lines"] == {i: "revised" for i in RUN_LINES}
    assert "nothing was stored" in out["error"]
    assert store.slices.pending_count() == 0 and store.slices.batches() == []
    assert store.sources.orders_path.read_bytes() == orders, "no line order is kept"
    # Sent again as it is, r1 is refused before the side model.
    status, out = call("POST", body(revision="r1"))
    assert status == 400 and "m_0005 is revised" in out["error"] and "note" not in out


def test_a_batch_at_the_runs_new_revision_is_taken(store):
    _register_run(store)
    run(_revise_run(store, "r2"))
    out = take(store, stub(THREE), b=body(revision="r2"))
    assert len(out["slices"]) == 3 and store.slices.pending_count() == 3


def test_the_newest_revision_of_a_line_is_the_one_applied_last(store):
    # host_seq is one order per source: the line's 9 and the run's 1 do not compare.
    _register_run(store)
    run(_revise(store, "m_0006", "e9", host_seq=9))
    run(_revise_run(store, "r2", host_seq=1))
    out = take(store, stub(THREE), b=body(revision="r2"))
    assert len(out["slices"]) == 3, "the run's r2 came after the line's e9"
    run(_revise(store, "m_0006", "e10", host_seq=10, change_id="c-r10"))
    with pytest.raises(SL.BatchError, match="m_0006 is revised"):
        take(store, stub(THREE), b=body(day="2026-01-01", revision="r2"))
    line = store.sources.revisions_reaching(f"lento:home/private:U#{IDS[5]}")
    assert [(r["source"], r["revision"]) for r in line] == [
        ("lento:home/private:U#m_0006", "e9"), (RUN, "r2"), ("lento:home/private:U#m_0006", "e10")]


def test_a_run_that_does_not_hold_the_line_has_no_effect(store):
    # Registered over other lines of the container, or over these ids in another one,
    # or never registered at all: none of them reaches the batch's lines.
    _register_run(store, ids=["m_0013", "m_0014", "m_0015"])
    _register_run(store, container="private:V")
    run(_revise_run(store, "r2", run_="lento:home/private:U#m_0013..m_0015"))
    run(_revise_run(store, "r2", run_="lento:home/private:V#m_0005..m_0008",
                    change_id="c-v"))
    run(_revise_run(store, "r2", run_="lento:home/private:U#m_0001..m_0003",
                    change_id="c-unknown"))

    async def revising_elsewhere(system, user):
        await _revise_run(store, "r3", run_="lento:home/private:U#m_0013..m_0015",
                          host_seq=2, change_id="c-run-2")
        return json.dumps({"slices": THREE})
    out = take(store, revising_elsewhere, b=body(revision="r1"))
    assert len(out["slices"]) == 3 and store.slices.pending_count() == 3


def test_a_memory_cut_before_a_run_was_revised_counts_as_revised(store):
    # What tells breath and the write receipt that a source moved on reads the same reach.
    _register_run(store)
    run(_revise_run(store, "r2", fingerprint="sha256:whole-run", fingerprint_by="loci"))
    line = {**SOURCE, "id": IDS[5], "revision": "r1", "fingerprint": _fp(TEXTS[5])}
    assert store.sources.newer_revision(line) == ("r2", "r1")
    assert store.sources.newer_revision({**line, "revision": "r2"}) == ("", "r2")
    run_rec = {**SOURCE, "id": IDS[4], "through": IDS[7], "revision": "r1"}
    assert [(str(ln), new, mine) for ln, new, mine in store.sources.run_revisions(run_rec)] \
        == [(f"lento:home/private:U#{i}", "r2", "r1") for i in RUN_LINES]
    assert store.sources.run_revisions({**run_rec, "revision": "r2"}) == []
    # Without a revision, a run's fingerprint is not compared with one line's.
    run(_revise_run(store, None, host_seq=2, change_id="c-fp",
                    fingerprint="sha256:whole-run-2", fingerprint_by="loci"))
    assert store.sources.newer_revision(line) == ("", None)
    out = take(store, stub(THREE), b=body(revision="r1"))
    assert len(out["slices"]) == 3


def test_both_routes_want_the_hook_key_when_the_panel_is_locked(store, monkeypatch):
    call = _routes(monkeypatch, store, stub(THREE), locked=True)
    assert call("POST", body())[0] == 401
    assert call("GET")[0] == 401
    assert call("GET", key="wrong")[0] == 401
    assert call("POST", body(), key="s3cret")[0] == 200
    assert call("GET", key="s3cret")[1]["pending"] == 3


def test_a_batch_carries_when_its_host_last_wrote_its_daily_report(store, monkeypatch):
    call = _routes(monkeypatch, store, stub(THREE))
    status, out = call("POST", body(report_at="2026-10-07T05:12:00+08:00"))
    assert status == 200, out
    [row] = [json.loads(x) for x in store.slices.path.read_text(encoding="utf-8").splitlines()]
    assert (row["host"], row["report_at"]) == ("legacy", "2026-10-07T05:12:00+08:00")
    moment, host = store.slices.last_report()
    assert host == "legacy" and moment.isoformat() == "2026-10-07T05:12:00+08:00"
    assert store.slices.last_report("someone-else") is None
    status, out = call("POST", body(day="2026-10-08"))
    assert status == 200 and store.slices.last_report()[0] == moment, "absent keeps none"


@pytest.mark.parametrize("bad", ["2026-10-07T05:12:00", "2026-10-07", "this morning", 1728249120,
                                 "2026-13-07T05:12:00+08:00"])
def test_a_report_at_that_does_not_read_is_a_400_and_nothing_is_stored(store, monkeypatch, bad):
    calls = []
    call = _routes(monkeypatch, store, stub(THREE, calls=calls))
    status, out = call("POST", body(report_at=bad))
    assert status == 400 and "report_at" in out["error"] and "offset" in out["error"], out
    assert calls == [] and store.slices.pending_count() == 0
    assert store.slices.last_report() is None


def test_the_hosts_order_of_a_batch_outlives_its_slices(store, tmp_path):
    take(store, stub(THREE))
    run_ = "lento:home/private:U#m_0002..m_0006"
    assert store.sources.members_of(run_) == IDS[1:6]
    fresh = BucketManager({"buckets_dir": str(tmp_path)})
    assert fresh.sources.members_of(run_) == IDS[1:6], "kept on disk, ids only"
    assert "Slept" not in (tmp_path / "_sources" / "line_orders.jsonl").read_text(encoding="utf-8")


def test_a_batch_registers_each_lines_revision_under_its_watermark(store):
    # The other team's 10-02 ruling: what was delivered at which revision is registered
    # with the order, so a memory cut from it is checked against what the host revises.
    b = body(revision="w-1")
    b["lines"] = [{**ln, "revision": "e1"} for ln in b["lines"]]
    take(store, stub(THREE), b=b)
    run_rec = {**SOURCE, "id": IDS[0], "through": IDS[3], "revision": "w-1"}
    assert store.sources.adopted_revisions(run_rec) == {i: "e1" for i in IDS[:4]}
    clash = body(day="2026-01-01", revision="w-1")
    clash["lines"] = [{**ln, "revision": "e2"} for ln in clash["lines"]]
    with pytest.raises(SL.BatchError):
        take(store, stub(THREE), b=clash)
