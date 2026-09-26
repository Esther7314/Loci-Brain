# -*- coding: utf-8 -*-
"""
exam/proxy.py — a pass-through that writes down every request a CLI sends to the model.

WHY
    The whole-turn layer has to show what actually reached the model — each request,
    with its messages in role order — not what the host believes it sent. A CLI adds its
    own system prompt, tool list and history, and none of that comes back on its output
    stream. So the host points the CLI at this relay (ANTHROPIC_BASE_URL), and the relay
    records each request body before forwarding it untouched.

WHAT IS RECORDED
    Request bodies of POST .../messages only, one JSON line each, with a sequence number
    and the time. Headers are never written: they carry the account's credentials.
    Responses are streamed straight back and not recorded here; tool calls and replies
    come from the CLI's own output stream.

    Upstream is https://api.anthropic.com unless EXAM_UPSTREAM says otherwise. The
    machine's HTTPS proxy settings apply to the upstream leg as usual.
"""

from __future__ import annotations

import asyncio
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx

UPSTREAM = os.environ.get("EXAM_UPSTREAM", "https://api.anthropic.com").rstrip("/")
_HOP = {"host", "content-length", "connection", "keep-alive", "transfer-encoding",
        "proxy-connection", "upgrade", "accept-encoding"}


class Recorder:
    def __init__(self, log_path: Path):
        self.log_path = log_path
        self.lock = threading.Lock()
        self.seq = 0

    def write(self, path: str, body: bytes) -> None:
        try:
            payload = json.loads(body.decode("utf-8"))
        except Exception:
            payload = {"_unparsed": body[:2000].decode("utf-8", "replace")}
        with self.lock:
            self.seq += 1
            line = {"seq": self.seq, "t": time.time(), "path": path, "body": payload}
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")


def make_handler(recorder: Recorder, client: httpx.Client):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *_):
            pass

        def _forward(self, method: str) -> None:
            length = int(self.headers.get("content-length") or 0)
            body = self.rfile.read(length) if length else b""
            if method == "POST" and self.path.split("?")[0].endswith("/messages"):
                recorder.write(self.path, body)
            headers = {k: v for k, v in self.headers.items() if k.lower() not in _HOP}
            try:
                with client.stream(method, UPSTREAM + self.path, headers=headers,
                                   content=body) as up:
                    self.send_response(up.status_code)
                    # iter_raw hands the bytes over exactly as they came, compressed or
                    # not, so content-encoding has to travel with them.
                    for k, v in up.headers.items():
                        if k.lower() not in _HOP:
                            self.send_header(k, v)
                    self.send_header("transfer-encoding", "chunked")
                    self.end_headers()
                    for chunk in up.iter_raw():
                        if chunk:
                            self.wfile.write(f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n")
                            self.wfile.flush()
                    self.wfile.write(b"0\r\n\r\n")
                    self.wfile.flush()
            except Exception as exc:
                msg = json.dumps({"error": f"exam proxy: {type(exc).__name__}: {exc}"}).encode()
                self.send_response(502)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(msg)))
                self.end_headers()
                self.wfile.write(msg)

        def do_POST(self):
            self._forward("POST")

        def do_GET(self):
            self._forward("GET")

    return Handler


class _QuietServer(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        pass  # the CLI drops idle keep-alive sockets; that is not worth a traceback


class Proxy:
    """Start with `with Proxy(log_path) as p:`; point the CLI at p.url."""

    def __init__(self, log_path: Path):
        self.recorder = Recorder(Path(log_path))
        self.client = httpx.Client(timeout=httpx.Timeout(600, connect=30), http2=False)
        self.server = _QuietServer(("127.0.0.1", 0),
                                          make_handler(self.recorder, self.client))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    def __enter__(self) -> "Proxy":
        self.thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self.server.shutdown()
        self.client.close()

    def requests_since(self, seq: int) -> list[dict]:
        if not self.recorder.log_path.exists():
            return []
        out = []
        for line in self.recorder.log_path.read_text(encoding="utf-8").splitlines():
            rec = json.loads(line)
            if rec["seq"] > seq:
                out.append(rec)
        return out


if __name__ == "__main__":  # manual check: python exam/proxy.py, then point a CLI at it
    with Proxy(Path("proxy-requests.jsonl")) as p:
        print(p.url, flush=True)
        asyncio.run(asyncio.sleep(3600))
