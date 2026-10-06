"""Canonical public-origin helpers shared by OAuth and MCP authentication.

The externally visible origin is security-sensitive: OAuth grants are bound to
it, and the MCP bearer-token middleware must validate against exactly the same
value.  Keep parsing in this side-effect-free module so both startup snapshots
use one normalization contract.
"""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import urlsplit, urlunsplit


MAX_PUBLIC_URI_CHARS = 2048
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_LEGACY_IPV4_COMPONENT = re.compile(r"(?:0x[0-9a-f]+|[0-9]+)\Z", re.IGNORECASE)


def _canonical_hostname(hostname: str) -> str:
    raw = str(hostname or "").rstrip(".")
    if not raw:
        return ""
    try:
        address = ipaddress.ip_address(raw)
    except ValueError:
        # WHATWG URL parsers may interpret decimal integers, hexadecimal/octal
        # values, or shortened dotted forms as IPv4 even though ipaddress and
        # RFC-style DNS parsing do not. Reject those ambiguous spellings instead
        # of letting OAuth bind what looks like a hostname to a loopback/private IP.
        numeric_parts = raw.split(".")
        if numeric_parts and all(
            _LEGACY_IPV4_COMPONENT.fullmatch(part) for part in numeric_parts
        ):
            return ""
        try:
            canonical = raw.encode("idna").decode("ascii").lower()
        except UnicodeError:
            return ""
        if len(canonical) > 253:
            return ""
        labels = canonical.split(".")
        if not labels or any(not _DNS_LABEL.fullmatch(label) for label in labels):
            return ""
        return canonical
    # ipaddress provides a stable lowercase/compressed form for IPv6 and a
    # canonical dotted-decimal form for IPv4.
    return address.compressed.lower()


def _canonical_authority(value: object) -> tuple[str, str] | None:
    raw = str(value or "").strip()
    if (
        not raw
        or len(raw) > MAX_PUBLIC_URI_CHARS
        or any(char.isspace() or char == "\\" for char in raw)
    ):
        return None
    try:
        parsed = urlsplit(raw)
        scheme = parsed.scheme.lower()
        hostname = parsed.hostname
        port = parsed.port
    except (TypeError, ValueError):
        return None
    if (
        scheme not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or port == 0
    ):
        return None
    canonical_host = _canonical_hostname(hostname)
    # Zone identifiers are local-interface details and must never become part
    # of a public OAuth resource identifier.
    if not canonical_host or "%" in canonical_host:
        return None
    if ":" in canonical_host:
        canonical_host = f"[{canonical_host}]"
    if port == (443 if scheme == "https" else 80):
        port = None
    authority = canonical_host if port is None else f"{canonical_host}:{port}"
    return scheme, authority


def normalize_public_origin(
    value: object, *, allow_mcp_endpoint: bool = True
) -> str:
    """Return a canonical HTTP(S) origin, or ``""`` for invalid input.

    Dashboard users commonly paste either ``https://host`` or the complete
    ``https://host/mcp`` connector URL.  Both identify the same public origin;
    other paths, queries, fragments, and credentials are rejected.
    """

    raw = str(value or "").strip()
    authority = _canonical_authority(raw)
    if authority is None:
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return ""
    path = parsed.path.rstrip("/")
    allowed_paths = {""}
    if allow_mcp_endpoint:
        allowed_paths.add("/mcp")
    if path not in allowed_paths or parsed.query or parsed.fragment:
        return ""
    scheme, netloc = authority
    return urlunsplit((scheme, netloc, "", "", ""))


def configured_public_origin(config: Mapping[str, Any] | object) -> str:
    """Read and normalize ``deployment.public_url`` from a config snapshot."""

    if not isinstance(config, Mapping):
        return ""
    deployment = config.get("deployment")
    if not isinstance(deployment, Mapping):
        return ""
    return normalize_public_origin(deployment.get("public_url"))


def normalize_http_resource(value: object) -> str:
    """Canonicalize an absolute HTTP(S) resource URI for equality checks."""

    raw = str(value or "").strip()
    authority = _canonical_authority(raw)
    if authority is None:
        return ""
    try:
        parsed = urlsplit(raw)
    except (TypeError, ValueError):
        return ""
    if parsed.query or parsed.fragment:
        return ""
    scheme, netloc = authority
    path = parsed.path.rstrip("/")
    return urlunsplit((scheme, netloc, path, "", ""))
