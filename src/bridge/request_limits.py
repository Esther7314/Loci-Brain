"""Small ASGI request-size guard for the public MCP endpoint."""

from __future__ import annotations

import json
from typing import Awaitable, Callable


_Receive = Callable[[], Awaitable[dict]]
_Send = Callable[[dict], Awaitable[None]]
_REJECTION_DRAIN_MULTIPLIER = 2


def is_mcp_endpoint_path(path: object) -> bool:
    """Match the one public MCP endpoint without accepting prefix lookalikes."""
    return str(path or "").rstrip("/") == "/mcp"


def is_sse_endpoint_path(path: object) -> bool:
    """Match FastMCP's legacy SSE handshake and message endpoints."""
    normalized = str(path or "").rstrip("/") or "/"
    return normalized == "/sse" or normalized == "/messages" or normalized.startswith(
        "/messages/"
    )


class MCPRequestBodyLimitMiddleware:
    """Reject oversized MCP requests before JSON-RPC parsing or tool dispatch."""

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        self.app = app
        self.max_bytes = max(0, int(max_bytes))
        self.path_matcher = path_matcher

    async def __call__(self, scope: dict, receive: _Receive, send: _Send) -> None:
        if (
            self.max_bytes <= 0
            or scope.get("type") != "http"
            or not self.path_matcher(scope.get("path"))
            or str(scope.get("method", "GET")).upper() not in {"POST", "PUT", "PATCH"}
        ):
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"").decode("latin-1").strip()
        if raw_length:
            try:
                declared_length = int(raw_length)
            except ValueError:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared_length < 0:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared_length > self.max_bytes:
                # Docker Desktop on Windows may reset the TCP connection when
                # an ASGI app returns before a modest in-flight request body is
                # consumed. Drain only a bounded amount, without parsing or
                # retaining it, so normal oversized clients reliably see 413
                # while very large/slow attacks are still rejected promptly.
                if declared_length <= self.max_bytes * _REJECTION_DRAIN_MULTIPLIER:
                    await self._drain_request(receive, max_bytes=declared_length)
                await self._send_too_large(send)
                return

        received = 0
        buffered: list[dict] = []
        while True:
            message = await receive()
            if not isinstance(message, dict):
                return
            if message.get("type") == "http.disconnect":
                return
            if message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > self.max_bytes:
                    if message.get("more_body", False):
                        await self._drain_request(receive, max_bytes=self.max_bytes)
                    await self._send_too_large(send)
                    return
                buffered.append(message)
                if not message.get("more_body", False):
                    break

        buffered_iter = iter(buffered)

        async def replay_receive() -> dict:
            try:
                return next(buffered_iter)
            except StopIteration:
                # Long-lived MCP transports keep reading after the request body
                # to observe the real disconnect. Replaying synthetic empty
                # requests forever creates a tight CPU loop.
                return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _drain_request(receive: _Receive, *, max_bytes: int) -> bool:
        """Discard at most ``max_bytes`` and report whether the request ended."""
        drained = 0
        while drained <= max(0, max_bytes):
            message = await receive()
            if not isinstance(message, dict):
                return False
            if message.get("type") == "http.disconnect":
                return True
            if message.get("type") != "http.request":
                continue
            drained += len(message.get("body", b""))
            if not message.get("more_body", False):
                return True
        return False

    async def _send_too_large(self, send: _Send) -> None:
        await self._send_json(
            send,
            413,
            f"MCP request body exceeds {self.max_bytes} bytes",
        )

    @staticmethod
    async def _send_json(send: _Send, status: int, error: str) -> None:
        body = json.dumps({"error": error}, separators=(",", ":")).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body, "more_body": False})


_MIB = 1024 * 1024
# The files the upload routes take, and what a multipart body adds around one file (the
# boundaries, the part headers, the few small fields beside it).
IMPORT_UPLOAD_BYTES = 50 * _MIB         # web/import_api.py: an exported conversation file
PACKAGE_UPLOAD_BYTES = 512 * _MIB       # web/library_api.py: an export package
                                        # (locibrain.storage.backup_archive.MAX_ARCHIVE_BYTES)
MULTIPART_SLACK_BYTES = 1 * _MIB

# The routes that take an upload, each with its own ceiling in place of the management
# limit. Their bodies are counted as they stream through and never held here: a 512 MiB
# package must not sit in memory before the route spools it to disk.
UPLOAD_CEILINGS = {
    "/api/import/preflight": IMPORT_UPLOAD_BYTES + MULTIPART_SLACK_BYTES,
    "/api/import/upload": IMPORT_UPLOAD_BYTES + MULTIPART_SLACK_BYTES,
    "/api/loci/import-package": PACKAGE_UPLOAD_BYTES + MULTIPART_SLACK_BYTES,
}


def upload_ceiling(path: object) -> int:
    """The body ceiling of an upload route, or 0 for any other path."""
    return UPLOAD_CEILINGS.get(str(path or "").rstrip("/") or "/", 0)


class ManagementRequestBodyLimitMiddleware(MCPRequestBodyLimitMiddleware):
    """Bound normal Dashboard/OAuth mutations; an upload route gets its own, larger ceiling
    (`UPLOAD_CEILINGS`), enforced while the body streams."""

    def __init__(
        self,
        app,
        *,
        max_bytes: int,
        mcp_path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        def should_limit(path: object) -> bool:
            normalized = str(path or "").rstrip("/") or "/"
            return not mcp_path_matcher(normalized) and not upload_ceiling(normalized)

        super().__init__(app, max_bytes=max_bytes, path_matcher=should_limit)

    async def __call__(self, scope: dict, receive: _Receive, send: _Send) -> None:
        ceiling = upload_ceiling(scope.get("path")) if scope.get("type") == "http" else 0
        if not ceiling or str(scope.get("method", "GET")).upper() not in {
                "POST", "PUT", "PATCH"}:
            await super().__call__(scope, receive, send)
            return
        await self._stream_under(ceiling, scope, receive, send)

    async def _stream_under(self, ceiling: int, scope: dict, receive: _Receive,
                            send: _Send) -> None:
        """Pass an upload through while counting it. Past the ceiling the client gets a
        413 at once and the route sees the client gone (its form parser stops, its own
        answer is dropped)."""
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        raw_length = headers.get(b"content-length", b"").decode("latin-1").strip()
        if raw_length:
            try:
                declared = int(raw_length)
            except ValueError:
                declared = -1
            if declared < 0:
                await self._send_json(send, 400, "invalid Content-Length")
                return
            if declared > ceiling:
                # A modest overshoot is drained first, as for the management limit (Docker
                # Desktop resets a connection answered before its body was read); a huge
                # one is answered straight away.
                if declared <= ceiling + max(self.max_bytes, _MIB):
                    await self._drain_request(receive, max_bytes=declared)
                await self._send_upload_too_large(send, ceiling)
                return

        received = 0
        cut = False
        answered = False

        async def counted_receive() -> dict:
            nonlocal received, cut, answered
            if cut:
                return {"type": "http.disconnect"}
            message = await receive()
            if isinstance(message, dict) and message.get("type") == "http.request":
                received += len(message.get("body", b""))
                if received > ceiling:
                    cut = True
                    if not answered:
                        answered = True
                        await self._send_upload_too_large(send, ceiling)
                    return {"type": "http.disconnect"}
            return message

        async def guarded_send(message: dict) -> None:
            nonlocal answered
            if cut:
                return
            if message.get("type") == "http.response.start":
                answered = True
            await send(message)

        await self.app(scope, counted_receive, guarded_send)

    async def _send_upload_too_large(self, send: _Send, ceiling: int) -> None:
        # The panel shows this to the person.
        await self._send_json(send, 413, f"上传的文件超过 {ceiling // _MIB} MB 的上限")

    async def _send_too_large(self, send: _Send) -> None:
        await self._send_json(
            send,
            413,
            f"management request body exceeds {self.max_bytes} bytes",
        )
