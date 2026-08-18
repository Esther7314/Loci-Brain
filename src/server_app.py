"""Testable HTTP application and runtime lifecycle assembly.

This module deliberately has no Loci engine construction at import time. The
CLI entry point creates the concrete services, then passes them into the small
factory and lifecycle objects below. A future desktop host can use the same
boundary without importing the side-effectful ``server`` module.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping
import httpx
from starlette.middleware.cors import CORSMiddleware

from locibrain.security.public_origin import (
    configured_public_origin,
    normalize_public_origin,
)
from utils import parse_bool
from bridge.request_limits import (
    MCPRequestBodyLimitMiddleware,
    ManagementRequestBodyLimitMiddleware,
    is_mcp_endpoint_path,
    is_sse_endpoint_path,
)


DEFAULT_MAX_MCP_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES = 4 * 1024 * 1024
DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS = 5.0
DEFAULT_KEEPALIVE_INITIAL_DELAY_SECONDS = 10.0
DEFAULT_KEEPALIVE_INTERVAL_SECONDS = 60.0

TokenValidator = Callable[..., bool]
AsyncCallback = Callable[[], Awaitable[Any]]


@dataclass(frozen=True)
class HTTPRuntimeSettings:
    """Normalized settings used while assembling an HTTP MCP application."""

    auth_required: bool
    max_request_bytes: int
    max_management_request_bytes: int = DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES
    # "oauth" (default) or "token" — only consulted when auth_required is True.
    # Mutually exclusive: see MCPAuthMiddleware and web/oauth.py's route 404s.
    auth_mode: str = "oauth"
    # Canonical external origin captured from the same startup config snapshot
    # used by OAuth route registration.  An empty value means request-derived
    # trusted-proxy/Host fallback.
    public_origin: str = ""

    @classmethod
    def from_config(
        cls,
        config: Mapping[str, Any],
        *,
        default_max_request_bytes: int = DEFAULT_MAX_MCP_REQUEST_BYTES,
    ) -> "HTTPRuntimeSettings":
        limits = config.get("limits")
        if not isinstance(limits, Mapping):
            limits = {}
        def body_limit(key: str, default: int) -> int:
            try:
                value = int(limits.get(key, default))
            except (TypeError, ValueError, OverflowError):
                return default
            return default if value < 0 else value

        max_request_bytes = body_limit(
            "max_mcp_request_bytes", default_max_request_bytes
        )
        max_management_request_bytes = body_limit(
            "max_management_request_bytes",
            DEFAULT_MAX_MANAGEMENT_REQUEST_BYTES,
        )
        auth_mode = str(config.get("mcp_auth_mode", "oauth")).strip().lower()
        if auth_mode not in ("oauth", "token"):
            auth_mode = "oauth"
        return cls(
            auth_required=parse_bool(
                config.get("mcp_require_auth", True), default=True
            ),
            max_request_bytes=max_request_bytes,
            max_management_request_bytes=max_management_request_bytes,
            auth_mode=auth_mode,
            public_origin=configured_public_origin(config),
        )


def merge_mcp_tool_registries(primary: Any, extra: Any) -> int:
    """Merge FastMCP's compatibility registry into the public registry.

    FastMCP does not currently expose a public registry merge API. Keeping this
    compatibility access in one function makes the private dependency easy to
    test and replace when the SDK adds one.
    """

    primary_tools = primary._tool_manager._tools
    extra_tools = extra._tool_manager._tools
    primary_tools.update(extra_tools)
    return len(extra_tools)


def _first_forwarded_value(value: str) -> str:
    return value.split(",", 1)[0].strip()


def _header_text(headers: Mapping[bytes, bytes], name: bytes) -> str:
    try:
        return headers.get(name, b"").decode("latin-1").strip()
    except (AttributeError, UnicodeError):
        return ""


def _scope_peer_host(scope: Mapping[str, Any]) -> str:
    client = scope.get("client")
    if isinstance(client, (tuple, list)) and client:
        return str(client[0] or "").strip()
    return ""


def _scope_peer_is_trusted_proxy(scope: Mapping[str, Any]) -> bool:
    """Use the Dashboard's existing trusted-proxy CIDR policy.

    Keeping one policy avoids accepting forwarding headers here while rejecting
    them in login rate limiting and secure-cookie detection (or vice versa).
    The import is lazy so importing this testable assembly module remains free
    of Dashboard runtime initialization.
    """

    from web._shared import _is_trusted_proxy

    return _is_trusted_proxy(_scope_peer_host(scope))


def _normalize_http_origin(value: str) -> str | None:
    """Return a canonical HTTP(S) origin, or ``None`` when malformed."""

    return normalize_public_origin(value, allow_mcp_endpoint=False) or None


def _request_base(scope: Mapping[str, Any], headers: Mapping[bytes, bytes]) -> str:
    proto = str(scope.get("scheme", "http")).strip().lower()
    if proto not in ("http", "https"):
        proto = "http"
    host = _first_forwarded_value(_header_text(headers, b"host"))

    if _scope_peer_is_trusted_proxy(scope):
        forwarded_proto = _first_forwarded_value(
            _header_text(headers, b"x-forwarded-proto")
        ).lower()
        if forwarded_proto in ("http", "https"):
            proto = forwarded_proto
        forwarded_host = _first_forwarded_value(
            _header_text(headers, b"x-forwarded-host")
        )
        if forwarded_host:
            host = forwarded_host

    return _normalize_http_origin(f"{proto}://{host}") or ""


def _canonical_mcp_base(
    scope: Mapping[str, Any],
    headers: Mapping[bytes, bytes],
    public_origin: str,
) -> str:
    """Use the configured public origin, then the trusted request fallback."""

    return normalize_public_origin(public_origin) or _request_base(scope, headers)


def _extract_bearer_token(value: str) -> str:
    """Parse an RFC 7235 auth scheme (scheme names are case-insensitive)."""

    parts = str(value or "").strip().split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        return ""
    return parts[1].strip()


class MCPAuthMiddleware:
    """Require a bearer token for every endpoint of the selected MCP transport."""

    def __init__(
        self,
        app: Any,
        *,
        auth_required: bool,
        token_validator: TokenValidator,
        auth_mode: str = "oauth",
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
        resource_path: str = "/mcp",
        public_origin: str = "",
    ) -> None:
        self.app = app
        self.auth_required = bool(auth_required)
        self.token_validator = token_validator
        self.auth_mode = auth_mode if auth_mode in ("oauth", "token") else "oauth"
        self.path_matcher = path_matcher
        self.resource_path = "/" + str(resource_path or "mcp").strip("/")
        self.public_origin = normalize_public_origin(public_origin)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        path = str(scope.get("path", ""))
        if (
            scope.get("type") == "http"
            and str(scope.get("method", "")).upper() != "OPTIONS"
            and self.auth_required
            and self.path_matcher(path)
        ):
            headers = {key.lower(): value for key, value in scope.get("headers", [])}
            auth = headers.get(b"authorization", b"").decode("latin-1")
            base = _canonical_mcp_base(scope, headers, self.public_origin)
            # OAuth discovery currently exposes one canonical MCP resource.
            # Legacy SSE's /sse and /messages routes are two transport legs of
            # that same resource, not independently token-bound resources.
            resource = f"{base}{self.resource_path}"
            bearer_token = _extract_bearer_token(auth)
            valid = bool(bearer_token) and self.token_validator(
                bearer_token, resource=resource
            )
            if not valid and self.auth_mode == "token":
                # Fallback header for MCP clients that can't customize Authorization.
                alt_token = headers.get(b"loci-mcp-token", b"").decode(
                    "latin-1"
                ).strip()
                if alt_token:
                    valid = self.token_validator(alt_token, resource=resource)
            if not valid:
                endpoint = self.resource_path.strip("/")
                if self.auth_mode == "token":
                    # No OAuth server exists in token mode — a resource_metadata
                    # challenge pointing at a 404'd discovery endpoint would mislead.
                    challenge = 'Bearer realm="Loci Brain"'
                    body = json.dumps({"error": "Unauthorized"}).encode()
                else:
                    metadata_url = (
                        f"{base}/.well-known/oauth-protected-resource/{endpoint}"
                    )
                    challenge = (
                        'Bearer realm="Loci Brain",'
                        f' resource_metadata="{metadata_url}", scope="mcp"'
                    )
                    body = json.dumps(
                        {
                            "error": "Unauthorized",
                            "resource_metadata": metadata_url,
                        }
                    ).encode()
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            [b"content-type", b"application/json"],
                            [b"www-authenticate", challenge.encode()],
                            [b"content-length", str(len(body)).encode()],
                        ],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": body,
                        "more_body": False,
                    }
                )
                return
        await self.app(scope, receive, send)


class MCPAcceptShim:
    """Ensure MCP clients advertise both supported response media types."""

    _REQUIRED = (b"application/json", b"text/event-stream")

    def __init__(
        self,
        app: Any,
        *,
        path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
    ) -> None:
        self.app = app
        self.path_matcher = path_matcher

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") == "http" and self.path_matcher(
            scope.get("path")
        ):
            headers = list(scope.get("headers", []))
            accept_index = next(
                (
                    index
                    for index, (key, _value) in enumerate(headers)
                    if key.lower() == b"accept"
                ),
                -1,
            )
            current = headers[accept_index][1].lower() if accept_index >= 0 else b""
            missing = [value for value in self._REQUIRED if value not in current]
            if missing:
                required = b", ".join(missing)
                if accept_index >= 0 and headers[accept_index][1].strip():
                    headers[accept_index] = (
                        headers[accept_index][0],
                        headers[accept_index][1] + b", " + required,
                    )
                elif accept_index >= 0:
                    headers[accept_index] = (headers[accept_index][0], required)
                else:
                    headers.append((b"accept", required))
                scope = dict(scope)
                scope["headers"] = headers
        await self.app(scope, receive, send)


class OriginCSRFGuardMiddleware:
    """Reject cross-origin state-changing requests to the cookie-session surface.

    CORS below is wide open (``allow_origins=["*"]``) only so browser-based MCP
    clients (e.g. claude.ai) can call ``/mcp`` cross-origin with a Bearer token.
    That has no business relaxing the Origin for the cookie-session dashboard
    (``/auth/*``, ``/api/*``, ...): a POST/PUT/DELETE whose ``Origin`` doesn't
    match its own ``Host`` has no legitimate reason to mutate password/session
    state there, no matter what the CORS preflight allowed. ``/mcp``,
    ``/oauth/*`` and ``/.well-known/*`` are exempt — they authenticate via
    bearer tokens / proof-of-possession (PKCE), not ambient cookies, so a
    mismatched Origin there isn't a CSRF risk.
    """

    _SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
    _EXEMPT_PREFIXES = ("/oauth/", "/.well-known/")

    def __init__(
        self,
        app: Any,
        *,
        mcp_path_matcher: Callable[[object], bool] = is_mcp_endpoint_path,
        public_origin: str = "",
    ) -> None:
        self.app = app
        self.mcp_path_matcher = mcp_path_matcher
        # Optional second proof for browsers/proxies that strip Fetch Metadata:
        # the persisted external origin is operator-controlled and shared with
        # OAuth/MCP resource binding. It never overrides an explicit cross-site
        # Fetch Metadata signal below.
        self.public_origin = normalize_public_origin(public_origin)

    def _is_exempt(self, path: str) -> bool:
        if self.mcp_path_matcher(path):
            return True
        return any(path.startswith(prefix) for prefix in self._EXEMPT_PREFIXES)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        path = str(scope.get("path", ""))
        if method in self._SAFE_METHODS or self._is_exempt(path):
            await self.app(scope, receive, send)
            return
        raw_headers = [
            (key.lower(), value) for key, value in scope.get("headers", [])
        ]
        headers = dict(raw_headers)
        origin = _header_text(headers, b"origin")
        fetch_site = _header_text(headers, b"sec-fetch-site").lower()
        singular_security_headers = {
            b"origin",
            b"host",
            b"sec-fetch-site",
            b"forwarded",
            b"x-forwarded-for",
            b"x-forwarded-host",
            b"x-forwarded-proto",
        }
        duplicate_security_header = any(
            sum(1 for key, _value in raw_headers if key == name) > 1
            for name in singular_security_headers
        )
        normalized_origin = _normalize_http_origin(origin) if origin else None
        # Browsers do not let page JavaScript forge Sec-Fetch-Site.  Its
        # same-origin value therefore remains useful when a reverse proxy
        # rewrites Host/scheme but omits X-Forwarded-Host/Proto.  Missing,
        # navigation-only ("none"), and unknown values retain the Origin
        # fallback for older clients; non-same-origin browser contexts fail
        # closed even if they omit Origin.
        reject = (
            duplicate_security_header
            or (bool(origin) and normalized_origin is None)
            or fetch_site in ("same-site", "cross-site")
        )
        if fetch_site != "same-origin" and not reject and origin:
            own_base = _request_base(scope, headers)
            allowed_origins = {own_base}
            if self.public_origin:
                allowed_origins.add(self.public_origin)
            reject = (
                normalized_origin is None or normalized_origin not in allowed_origins
            )
        if reject:
            body = json.dumps({"error": "Cross-origin request rejected"}).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [
                        [b"content-type", b"application/json"],
                        [b"content-length", str(len(body)).encode()],
                    ],
                }
            )
            await send({"type": "http.response.body", "body": body, "more_body": False})
            return
        await self.app(scope, receive, send)


class NgrokHeaderMiddleware:
    """Stamp every response with ``ngrok-skip-browser-warning`` (issue #16).

    Deployments tunneled through ngrok's free tier serve an HTML browser-
    warning interstitial instead of proxying to the origin. ngrok recognizes
    this header (request *or* response side) as an explicit opt-out for
    non-browser clients, which is what an MCP client like claude.ai always
    is. Applied unconditionally so it also lands on auth-rejected and
    error responses, not just successful tool calls.
    """

    _HEADER_NAME = b"ngrok-skip-browser-warning"
    _HEADER_VALUE = b"true"

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_header(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(key.lower() == self._HEADER_NAME for key, _ in headers):
                    headers.append((self._HEADER_NAME, self._HEADER_VALUE))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_header)


class SecurityHeadersMiddleware:
    """Apply browser hardening headers to success and error responses."""

    # 🔴 2026-08-08：允许 Lento Home（3010）把这个页面嵌进「地下室」那一格。
    #
    #    她要的：「点击 basement 的那一格 **放上那个星空那个 dashboard**」。
    #    原来是 `frame-ancestors 'none'` + `X-Frame-Options: DENY` —— 谁都不许嵌，
    #    所以 iframe 是一片空白。
    #
    #    ⚠️ **这不是把门打开，是点名放行自己家那一页。** 别的来源照样全拒。
    #    ⚠️ `X-Frame-Options` **必须整条拿掉**，不能改成 ALLOW-FROM ——
    #       那个语法现代浏览器早就不支持了，留着 DENY 会跟下面的 CSP 打架
    #       （规范说 CSP frame-ancestors 在场时该忽略 XFO，但别赌浏览器都守规矩）。
    #    📌 **她家的内网 IP 换了，这一行要跟着改** —— 记在 交接/环境速查.md 里。
    _可以嵌我的 = " ".join([
        "'self'",
        "http://localhost:3010",
        "http://127.0.0.1:3010",
        "http://192.168.1.5:3010",      # 她手机开 Home 用的那个
    ])

    _HEADERS = (
        (b"content-security-policy",
         ("frame-ancestors " + _可以嵌我的).encode("utf-8")),
        (b"x-content-type-options", b"nosniff"),
        (b"referrer-policy", b"no-referrer"),
        (
            b"permissions-policy",
            b"camera=(), geolocation=(), microphone=(), payment=(), usb=()",
        ),
    )

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        async def send_with_headers(message: dict) -> None:
            if message.get("type") == "http.response.start":
                headers = list(message.get("headers", []))
                existing = {key.lower() for key, _value in headers}
                headers.extend(
                    (key, value)
                    for key, value in self._HEADERS
                    if key not in existing
                )
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, send_with_headers)


@dataclass
class RuntimeLifecycle:
    """Own background service startup and shutdown for one HTTP app lifespan."""

    logger: Any
    decay_engine: Any = None
    embedding_outbox: Any = None
    ensure_ollama_child: AsyncCallback | None = None
    stop_ollama_child: AsyncCallback | None = None
    load_tunnel_config: Callable[[], Mapping[str, Any]] | None = None
    start_tunnel: Callable[[str], tuple[bool, str]] | None = None
    stop_tunnel: Callable[[], Any] | None = None
    restart_github_auto_task: Callable[[int], Any] | None = None
    github_auto_interval: int = 0
    boot_marker_path: str = ""
    keepalive_url: str = ""
    keepalive_initial_delay: float = DEFAULT_KEEPALIVE_INITIAL_DELAY_SECONDS
    keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL_SECONDS
    health_probe_timeout: float = DEFAULT_HEALTH_PROBE_TIMEOUT_SECONDS
    _keepalive_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _started: bool = field(default=False, init=False, repr=False)

    async def _run_async_step(self, label: str, callback: AsyncCallback | None) -> None:
        if callback is None:
            return
        try:
            await callback()
        except Exception as exc:
            self.logger.warning("%s failed: %s", label, exc)

    def _start_optional_services(self) -> None:
        if self.load_tunnel_config is not None and self.start_tunnel is not None:
            try:
                tunnel_config = self.load_tunnel_config()
                if tunnel_config.get("auto_start") and tunnel_config.get("token"):
                    _ok, message = self.start_tunnel(str(tunnel_config["token"]))
                    self.logger.info("Tunnel auto-start: %s", message)
            except Exception as exc:
                self.logger.warning("tunnel auto-start failed: %s", exc)

        if self.github_auto_interval > 0 and self.restart_github_auto_task is not None:
            try:
                self.restart_github_auto_task(self.github_auto_interval)
            except Exception as exc:
                self.logger.warning("github auto-sync start failed: %s", exc)

    def _reset_boot_marker(self) -> None:
        if not self.boot_marker_path or not os.path.exists(self.boot_marker_path):
            return
        try:
            with open(self.boot_marker_path, "w", encoding="utf-8") as marker:
                marker.write("0")
            self.logger.info("boot ok -> reset .boot_fails")
        except Exception as exc:
            self.logger.warning("reset .boot_fails failed: %s", exc)

    async def _keepalive_loop(self) -> None:
        await asyncio.sleep(max(0.0, self.keepalive_initial_delay))
        async with httpx.AsyncClient() as client:
            while True:
                try:
                    await client.get(
                        self.keepalive_url,
                        timeout=self.health_probe_timeout,
                    )
                    self.logger.debug("Keepalive ping OK")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.logger.warning("Keepalive ping failed: %s", exc)
                await asyncio.sleep(max(0.01, self.keepalive_interval))

    async def start(self) -> None:
        if self._started:
            return
        self._started = True
        self._start_optional_services()
        await self._run_async_step(
            "decay engine start",
            getattr(self.decay_engine, "start", None),
        )
        await self._run_async_step("ollama child boot", self.ensure_ollama_child)
        await self._run_async_step(
            "embedding outbox start",
            getattr(self.embedding_outbox, "start", None),
        )
        if self.keepalive_url:
            self._keepalive_task = asyncio.create_task(
                self._keepalive_loop(),
                name="loci-health-keepalive",
            )
        self._reset_boot_marker()

    async def stop(self) -> None:
        if not self._started:
            return
        self._started = False

        task = self._keepalive_task
        self._keepalive_task = None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        if self.restart_github_auto_task is not None:
            try:
                self.restart_github_auto_task(0)
            except Exception as exc:
                self.logger.warning("github auto-sync stop failed: %s", exc)

        await self._run_async_step(
            "embedding outbox stop",
            getattr(self.embedding_outbox, "stop", None),
        )
        await self._run_async_step(
            "decay engine stop",
            getattr(self.decay_engine, "stop", None),
        )
        await self._run_async_step("ollama child stop", self.stop_ollama_child)
        if self.stop_tunnel is not None:
            try:
                self.stop_tunnel()
            except Exception as exc:
                self.logger.warning("tunnel stop failed: %s", exc)


def install_runtime_lifespan(app: Any, lifecycle: RuntimeLifecycle) -> Any:
    """Compose Loci runtime services with an app's existing lifespan."""

    parent_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def managed_lifespan(lifespan_app: Any):
        async with parent_lifespan(lifespan_app):
            await lifecycle.start()
            try:
                yield
            finally:
                await lifecycle.stop()

    app.router.lifespan_context = managed_lifespan
    return app


def build_http_app(
    mcp: Any,
    transport: str,
    *,
    settings: HTTPRuntimeSettings,
    token_validator: TokenValidator,
    lifecycle: RuntimeLifecycle,
) -> Any:
    """Build the HTTP/SSE ASGI app with one consistent middleware stack."""

    if transport == "streamable-http":
        app = mcp.streamable_http_app()
    elif transport == "sse":
        app = mcp.sse_app()
    else:
        raise ValueError(f"HTTP app cannot be built for transport: {transport}")

    mcp_path_matcher = (
        is_sse_endpoint_path if transport == "sse" else is_mcp_endpoint_path
    )

    install_runtime_lifespan(app, lifecycle)
    app.add_middleware(
        OriginCSRFGuardMiddleware,
        mcp_path_matcher=mcp_path_matcher,
        public_origin=settings.public_origin,
    )
    app.add_middleware(
        MCPRequestBodyLimitMiddleware,
        max_bytes=settings.max_request_bytes,
        path_matcher=mcp_path_matcher,
    )
    app.add_middleware(
        ManagementRequestBodyLimitMiddleware,
        max_bytes=settings.max_management_request_bytes,
        mcp_path_matcher=mcp_path_matcher,
    )
    app.add_middleware(MCPAcceptShim, path_matcher=mcp_path_matcher)
    app.add_middleware(
        MCPAuthMiddleware,
        auth_required=settings.auth_required,
        token_validator=token_validator,
        auth_mode=settings.auth_mode,
        path_matcher=mcp_path_matcher,
        resource_path="/mcp",
        public_origin=settings.public_origin,
    )
    # Starlette wraps middleware in reverse registration order.  CORS must be
    # outside MCP auth so browser preflights never receive a bare 401 and auth
    # challenges/errors still carry the appropriate CORS response headers.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["*"],
    )
    # Outermost: must still fire on auth-rejected/error responses, not just
    # successful tool calls, so add it last (see NgrokHeaderMiddleware).
    app.add_middleware(NgrokHeaderMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    app.state.loci_http_settings = settings
    app.state.loci_runtime_lifecycle = lifecycle
    return app
