"""
========================================
deployment_profile.py — the HTTPS rule for a public address
========================================

Normalizes the public address a user enters in the panel: a bare hostname is taken as
https://, and anything that is not HTTPS is refused.

What this does NOT do: no file reads or writes, no HTTP route registration, no changes to
environment variables, no service restarts.
Public surface: normalize_public_https_origin() (used by web/config_api.py).
========================================
"""

from __future__ import annotations

from typing import Any

from bridge.public_origin import (
    normalize_public_origin,
)


def normalize_public_https_origin(value: Any) -> str:
    """Apply public-deployment HTTPS policy around the shared URL parser."""
    raw = str(value or "").strip()
    if not raw:
        return ""
    # The Dashboard explicitly invites a hostname.  Only an omitted scheme is
    # promoted; an explicit http:// value remains invalid rather than silently
    # changing what the operator entered.
    candidate = raw if "://" in raw else f"https://{raw}"
    normalized = normalize_public_origin(candidate, allow_mcp_endpoint=True)
    return normalized if normalized.startswith("https://") else ""
