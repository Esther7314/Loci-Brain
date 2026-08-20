"""
========================================
deployment_profile.py — pure domain rules for deployment mode and secure defaults
========================================

Normalizes the three choices a user can make — local machine / secured public / advanced
custom — into explicit configuration, and checks the security invariants around anonymous
public exposure, transport, and OAuth.

What this does NOT do: no file reads or writes, no HTTP route registration, no changes to
environment variables, no service restarts.
Public surface: profile_catalog(), build_profile_patch(), validate_profile_patch(),
effective_configuration_report().
========================================
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from locibrain.security.public_origin import (
    configured_public_origin,
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


def profile_catalog() -> list[dict[str, Any]]:
    """The three modes the front-end can display. Their security meaning is defined
    here once and nowhere else."""
    return [
        {
            "id": PROFILE_LOCAL,
            "name": "本机模式",
            "description": "只在自己的设备或可信内网使用，不直接暴露公网。",
            "recommended_for": "本机、NAS、可信局域网",
            "defaults": {"transport": "streamable-http", "mcp_require_auth": False},
        },
        {
            "id": PROFILE_PUBLIC,
            "name": "公网安全模式",
            "description": "通过 HTTPS 域名远程连接，强制 OAuth 保护 MCP。",
            "recommended_for": "Zeabur、Render、Cloudflare Tunnel、公开域名",
            "defaults": {
                "transport": "streamable-http",
                "mcp_require_auth": True,
                "mcp_auth_mode": "oauth",
            },
        },
        {
            "id": PROFILE_ADVANCED,
            "name": "高级自定义",
            "description": "自行管理反向代理、外部鉴权和网络边界；系统持续显示风险。",
            "recommended_for": "已有安全网关或自定义客户端",
            "defaults": {},
        },
    ]


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


def build_profile_patch(profile: Any, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Turn the user's choice into the smallest patch that can be written to config.yaml."""
    normalized = normalize_profile(profile)
    opts = dict(options or {})
    if normalized == PROFILE_LOCAL:
        auth_required = False
    elif normalized == PROFILE_PUBLIC:
        auth_required = True
    else:
        auth_required = _as_bool(opts.get("mcp_require_auth"), default=True)
    transport = str(opts.get("transport") or "streamable-http").strip().lower()
    if transport in {"http", "streamable", "streamable_http", "streamablehttp"}:
        transport = "streamable-http"
    patch: dict[str, Any] = {
        "transport": transport,
        "mcp_require_auth": auth_required,
        "deployment": {
            "profile": normalized,
            "onboarding_completed": True,
        },
    }
    if normalized == PROFILE_PUBLIC:
        patch["mcp_auth_mode"] = "oauth"
    public_url = str(opts.get("public_url") or "").strip()
    if public_url:
        normalized_public_url = normalize_public_https_origin(public_url)
        if not normalized_public_url:
            raise ValueError("公网地址必须是 HTTPS 域名或完整的 /mcp 地址")
        patch["deployment"]["public_url"] = normalized_public_url
    return patch


def validate_profile_patch(patch: Mapping[str, Any]) -> list[str]:
    """Return the security problems that block saving. An empty list means it may be
    written to disk."""
    deployment = patch.get("deployment")
    if not isinstance(deployment, Mapping):
        return ["缺少 deployment 配置"]
    try:
        profile = normalize_profile(deployment.get("profile"))
    except ValueError as exc:
        return [str(exc)]
    issues: list[str] = []
    transport = str(patch.get("transport") or "").strip().lower()
    auth_required = _as_bool(patch.get("mcp_require_auth"), default=True)
    auth_mode = str(patch.get("mcp_auth_mode") or "oauth").strip().lower()
    public_url = str(deployment.get("public_url") or "").strip()
    if transport not in {"streamable-http", "sse", "stdio"}:
        issues.append("transport 必须是 streamable-http、sse 或 stdio")
    if profile == PROFILE_PUBLIC:
        if not auth_required:
            issues.append("公网安全模式不能关闭 OAuth")
        if auth_mode != "oauth":
            issues.append("公网安全模式必须使用 OAuth 鉴权模式")
        if public_url:
            if not normalize_public_https_origin(public_url):
                issues.append("公网地址必须是 HTTPS 域名或完整的 /mcp 地址")
    if profile == PROFILE_LOCAL and public_url:
        issues.append("本机模式不能同时声明公网地址")
    return issues


def effective_configuration_report(
    runtime_config: Mapping[str, Any],
    persisted_config: Mapping[str, Any],
    *,
    environment: Mapping[str, str] | None = None,
    config_path: str = "",
    persistence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Produce the single report of "value as saved / value in effect / overridden by
    the environment"."""
    env = environment if environment is not None else os.environ
    deployment = persisted_config.get("deployment")
    persisted_deployment = deployment if isinstance(deployment, Mapping) else {}
    # Someone who skipped the /onboarding wizard but did save once from the dashboard's
    # MCP-auth panel will have mcp_require_auth (or mcp_auth_mode) explicitly present in
    # config.yaml. That key is the evidence of a deliberate choice, so they must not be
    # treated as "never configured" and nagged to run the wizard again.
    manual_auth_configured = (
        "mcp_require_auth" in persisted_config or "mcp_auth_mode" in persisted_config
    )
    effective_auth = _as_bool(runtime_config.get("mcp_require_auth"), default=True)
    saved_auth = (
        _as_bool(persisted_config.get("mcp_require_auth"), default=effective_auth)
        if "mcp_require_auth" in persisted_config
        else effective_auth
    )
    effective_auth_mode = str(runtime_config.get("mcp_auth_mode") or "oauth").strip().lower()
    if effective_auth_mode not in {"oauth", "token"}:
        effective_auth_mode = "oauth"
    saved_auth_mode = (
        str(persisted_config.get("mcp_auth_mode") or "oauth").strip().lower()
        if "mcp_auth_mode" in persisted_config
        else effective_auth_mode
    )
    if saved_auth_mode not in {"oauth", "token"}:
        saved_auth_mode = "oauth"
    environment_sources: list[dict[str, str]] = []
    source_map = {
        "LOCI_MCP_REQUIRE_AUTH": "mcp_require_auth",
        "LOCI_MCP_AUTH_MODE": "mcp_auth_mode",
        "LOCI_TRANSPORT": "transport",
        "LOCI_CONFIG_PATH": "config_path",
        "LOCI_BUCKETS_DIR": "buckets_dir",
        "LOCI_VAULT_DIR": "buckets_dir",
        "LOCI_BIND_HOST": "bind_host",
    }
    for env_name, field in source_map.items():
        value = str(env.get(env_name, "") or "").strip()
        if value:
            environment_sources.append({"env": env_name, "field": field, "value": value})
    effective_transport = str(runtime_config.get("transport") or "stdio")
    saved_transport = (
        str(persisted_config.get("transport") or effective_transport)
        if "transport" in persisted_config
        else effective_transport
    )
    effective_public_url = configured_public_origin(runtime_config)
    saved_public_url = (
        configured_public_origin(persisted_config)
        if isinstance(deployment, Mapping)
        else effective_public_url
    )
    overrides: list[dict[str, str]] = []
    for source in environment_sources:
        field = source["field"]
        if field == "mcp_require_auth" and saved_auth != effective_auth:
            overrides.append(source)
        elif field == "mcp_auth_mode" and saved_auth_mode != effective_auth_mode:
            overrides.append(source)
        elif field == "transport" and saved_transport != effective_transport:
            overrides.append(source)
    return {
        "profile": str(persisted_deployment.get("profile") or "unconfigured"),
        "onboarding_completed": bool(persisted_deployment.get("onboarding_completed")),
        "manual_auth_configured": manual_auth_configured,
        "config_path": config_path,
        "saved": {
            "transport": saved_transport,
            "mcp_require_auth": saved_auth,
            "mcp_auth_mode": saved_auth_mode,
            "public_url": saved_public_url,
        },
        "effective": {
            "transport": effective_transport,
            "mcp_require_auth": effective_auth,
            "mcp_auth_mode": effective_auth_mode,
            "public_url": effective_public_url,
            "buckets_dir": str(runtime_config.get("buckets_dir") or ""),
            # Report-only: reflects server.py's own LOCI_BIND_HOST default for
            # display in the diagnostics report; this module never opens a socket.
            "bind_host": str(env.get("LOCI_BIND_HOST", "") or "0.0.0.0"),  # nosec B104
        },
        "overrides": overrides,
        "environment_sources": environment_sources,
        "restart_required": (
            saved_auth != effective_auth
            or saved_auth_mode != effective_auth_mode
            or saved_transport != effective_transport
            or saved_public_url != effective_public_url
        ),
        "persistence": dict(persistence or {}),
    }
