# -*- coding: utf-8 -*-
"""
tests/test_side_model_calls_are_bounded.py — a slow side model is not sent the same
call over and over, and a sweep does not send it everything at once

Dehydrator._chat is the one retry layer (three attempts, with backoff): the OpenAI client
under it retries nothing itself, so a side model slower than the timeout gets each call
three times, not three times three. The backfill runner (tools/grow/rooms_path.
_backfill_batch) keeps at most BACKFILL_CONCURRENCY calls in flight, so a startup sweep
over many unfinished entries drains instead of queueing past its timeouts.
"""

import asyncio
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from core import dehydrator as DH
from tools.grow import rooms_path


class _Slow(BaseHTTPRequestHandler):
    """An upstream that takes every request and answers none in time."""
    seen = 0
    lock = threading.Lock()

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        with _Slow.lock:
            _Slow.seen += 1
        time.sleep(2)


@pytest.fixture
def slow_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Slow)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Slow.seen = 0
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/v1"
    finally:
        server.shutdown()
        server.server_close()


def test_a_call_that_times_out_is_sent_once_per_attempt(slow_upstream, tmp_path, monkeypatch):
    monkeypatch.setattr(DH, "_RETRY_BASE_DELAY", 0.0)
    d = DH.Dehydrator({"buckets_dir": str(tmp_path),
                       "dehydration": {"base_url": slow_upstream, "api_key": "k", "model": "m",
                                       "timeout_seconds": 0.3}})
    try:
        with pytest.raises(Exception) as e:
            asyncio.run(d._chat("system", "user"))
        assert DH.Dehydrator._is_transient_error(e.value)
        assert _Slow.seen == DH._RETRY_MAX_ATTEMPTS
    finally:
        d.close()


def test_the_client_retries_nothing_itself():
    assert DH.openai_client("k", "http://127.0.0.1:9/v1", 1.0).max_retries == 0


def test_a_backfill_batch_keeps_few_calls_in_flight(monkeypatch):
    in_flight, most, done = 0, 0, []

    async def one(bucket_id, text, kind):
        nonlocal in_flight, most
        in_flight += 1
        most = max(most, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        done.append(bucket_id)
        return "backfilled"

    async def nothing(*args, **kwargs):
        return []

    class Store:
        list_all = staticmethod(nothing)

    monkeypatch.setattr(rooms_path, "_backfill_one", one)
    monkeypatch.setattr(rooms_path.rt, "bucket_mgr", Store())
    pairs = [(f"b{i}", "正文", "event") for i in range(30)]
    asyncio.run(rooms_path._backfill_batch(pairs))
    assert sorted(done) == sorted(p[0] for p in pairs)
    assert most == rooms_path.BACKFILL_CONCURRENCY
