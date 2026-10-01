"""
========================================
web/config_api.py — engine config / API-key tests / model listing (four routes kept, seven dropped)
========================================

This file used to hold nine routes, and two separate doorways governed the same settings:
both `/api/config` and `/api/env-config` could edit the compress/embed fields. That was
the source of the twin-doorway trap — hitting save on a stale, unrefreshed page wrote the
old values straight back over the new ones. **The current panel keeps exactly one
doorway**:

- /api/config (GET/POST): read runtime config and hot-update it (including hot-swapping
  the embedding backend). `config.yaml` is the single source of truth.
- /api/test/dehydration, /api/test/embedding: connectivity self-tests for compression and
  vectorization.
- /api/models: list the models available from the target provider.

The seven that were dropped (`/dashboard`, `/api/env-vars`, `/api/env-config` GET+POST,
`/api/mcp-token/regenerate`, `/api/transport`) each have a comment at the point of
deletion.
These four are no longer authenticated at this layer either — the `sh._require_auth` gate
was removed, matching the rest of the panel routes.

Public surface: register(mcp).
========================================
"""

import os
import sys
from collections.abc import Mapping

import httpx

from starlette.requests import Request
from starlette.responses import Response

from locibrain.security.deployment_profile import normalize_public_https_origin
from locibrain.security.public_origin import configured_public_origin

from . import _shared as sh

try:
    from utils import (  # type: ignore
        get_ai_name as _get_ai_name,
        get_owner_name as _get_owner_name,
        get_owner_count as _get_owner_count,
        positive_float as _positive_float,
        parse_bool as _parse_bool,
        atomic_update_config_yaml,
        read_config_yaml,
    )
except ImportError:  # pragma: no cover
    from ..utils import (  # type: ignore
        get_ai_name as _get_ai_name,
        get_owner_name as _get_owner_name,
        get_owner_count as _get_owner_count,
        positive_float as _positive_float,
        parse_bool as _parse_bool,
        atomic_update_config_yaml,
        read_config_yaml,
    )

logger = sh.logger
_MAX_PROVIDER_KEY_CHARS = 8192
_MAX_PROVIDER_URL_CHARS = 2048
_MAX_PROVIDER_FORMAT_CHARS = 64
_MAX_ENV_VALUE_CHARS = 8192


def _rebuild_embedding_runtime():
    """Rebuild and publish one embedding engine to every runtime holder."""
    try:
        from core.embedding_engine import EmbeddingEngine  # type: ignore
    except ImportError:  # pragma: no cover
        from ..core.embedding_engine import EmbeddingEngine  # type: ignore

    engine = EmbeddingEngine(sh.config)
    sh.replace_embedding_engine(engine)
    return engine


def _mcp_auth_mode(config: Mapping[str, object] | object) -> str:
    """Normalize one config snapshot's mutually exclusive MCP auth mode."""
    raw = (
        str(config.get("mcp_auth_mode", "oauth")).strip().lower()
        if isinstance(config, Mapping)
        else "oauth"
    )
    return raw if raw in ("oauth", "token") else "oauth"


def _current_mcp_token() -> str:
    """Live static MCP token — env wins over config.yaml, same priority as validation."""
    return (
        os.environ.get("LOCI_MCP_TOKEN", "").strip()
        or str(sh.config.get("mcp_token", "") or "").strip()
    )


def _mask_mcp_token(token: str) -> str | None:
    if not token:
        return None
    if len(token) <= 8:
        return "***"
    return f"{token[:4]}...{token[-4:]}"


def _panel_locked() -> bool:
    """Whether this screen is actually locked right now — the switch *and* a password
    both have to be in place."""
    try:
        from . import panel_auth
        return bool(panel_auth.gate_needed())
    except Exception:      # noqa: BLE001
        return False


def register(mcp) -> None:
    # MCP auth is bound into middleware and OAuth route visibility at process
    # startup. Keep the effective value separate from the desired persisted
    # value so the Dashboard cannot falsely claim a hot switch took effect.
    runtime_mcp_auth_required = _parse_bool(
        sh.config.get("mcp_require_auth", True), default=True
    )
    runtime_mcp_auth_mode = _mcp_auth_mode(sh.config)
    runtime_transport = str(sh.config.get("transport") or "stdio")
    # deployment.public_url participates in OAuth resource/audience binding and
    # is a startup snapshot too.  Keep a separate desired value for Dashboard
    # round-trips; publishing it into sh.config before restart would split the
    # already-bound OAuth routes from MCP middleware.
    runtime_public_url = configured_public_origin(sh.config)

    def _desired_startup_state(persisted: Mapping[str, object]) -> dict[str, object]:
        persisted_deployment = persisted.get("deployment")
        has_persisted_deployment = isinstance(persisted_deployment, Mapping)
        return {
            "transport": str(persisted.get("transport") or runtime_transport)
            if "transport" in persisted
            else runtime_transport,
            "mcp_require_auth": _parse_bool(
                persisted.get("mcp_require_auth"), default=runtime_mcp_auth_required
            )
            if "mcp_require_auth" in persisted
            else runtime_mcp_auth_required,
            "mcp_auth_mode": _mcp_auth_mode(persisted)
            if "mcp_auth_mode" in persisted
            else runtime_mcp_auth_mode,
            "public_url": configured_public_origin(persisted)
            if has_persisted_deployment
            else runtime_public_url,
        }

    # Four kept, seven dropped: `/dashboard` (the page itself), `/api/env-vars` and
    # `/api/env-config` (the twin-doorway trap — two places governing one setting, so
    # deleting it fixed a real bug as a side effect), `/api/mcp-token/regenerate` (auth
    # went, so the token is no longer needed), and `/api/transport` (transport is fixed at
    # http). Seven routes gone entirely. The panel keeps one doorway and `config.yaml` is
    # the single source of truth. The four survivors follow below, and `/api/*` is no
    # longer authenticated at this layer — the `sh._require_auth` gate came out with the
    # rest.

    @mcp.custom_route("/api/config", methods=["GET"])
    async def api_config_get(request: Request) -> Response:
        """Get current runtime config (safe fields only, API key masked)."""
        from starlette.responses import JSONResponse
        try:
            desired = _desired_startup_state(read_config_yaml())
        except (OSError, ValueError) as exc:
            logger.error("读取持久化启动配置失败: %s", exc)
            return JSONResponse(
                {"error": f"failed to read persisted config: {exc}"},
                status_code=500,
            )
        dehy = sh.config.get("dehydration", {})
        emb = sh.config.get("embedding", {})
        api_key = dehy.get("api_key", "")
        masked_key = f"{api_key[:4]}...{api_key[-4:]}" if len(api_key) > 8 else ("***" if api_key else "")
        return JSONResponse({
            "dehydration": {
                "model": dehy.get("model", ""),
                "base_url": dehy.get("base_url", ""),
                "api_key_masked": masked_key,
                "max_tokens": dehy.get("max_tokens", 1024),
                "temperature": dehy.get("temperature", 0.1),
                "api_format": dehy.get("api_format", "openai_compat"),
                "timeout_seconds": dehy.get("timeout_seconds", 60),
            },
            "embedding": {
                "enabled": _parse_bool(emb.get("enabled", False), default=False),
                "model": emb.get("model", ""),
                "api_format": emb.get("api_format", "openai_compat"),
                "timeout_seconds": emb.get("timeout_seconds", 30),
                "backend": "api",
                "backend_options": [
                    {"value": "api", "label": "Gemini API（云端）", "note": "需填 LOCI_EMBED_API_KEY，3072 维质量最高，需联网；客户端几乎不占额外内存"},
                ],
            },
            "surfacing": {
                "breath_max_results": int(sh.config.get("surfacing", {}).get("breath_max_results") or 20),
                "breath_max_tokens": int(sh.config.get("surfacing", {}).get("breath_max_tokens") or 10000),
                "feel_max_tokens": int(sh.config.get("surfacing", {}).get("feel_max_tokens") or 6000),
            },
            "merge_threshold": sh.config.get("merge_threshold", 75),
            # The panel lock: the switch itself, plus **whether it is actually locked**
            # (with no password set, the switch can be on and still lock nothing).
            "panel_auth": _parse_bool(sh.config.get("panel_auth", True), default=True),
            "panel_locked": _panel_locked(),
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "buckets_dir": sh.config.get("buckets_dir", ""),
            # The MCP OAuth switch. Default true (OAuth enforced). The front-end's MCP
            # connection panel renders a toggle from it; turned off, /mcp accepts
            # unauthenticated direct connections (for a self-hosted front-end or other
            # clients).
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            # Auth mode, meaningful only when mcp_require_auth=true: "oauth" (default) or
            # "token". The two are mutually exclusive.
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            # Static-token status: report only the mask and whether one is configured.
            # Never the plaintext.
            "mcp_token_configured": bool(_current_mcp_token()),
            "mcp_token_hint": _mask_mcp_token(_current_mcp_token()),
            # The public MCP URL is start-time configuration for the OAuth
            # resource/audience. Both the saved value and this process's actual value are
            # returned, so the UI cannot pretend a hot switch succeeded when it did not.
            "deployment": {
                "public_url": desired["public_url"],
                "public_url_effective": runtime_public_url,
            },
            "restart_required": (
                desired["mcp_require_auth"] != runtime_mcp_auth_required
                or desired["mcp_auth_mode"] != runtime_mcp_auth_mode
                or desired["transport"] != runtime_transport
                or desired["public_url"] != runtime_public_url
            ),
            # Deployment info: data directory, port, whether we are inside a container.
            # Shown in the front-end's system section; the port is editable.
            "host_port": sh.config.get("host_port"),
            "in_docker": sh.in_docker(),
            # Display name for the AI side, from the AI_NAME environment variable,
            # falling back to "AI". Read-only for the front-end; used in user-facing
            # copy such as delete confirmations.
            "ai_name": _get_ai_name(),
            # Memory ownership: when several people share one store, this says whose
            # memories these are. The front-end only shows the ownership badge when
            # owner_count >= 2, so a single user is never bothered by it; owner_name is
            # the badge text. Both read-only.
            "owner_name": _get_owner_name(),
            "owner_count": _get_owner_count(),
        })


    @mcp.custom_route("/api/config", methods=["POST"])
    async def api_config_update(request: Request) -> Response:
        """Hot-update runtime sh.config. Optionally persist to config.yaml."""
        from starlette.responses import JSONResponse
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": "invalid JSON"}, status_code=400)
        if not isinstance(body, dict):
            return JSONResponse({"error": "JSON body must be an object"}, status_code=400)

        updated = []
        try:
            persist_requested = _parse_bool(body.get("persist", False))
            mcp_auth_value = (
                _parse_bool(body["mcp_require_auth"])
                if "mcp_require_auth" in body
                else None
            )
            mcp_auth_mode_value = None
            if "mcp_auth_mode" in body:
                mcp_auth_mode_value = str(body["mcp_auth_mode"]).strip().lower()
                if mcp_auth_mode_value not in ("oauth", "token"):
                    return JSONResponse(
                        {"error": "mcp_auth_mode must be 'oauth' or 'token'"},
                        status_code=400,
                    )
            embedding_payload = body.get("embedding")
            if "embedding" in body and not isinstance(embedding_payload, dict):
                return JSONResponse(
                    {"error": "embedding must be an object"}, status_code=400
                )
            if "dehydration" in body and not isinstance(
                body.get("dehydration"), dict
            ):
                return JSONResponse(
                    {"error": "dehydration must be an object"}, status_code=400
                )
            if "surfacing" in body and not isinstance(body.get("surfacing"), dict):
                return JSONResponse(
                    {"error": "surfacing must be an object"}, status_code=400
                )
            deployment_payload = body.get("deployment")
            if "deployment" in body and not isinstance(deployment_payload, dict):
                return JSONResponse(
                    {"error": "deployment must be an object"}, status_code=400
                )
            deployment_public_url = None
            if isinstance(deployment_payload, dict) and "public_url" in deployment_payload:
                raw_public_url = str(deployment_payload["public_url"] or "").strip()
                deployment_public_url = ""
                if raw_public_url:
                    deployment_public_url = normalize_public_https_origin(
                        raw_public_url
                    )
                    if not deployment_public_url:
                        return JSONResponse(
                            {
                                "error": (
                                    "deployment.public_url must be an HTTPS domain "
                                    "or complete /mcp URL"
                                )
                            },
                            status_code=400,
                        )
            embedding_enabled = (
                _parse_bool(embedding_payload["enabled"])
                if isinstance(embedding_payload, dict)
                and "enabled" in embedding_payload
                else None
            )
            embedding_backend = None
            if isinstance(embedding_payload, dict) and "backend" in embedding_payload:
                backend_raw = str(embedding_payload["backend"]).strip().lower()
                embedding_backend = (
                    "api" if backend_raw in ("api", "gemini") else backend_raw
                )
                if embedding_backend != "api":
                    return JSONResponse(
                        {"error": f"unsupported embedding backend: {backend_raw}"},
                        status_code=400,
                    )
            sampling_payload = None
            if isinstance(body.get("surfacing"), dict):
                candidate = body["surfacing"].get("sampling")
                if candidate is not None and not isinstance(candidate, dict):
                    return JSONResponse(
                        {"error": "surfacing.sampling must be an object"},
                        status_code=400,
                    )
                sampling_payload = candidate
            sampling_enabled = (
                _parse_bool(sampling_payload["enabled"])
                if isinstance(sampling_payload, dict)
                and "enabled" in sampling_payload
                else None
            )
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        startup_setting_requested = (
            deployment_public_url is not None
            or mcp_auth_value is not None
            or mcp_auth_mode_value is not None
        )
        if startup_setting_requested and not persist_requested:
            return JSONResponse(
                {
                    "error": (
                        "MCP startup settings require persist=true because "
                        "they only take effect after restart"
                    )
                },
                status_code=400,
            )

        # --- Dehydration config ---
        if "dehydration" in body:
            d = body["dehydration"]
            dehy = sh.config.setdefault("dehydration", {})
            for key in ("model", "base_url", "max_tokens", "temperature", "api_format", "timeout_seconds"):
                if key in d:
                    dehy[key] = d[key]
                    updated.append(f"dehydration.{key}")
            if "api_key" in d and d["api_key"]:
                dehy["api_key"] = d["api_key"]
                updated.append("dehydration.api_key")
            # Hot-reload dehydrator — sync ALL attributes so dashboard changes take effect immediately
            sh.dehydrator.model = dehy.get("model", sh.dehydrator.model)
            sh.dehydrator.base_url = dehy.get("base_url", sh.dehydrator.base_url)
            sh.dehydrator.max_tokens = int(dehy.get("max_tokens") or sh.dehydrator.max_tokens)
            sh.dehydrator.temperature = float(dehy.get("temperature") or sh.dehydrator.temperature)
            sh.dehydrator.timeout_seconds = _positive_float(dehy.get("timeout_seconds"), sh.dehydrator.timeout_seconds)
            sh.dehydrator.api_format = dehy.get("api_format", getattr(sh.dehydrator, "api_format", "openai_compat"))
            if "api_key" in d and d["api_key"]:
                sh.dehydrator.api_key = dehy["api_key"]
            sh.dehydrator.api_available = bool(sh.dehydrator.api_key)
            # Rebuild OpenAI-compat client whenever key or url changes
            if sh.dehydrator.api_available and sh.dehydrator.api_format == "openai_compat":
                from openai import AsyncOpenAI
                sh.dehydrator.client = AsyncOpenAI(
                    api_key=sh.dehydrator.api_key,
                    base_url=sh.dehydrator.base_url,
                    timeout=sh.dehydrator.timeout_seconds,
                )
            else:
                sh.dehydrator.client = None

        # --- Embedding config ---
        if "embedding" in body:
            e = embedding_payload
            emb = sh.config.setdefault("embedding", {})
            rebuild_embedding = False
            if embedding_enabled is not None:
                emb["enabled"] = embedding_enabled
                updated.append("embedding.enabled")
                rebuild_embedding = True
            if "model" in e:
                emb["model"] = e["model"]
                updated.append("embedding.model")
                rebuild_embedding = True
            if "base_url" in e:
                emb["base_url"] = str(e["base_url"]).strip()
                updated.append("embedding.base_url")
                rebuild_embedding = True
            if "timeout_seconds" in e:
                emb["timeout_seconds"] = e["timeout_seconds"]
                updated.append("embedding.timeout_seconds")
                rebuild_embedding = True
            if "api_format" in e:
                emb["api_format"] = str(e["api_format"]).strip()
                updated.append("embedding.api_format")
                rebuild_embedding = True
            if embedding_backend is not None:
                emb["backend"] = embedding_backend
                updated.append("embedding.backend")
                rebuild_embedding = True

            # One request may change several fields. Rebuild once, then publish
            # the same instance to web routes, BucketManager, ImportEngine and
            # the MCP tools runtime so reads and writes cannot split models.
            if rebuild_embedding:
                try:
                    _rebuild_embedding_runtime()
                except Exception as e:
                    return JSONResponse(
                        {"error": f"embedding reload failed: {e}"},
                        status_code=400,
                    )

        # --- The panel lock ---
        # Takes effect immediately: gate_needed() re-reads sh.config on every request, so
        # a change here applies at once with no restart.
        # It can only actually lock when **a password already exists** (the hard line
        # inside panel_auth.gate_needed), so no extra validation is needed here: switch on
        # with no password set means the door stays open, and nobody gets locked out.
        if "panel_auth" in body:
            sh.config["panel_auth"] = _parse_bool(body["panel_auth"])
            updated.append("panel_auth")

        # --- The two names ---
        # They are editable in the panel now, so this has to accept them. Besides the
        # in-memory sh.config update, they **must** be written to config.yaml
        # (persist=True): get_ai_name() re-reads the config file every time, so an
        # in-memory-only change would read back stale on the next call.
        for _k, _cap in (("ai_name", 40), ("owner_name", 40)):
            if _k in body:
                _v = str(body.get(_k) or "").strip()[:_cap]
                sh.config[_k] = _v
                updated.append(_k)

        # --- Merge threshold ---
        if "merge_threshold" in body:
            try:
                sh.config["merge_threshold"] = int(body["merge_threshold"])
                updated.append("merge_threshold")
            except (TypeError, ValueError):
                pass

        # The MCP auth switch, the auth mode and the public URL are all start-time
        # snapshots. They are written to config.yaml only, and must not be published early
        # into sh.config — otherwise the OAuth/MCP middleware keeps using its old closure
        # while diagnostics and other routes believe the new value is already live.
        # GET /api/config echoes the desired values from the persisted config and returns
        # the effective values separately.

        # --- The externally visible port (host_port) ---
        # Bare metal: write the config, and the process listens on the new port after its
        # own restart (the front-end's "save and restart").
        # Docker: the in-container port is fixed by the Dockerfile, and host_port is only
        # read by deployment scripts to inject LOCI_HOST_PORT, so the container has to be
        # rebuilt for it to take effect. The front-end says so.
        if "host_port" in body:
            try:
                sh.config["host_port"] = int(body["host_port"])
                updated.append("host_port")
            except (TypeError, ValueError):
                pass

        # --- Surfacing defaults (breath/feel token & result caps) ---
        if "surfacing" in body and isinstance(body["surfacing"], dict):
            sf = sh.config.setdefault("surfacing", {})
            for key, lo, hi in (
                ("breath_max_results", 1, 50),
                ("breath_max_tokens", 500, 20000),
                ("feel_max_tokens", 500, 20000),
            ):
                if key in body["surfacing"]:
                    try:
                        val = int(body["surfacing"][key])
                        sf[key] = max(lo, min(hi, val))
                        updated.append(f"surfacing.{key}")
                    except (TypeError, ValueError):
                        pass

        persisted_after: dict | None = None

        # --- Persist to config.yaml if requested ---
        if persist_requested:
            def _mutate(save_config: dict) -> None:
                if "dehydration" in body:
                    sc_dehy = save_config.setdefault("dehydration", {})
                    if not isinstance(sc_dehy, dict):
                        sc_dehy = {}
                        save_config["dehydration"] = sc_dehy
                    for key in ("model", "base_url", "max_tokens", "temperature", "api_format", "timeout_seconds"):
                        if key in body["dehydration"]:
                            sc_dehy[key] = body["dehydration"][key]
                    # Never persist api_key to yaml (use env var)

                if "embedding" in body:
                    sc_emb = save_config.setdefault("embedding", {})
                    if not isinstance(sc_emb, dict):
                        sc_emb = {}
                        save_config["embedding"] = sc_emb
                    for key in ("model", "base_url", "api_format", "timeout_seconds"):
                        if key in body["embedding"]:
                            sc_emb[key] = body["embedding"][key]
                    if embedding_enabled is not None:
                        sc_emb["enabled"] = embedding_enabled
                    if embedding_backend is not None:
                        sc_emb["backend"] = embedding_backend

                if "panel_auth" in body:
                    save_config["panel_auth"] = _parse_bool(body["panel_auth"])

                for _k in ("ai_name", "owner_name"):
                    if _k in body:
                        save_config[_k] = str(body.get(_k) or "").strip()[:40]

                if "merge_threshold" in body:
                    try:
                        save_config["merge_threshold"] = int(body["merge_threshold"])
                    except (TypeError, ValueError):
                        pass

                if mcp_auth_value is not None:
                    save_config["mcp_require_auth"] = mcp_auth_value

                if mcp_auth_mode_value is not None:
                    save_config["mcp_auth_mode"] = mcp_auth_mode_value

                if "host_port" in body:
                    try:
                        save_config["host_port"] = int(body["host_port"])
                    except (TypeError, ValueError):
                        pass

                if "surfacing" in body and isinstance(body["surfacing"], dict):
                    sc_sf = save_config.setdefault("surfacing", {})
                    if not isinstance(sc_sf, dict):
                        sc_sf = {}
                        save_config["surfacing"] = sc_sf
                    for key in ("breath_max_results", "breath_max_tokens", "feel_max_tokens"):
                        if key in body["surfacing"]:
                            try:
                                sc_sf[key] = int(body["surfacing"][key])
                            except (TypeError, ValueError):
                                pass
                    if "sampling" in body["surfacing"] and isinstance(body["surfacing"]["sampling"], dict):
                        sc_samp = sc_sf.setdefault("sampling", {})
                        if not isinstance(sc_samp, dict):
                            sc_samp = {}
                            sc_sf["sampling"] = sc_samp
                        src_samp = body["surfacing"]["sampling"]
                        if sampling_enabled is not None:
                            sc_samp["enabled"] = sampling_enabled
                        for key in ("top_k", "sample_k"):
                            if key in src_samp:
                                try:
                                    sc_samp[key] = int(src_samp[key])
                                except (TypeError, ValueError):
                                    pass
                        if "temperature" in src_samp:
                            try:
                                sc_samp["temperature"] = float(src_samp["temperature"])
                            except (TypeError, ValueError):
                                pass

                if deployment_public_url is not None:
                    sc_deployment = save_config.get("deployment")
                    if not isinstance(sc_deployment, dict):
                        sc_deployment = {}
                        save_config["deployment"] = sc_deployment
                    if deployment_public_url:
                        sc_deployment["public_url"] = deployment_public_url
                    else:
                        sc_deployment.pop("public_url", None)

            try:
                persisted_after = atomic_update_config_yaml(_mutate)
                updated.append("persisted_to_yaml")
                if mcp_auth_value is not None:
                    updated.append("mcp_require_auth")
                if mcp_auth_mode_value is not None:
                    updated.append("mcp_auth_mode")
                if deployment_public_url is not None:
                    updated.append("deployment.public_url")
            except Exception as e:
                return JSONResponse({"error": f"persist failed: {e}", "updated": updated}, status_code=500)

        desired = _desired_startup_state(
            persisted_after if persisted_after is not None else sh.config
        )
        restart_required = (
            desired["mcp_require_auth"] != runtime_mcp_auth_required
            or desired["mcp_auth_mode"] != runtime_mcp_auth_mode
            or desired["transport"] != runtime_transport
            or desired["public_url"] != runtime_public_url
        )
        return JSONResponse({
            "updated": updated,
            "ok": True,
            "restart_required": restart_required,
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "deployment": {
                "public_url": desired["public_url"],
                "public_url_effective": runtime_public_url,
            },
            "message": (
                "MCP 启动配置已保存，需要重启服务后生效。"
                if restart_required else "设置已生效。"
            ),
        })


    # Dropped: `/api/mcp-token/regenerate`. The static secret behind
    # `mcp_auth_mode="token"` can now only be changed by editing the `mcp_token` field in
    # config.yaml by hand, or by setting the `LOCI_MCP_TOKEN` environment variable
    # (`_is_valid_static_mcp_token` accepts both; env wins). The panel no longer offers a
    # one-click rotation button. `oauth`, the default mode, goes through the authorization
    # page in bridge/oauth.py and is unaffected.

    # =============================================================
    # /api/test/dehydration — check whether the dehydration LLM's API key works
    # =============================================================
    @mcp.custom_route("/api/test/dehydration", methods=["POST"])
    async def api_test_dehydration(request: Request) -> Response:
        from starlette.responses import JSONResponse
        # Use current runtime config (api_key may have been updated in-memory)
        dehyd = sh.config.get("dehydration", {})
        model = dehyd.get("model", "")
        base_url = dehyd.get("base_url", "")
        api_key = dehyd.get("api_key", "")
        if not api_key:
            return JSONResponse({"ok": False, "error": "未设置 API Key"}, status_code=400)
        try:
            import httpx as _httpx
            headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
            payload = {"model": model, "messages": [{"role": "user", "content": "hi"}], "max_tokens": 5}
            async with _httpx.AsyncClient(timeout=15) as client:
                r = await client.post(f"{base_url.rstrip('/')}/chat/completions", json=payload, headers=headers)
            if r.status_code in (200, 201):
                return JSONResponse({"ok": True, "message": "API Key 有效 ✓"})
            else:
                try:
                    detail = r.json().get("error", {})
                    msg = detail.get("message", r.text[:200]) if isinstance(detail, dict) else str(detail)[:200]
                except Exception:
                    msg = r.text[:200]
                return JSONResponse({"ok": False, "error": f"HTTP {r.status_code}: {msg}"})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})


    # =============================================================
    # /api/test/embedding — check whether embedding really works
    # Only compression used to be testable; there was no way to verify vectorization, so
    # "compression fine, vectorization silently failing" was completely invisible to the
    # user. This actually issues one embedding request and reports success or failure to
    # the front-end as it happened.
    # =============================================================
    @mcp.custom_route("/api/test/embedding", methods=["POST"])
    async def api_test_embedding(request: Request) -> Response:
        from starlette.responses import JSONResponse
        eng = sh.embedding_engine  # read the global; it is correctly rebuilt after a config save
        if not getattr(eng, "enabled", False) or getattr(eng, "_backend", None) is None:
            return JSONResponse({
                "ok": False,
                "error": "向量化未启用或缺 key（standby）。请填入 Embedding API Key 点「保存」后再测。",
            })
        try:
            vec = await eng._generate_async("connectivity probe / 连接性探针")
        except Exception as e:
            return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"[:300]})
        if vec:
            model = getattr(eng, "model", "") or (
                eng._backend.model_name() if getattr(eng, "_backend", None) else "?"
            )
            return JSONResponse({
                "ok": True,
                "message": f"向量化连接成功 ✓（模型 {model}，维度 {len(vec)}）",
            })
        return JSONResponse({
            "ok": False,
            "error": "调用返回空向量：检查 model 名 / base_url / key 是否匹配该 provider"
                     "（如硅基流动 base_url=https://api.siliconflow.cn/v1、model=BAAI/bge-m3）。详见错误面板 OB-E001。",
        })


    # =============================================================
    # /api/models — list the models a provider offers, for the panel's model picker
    # POST Body: {api_key, base_url, api_format}
    # Supports three formats: openai_compat / gemini / anthropic
    # =============================================================
    @mcp.custom_route("/api/models", methods=["POST"])
    async def api_list_models(request: Request) -> Response:
        from starlette.responses import JSONResponse
        try:
            body = await sh._read_json_object(request)
        except Exception:
            return JSONResponse({"ok": False, "error": "invalid JSON"}, status_code=400)

        provider_fields = ("api_key", "base_url", "api_format")
        if any(key in body and not isinstance(body[key], str) for key in provider_fields):
            return JSONResponse({"ok": False, "error": "provider fields must be strings"}, status_code=400)
        api_key = str(body.get("api_key", "")).strip()
        base_url = str(body.get("base_url", "")).strip()
        api_format = str(body.get("api_format", "openai_compat")).strip().lower()
        if (
            len(api_key) > _MAX_PROVIDER_KEY_CHARS
            or len(base_url) > _MAX_PROVIDER_URL_CHARS
            or len(api_format) > _MAX_PROVIDER_FORMAT_CHARS
        ):
            return JSONResponse({"ok": False, "error": "provider configuration is too large"}, status_code=400)

        # Sentinel "__use_current__": use server-side key from dehydration config
        if api_key == "__use_current__":
            api_key = sh.config.get("dehydration", {}).get("api_key", "")
            if not base_url:
                base_url = sh.config.get("dehydration", {}).get("base_url", "")
            if not api_format or api_format == "openai_compat":
                api_format = sh.config.get("dehydration", {}).get("api_format", "openai_compat")
        # Sentinel "__use_current_embed__": use server-side key from embedding config
        if api_key == "__use_current_embed__":
            api_key = sh.config.get("embedding", {}).get("api_key", "")
            if not base_url:
                base_url = sh.config.get("embedding", {}).get("base_url", "")

        if not api_key:
            return JSONResponse({"ok": False, "error": "需要 api_key（请先保存 API Key 或在输入框填入）"}, status_code=400)

        try:
            models: list[str] = []
            if api_format in ("gemini", "gemini_embed"):
                # gemini → generateContent models；gemini_embed → embedContent models
                method_filter = "embedContent" if api_format == "gemini_embed" else "generateContent"
                url = "https://generativelanguage.googleapis.com/v1beta/models"
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(
                        url,
                        params={"pageSize": 200},
                        headers={"x-goog-api-key": api_key},
                    )
                r.raise_for_status()
                for m in r.json().get("models", []):
                    if method_filter in m.get("supportedGenerationMethods", []):
                        models.append(m.get("name", "").replace("models/", ""))
            elif api_format == "anthropic":
                ant_base = base_url.rstrip("/") if base_url else "https://api.anthropic.com"
                headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(f"{ant_base}/v1/models", headers=headers)
                r.raise_for_status()
                models = [m.get("id", "") for m in r.json().get("data", []) if m.get("id")]
            else:  # openai_compat
                if not base_url:
                    return JSONResponse({"ok": False, "error": "openai_compat 格式需要 base_url"}, status_code=400)
                headers_oai = {"Authorization": f"Bearer {api_key}"}
                async with httpx.AsyncClient(timeout=10.0) as c:
                    r = await c.get(f"{base_url.rstrip('/')}/models", headers=headers_oai)
                r.raise_for_status()
                models = sorted(m.get("id", "") for m in r.json().get("data", []) if m.get("id"))
            return JSONResponse({"ok": True, "models": [m for m in models if m]})
        except Exception as e:
            return JSONResponse({"ok": False, "error": str(e)[:300]})

    # Dropped: `/api/env-config` (GET+POST) entirely. It governed the same settings as
    # `/api/config` — the compress/embed fields were editable from both sides — and that
    # was the source of the twin-doorway trap: hitting save on a stale, unrefreshed page
    # wrote the old values back over the new ones. Deleting it fixed a real bug as a side
    # effect. The panel keeps `/api/config` as its one doorway, and `config.yaml` is the
    # single source of truth.
    # `/api/transport` is gone too: transport is fixed at http, and the `transport` key in
    # `config.yaml` gets no hot-switch entry point. Changing transport means editing
    # config.yaml or the environment by hand and restarting.
