"""
========================================
deployment_profile.py — pure domain rules for deployment mode and secure defaults
========================================

Normalizes the three choices a user can make — local machine / secured public / advanced
custom — into explicit configuration, and checks the security invariants around anonymous
public exposure, transport, and OAuth.

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


PROFILE_LOCAL = "local"
PROFILE_PUBLIC = "public_secure"
PROFILE_ADVANCED = "advanced"
_PROFILE_NAMES = frozenset({PROFILE_LOCAL, PROFILE_PUBLIC, PROFILE_ADVANCED})


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


def normalize_profile(value: Any) -> str:
    """Normalize a deployment-mode identifier. An unknown value raises rather than
    being silently guessed at."""
    profile = str(value or "").strip().lower()
    aliases = {
        "public": PROFILE_PUBLIC,
        "secure": PROFILE_PUBLIC,
        "public-secure": PROFILE_PUBLIC,
        "custom": PROFILE_ADVANCED,
    }
    profile = aliases.get(profile, profile)
    if profile not in _PROFILE_NAMES:
        raise ValueError("profile 必须是 local、public_secure 或 advanced")
    return profile


def _as_bool(value: Any, *, default: bool) -> bool:
    """Parse a wizard boolean strictly: the string "false" must not come out true."""
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


