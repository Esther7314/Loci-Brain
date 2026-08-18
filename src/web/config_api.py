"""
========================================
web/config_api.py — 引擎配置 / API Key 测试 / 模型列表（E1 留 4 删 7 之后）
========================================

E1（2026-08-17）之前这里有 9 条路由、两个入口管着同一份设置（`/api/config`
跟 `/api/env-config` 都能改 compress/embed 字段）——那是 7-27 双入口坑的源头：
在没刷新的旧页面点保存就把旧值写回去。**新面板只留一个入口**：

- /api/config (GET/POST)：运行期配置读取 / 热更新（含 embedding 热替换），
  `config.yaml` 是唯一真相。
- /api/test/dehydration、/api/test/embedding：压缩 / 向量化连通性自检。
- /api/models：列目标 provider 可用模型。

砍掉的 7 条（`/dashboard`、`/api/env-vars`、`/api/env-config` GET+POST、
`/api/mcp-token/regenerate`、`/api/transport`）见各自删除处的注释。
E2 之后这四条也不再鉴权（`sh._require_auth` 那道闸摘了，跟面板其它路由一致）。

对外暴露：register(mcp)。
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

    # 🔴 E1（2026-08-17，留 4 删 7）：`/dashboard`（页面本体）、`/api/env-vars` +
    # `/api/env-config`（7-27 双入口坑的源头：跟「引擎」两处管同一个设置，删掉=
    # 顺手修一个真 bug）、`/api/mcp-token/regenerate`（auth 砍了 token 不需要了）、
    # `/api/transport`（固定 http）——七条整个删了。新面板只留一个入口，
    # `config.yaml` 是唯一真相。四条留的下面接着走，且 E2 之后 `/api/*` 不再
    # 鉴权，`sh._require_auth` 那道闸也一起摘了。

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
            "transport": desired["transport"],
            "transport_effective": runtime_transport,
            "buckets_dir": sh.config.get("buckets_dir", ""),
            # MCP OAuth 鉴权开关。默认 true（强制 OAuth）。前端「⑥ MCP 连接」面板用它
            # 渲染一键开关；关掉后 /mcp 免认证直连（供自有前端 / GPT / GLM 等）。
            "mcp_require_auth": desired["mcp_require_auth"],
            "mcp_require_auth_effective": runtime_mcp_auth_required,
            # 鉴权模式（仅 mcp_require_auth=true 时有意义）："oauth"（默认）或 "token"，二者互斥。
            "mcp_auth_mode": desired["mcp_auth_mode"],
            "mcp_auth_mode_effective": runtime_mcp_auth_mode,
            # 静态 Token 状态：只回掩码/是否已配置，绝不回明文。
            "mcp_token_configured": bool(_current_mcp_token()),
            "mcp_token_hint": _mask_mcp_token(_current_mcp_token()),
            # Dashboard 的公网 MCP 地址是 OAuth resource/audience 的启动期
            # 配置；同时回传已保存值与本进程实际值，避免假装热切换成功。
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
            # 部署信息：数据目录 + 端口 + 是否容器内。前端「系统」区展示，端口可改。
            "host_port": sh.config.get("host_port"),
            "in_docker": sh.in_docker(),
            # AI 一方的显示名（取自环境变量 AI_NAME，回退 "AI"）。前端只读，用于
            # 面向用户的文案（如删除确认、信件署名占位）。
            "ai_name": _get_ai_name(),
            # 记忆归属：多人共用一套 OB 时标明「这份记忆是谁的」。owner_count>=2 时
            # 前端顶部才显示归属徽标（单人不打扰）；owner_name 为徽标文字。均只读。
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

        # --- Merge threshold ---
        if "merge_threshold" in body:
            try:
                sh.config["merge_threshold"] = int(body["merge_threshold"])
                updated.append("merge_threshold")
            except (TypeError, ValueError):
                pass

        # MCP 鉴权开关、鉴权模式与公网地址都是启动期快照。它们只写入
        # config.yaml，不能提前发布到 sh.config；否则 OAuth/MCP 中间件仍使用
        # 旧闭包，而诊断与其他路由却会误以为新值已经生效。GET /api/config 会从
        # 持久配置回显 desired 值，并单独返回 effective 值。

        # --- 对外端口（host_port）---
        # 裸机：写 config 后进程自重启即监听新端口（前端「保存并重启」）。
        # Docker：容器内端口由 Dockerfile 固定，host_port 仅供部署脚本读取注入
        # LOCI_HOST_PORT，须重建容器才生效（前端会提示）。
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


    # 🔴 E1 删 7：`/api/mcp-token/regenerate` 砍了——`mcp_auth_mode="token"` 那颗
    # 静态密钥现在只能手改 config.yaml 的 `mcp_token` 字段、或设 `LOCI_MCP_TOKEN`
    # 环境变量（`_is_valid_static_mcp_token` 两条路都认，env 优先级更高）。
    # 面板不再提供一键轮换按钮——`oauth`（默认模式）走 bridge/oauth.py 的授权页，
    # 不受影响。

    # =============================================================
    # /api/test/dehydration — 测试脱水 LLM API Key 是否可用
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
    # /api/test/embedding — 测试向量化 Embedding 是否真的可用
    # 之前只有脱水(compress)能测，向量化无从验证 → 用户「压缩正常但向量化静默失败」
    # 时完全无感。这里实际发一次 embedding 请求，把成功/失败如实回给前端。(#2/#3)
    # =============================================================
    @mcp.custom_route("/api/test/embedding", methods=["POST"])
    async def api_test_embedding(request: Request) -> Response:
        from starlette.responses import JSONResponse
        eng = sh.embedding_engine  # 读全局（Fix: env-sh.config 保存后已正确重建）
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
    # /api/models — 获取 LLM provider 可用模型列表（供 Dashboard 模型选择器使用）
    # POST Body: {api_key, base_url, api_format}
    # 支持 openai_compat / gemini / anthropic 三种格式
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

    # 🔴 E1 删 7：`/api/env-config`（GET+POST）整个砍了——它跟 `/api/config` 两处
    # 管同一份设置（compress/embed 那几个字段两边都能改），这是 7-27 双入口坑的
    # 源头：在没刷新的旧页面点保存就把旧值写回去。删掉 = 顺手修一个真 bug。
    # 新面板只留 `/api/config` 这一个入口，`config.yaml` 是唯一真相。
    # `/api/transport` 也砍了（固定 http：`config.yaml` 里 `transport` 这单不留
    # 热切换入口，改传输模式回去手改 config.yaml / env 再重启）。
