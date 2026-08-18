"""
========================================
deployment_profile.py — 部署模式与安全默认的纯领域规则
========================================

把“本机 / 公网安全 / 高级自定义”三种用户选择归一化为明确配置，并校验
公网匿名暴露、传输方式和 OAuth 等安全不变量。

不做什么：不读写文件、不注册 HTTP 路由、不修改环境变量、不重启服务。
对外暴露：profile_catalog()、build_profile_patch()、validate_profile_patch()、
effective_configuration_report()。
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
    """返回前端可展示的三种模式；安全含义只在这里定义一次。"""
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
    """归一化部署模式标识；未知值显式报错，不静默猜测。"""
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
    """严格解析向导布尔值，拒绝把字符串 false 当作真。"""
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def build_profile_patch(profile: Any, options: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """把用户选择转换成可写入 config.yaml 的最小补丁。"""
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
    """返回阻止保存的安全问题；空列表表示可以落盘。"""
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
    """生成“已保存值 / 实际值 / 环境覆盖”的单一报告。"""
    env = environment if environment is not None else os.environ
    deployment = persisted_config.get("deployment")
    persisted_deployment = deployment if isinstance(deployment, Mapping) else {}
    # 未走 /onboarding 向导、但已经在 Dashboard「MCP 鉴权」面板里手动保存过一次的用户，
    # config.yaml 里会显式出现 mcp_require_auth（或 mcp_auth_mode）键——这是他们做过
    # 主动选择的证据，不该被当成「从没配置过」持续提醒重新走向导。
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
