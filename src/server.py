"""
========================================
server.py — the MCP service entry point and startup assembly
========================================

Starts the whole Loci Brain process: load config, construct BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine, inject them into core.runtime and
web._shared, mount the MCP tools, and bring up the transport.

Key behaviour:
- Once running, it exposes **seven** MCP tools: breath, grow, recall, regrow, fold, muse,
  trace. Each tool's face (signature, description, the one forwarding call) is
  server_tools/<tool>.py; the real implementations live under src/tools/<tool>/. This file
  mounts them in the table below, then runs each face's adapt() and the strict-argument
  sweep.
  `pulse` is implemented under `tools/pulse/` but is not an MCP tool; the panel reaches it
  through `GET /api/loci/pulse`.
- Every tool call goes through server_call._with_notice (who is calling, the three log
  phases, notices, OB-E004, the poke line); this file binds it to `mcp` and the store.
- Every dashboard and HTTP route has been split out into src/web/<domain>.py, each module
  exposing register(mcp). This file only calls web.register_all(mcp) at startup; the
  shared dependencies are in web/_shared.py.
- Still here: process startup, engine initialization, webhook delivery, and bringing up
  uvicorn (the HTTP app with its MCP Bearer auth middleware is server_app.py).

What this does NOT do (the boundary):
- No business logic for the individual tools; all of that lives under tools/*.
- No dream tool. Dream weaving is `core/_dream.py`, and **it has no MCP tool surface**:
  it weaves in the background after waking, dreams are fetched over HTTP (web/loci_dream.py),
  and how a dream reaches the conversation is the bridge's problem.
- No `seed` tool (the thirteen emotional roots): see the note at the mount table.
- No HTTP route handlers (they are all under web/*), and no LLM prompts (dehydrator owns
  those).
- No direct reads or writes of bucket files (bucket_manager owns those).

Public surface: the instance mcp, the mounted tool functions (breath, grow, recall, fold,
muse, regrow, trace), and `_with_notice`.
HTTP routes are in src/web/*.
========================================
"""

import os
import sys
import logging
import time

import httpx


# --- Ensure same-directory modules can be imported ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from core.bucket_manager import BucketManager
from core.dehydrator import Dehydrator
from core.decay_engine import DecayEngine
from core.embedding_engine import EmbeddingEngine
from core.embedding_outbox import EmbeddingOutbox
from core.import_memory import ImportEngine
from core.strict_args import harden as _harden_tool
from core.package_import import MigrateEngine
from core import runtime as _core_runtime
from utils import get_version, load_config, setup_logging

# --- The MCP tools: each face is server_tools/<tool>.py, each implementation tools/<tool>/ ---
from server_tools import breath as _face_breath
from server_tools import grow as _face_grow
from server_tools import recall as _face_recall
from server_tools import regrow as _face_regrow
from server_tools import fold as _face_fold
from server_tools import muse as _face_muse
from server_tools import trace as _face_trace
import server_call as _call
from server_call import (  # noqa: F401 — _with_notice and _run_with_notice are reached on server
    _hosts,
    _pop_deletion_notice,
    _run_with_notice,
    _with_notice,
    _write_deletion_notice,
)

# --- Load config & init logging ---
config = load_config()
setup_logging(config.get("log_level", "INFO"))
logger = logging.getLogger("loci_brain")

# --- Project version (read from <repo_root>/VERSION) ---
# get_version() gathers the file reads and the fallback logic.
# Assigning to the dunder `__version__` is the Python community's conventional name for a
# module's version field.
__version__ = get_version()
logger.info(f"Loci Brain v{__version__}")

# --- Legacy path migration check ---
# The situation: early users ran `python server.py` from the project root; after the
# reorganization it is `python src/server.py`. This only **detects and warns**. It performs
# no destructive action of any kind.
# load_config() still defaults buckets_dir to <repo_root>/buckets, so no old data is lost.
#
# Notes:
#   * a leading `_` on a name is a convention for "module-internal", not enforced syntax
#   * there is no for/else here; an early break is used instead
#   * `os.path.isdir(p) and any(...)` short-circuits: a False on the left skips the listdir
try:
    _bd = config.get("buckets_dir", "")
    if _bd and os.path.isdir(_bd):
        _has_data = False
        # Walk each bucket directory; a .md file anywhere inside one (including its domain
        # subdirectories) means there is data.
        # os.walk has to recurse: buckets are stored under domain subdirectories
        # (permanent/<domain>/x.md), so an os.listdir of the top level sees only domain
        # folders and always concludes "empty" -> a false "fresh install" report. The data
        # is all still there and breath still reads it; the log line is just alarming.
        for sub in ("permanent", "dynamic", "feel"):
            p = os.path.join(_bd, sub)
            if not os.path.isdir(p):
                continue
            if any(
                f.endswith(".md") and not f.startswith(".")
                for _root, _dirs, _files in os.walk(p)
                for f in _files
            ):
                _has_data = True
                break
        if _has_data:
            logger.info(f"[migration] existing buckets detected at {_bd} — zero data loss expected.")
        else:
            logger.info(f"[migration] {_bd} is empty — fresh install assumed.")
except Exception as _e:  # pragma: no cover - defensive
    # No startup check may prevent the service from coming up; log a warning and move on.
    logger.warning(f"[migration] check skipped: {_e}")

# --- Runtime env vars (port + webhook) ---
# LOCI_PORT: the HTTP/SSE listen port, default 18001.
# Docker: compose sets LOCI_PORT=8000 explicitly to keep the in-container port at 8000, and
# a host port mapping of 18001:8000 exposes 18001. Bare metal listens on 18001 directly.
# Priority: env LOCI_PORT (fixed at 8000 by the Dockerfile under Docker) > config.yaml
# host_port (editable from the front-end on bare metal, saved straight to config) >
# the 18001 default. Under Docker, changing host_port from the front-end does not affect
# what the container listens on (still 8000); the externally visible port is decided by the
# host mapping LOCI_HOST_PORT, which the deployment script reads from config and injects.
try:
    _port_raw = os.environ.get("LOCI_PORT") or str(config.get("host_port") or "") or "18001"
    LOCI_PORT = int(_port_raw)
except (ValueError, TypeError):
    logger.warning("端口配置不是合法整数，回退到 18001")
    LOCI_PORT = 18001

# Docker needs an all-interface default; bare-metal deployments can restrict it
# with LOCI_BIND_HOST=127.0.0.1.
_BIND_HOST = (os.environ.get("LOCI_BIND_HOST") or "0.0.0.0").strip() or "0.0.0.0"  # nosec B104

# LOCI_HOOK_URL: after breath/dream is called, POST the event as JSON to this URL.
# LOCI_HOOK_SKIP: set to true/1/yes to skip the push. See ENV_VARS.md.
# _fire_webhook reads os.environ on every call rather than caching a module constant, so a
# change to the environment takes effect on the next call with no module global to update.


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# No bare magic numbers: every threshold that gets tuned is gathered here.
# Do not change security-, auth- or performance-related values at runtime; if one is
# adjusted, run pytest alongside.
# ============================================================

# --- Webhook / HTTP client timeout ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0

# --- The panel's password and login rate-limit constants live in web/_shared.py ---


async def _fire_webhook(event: str, payload: dict) -> None:
    """
    Fire-and-forget POST to LOCI_HOOK_URL with the given event payload.
    Failures are logged at WARNING level only — never propagated to the caller.
    """
    hook_url = os.environ.get("LOCI_HOOK_URL", "").strip()
    hook_skip = os.environ.get("LOCI_HOOK_SKIP", "").strip().lower() in ("1", "true", "yes", "on")
    if hook_skip or not hook_url:
        return
    if not hook_url.startswith(("http://", "https://")):
        logger.warning("LOCI_HOOK_URL rejected: only http/https URLs are allowed")
        return
    try:
        body = {
            "event": event,
            "timestamp": time.time(),
            "payload": payload,
        }
        async with httpx.AsyncClient(timeout=_WEBHOOK_TIMEOUT_SECONDS) as client:
            await client.post(hook_url, json=body)
    except Exception as e:
        # Webhook credentials commonly live in the URL path/query.  Never put
        # either the configured URL or httpx's URL-bearing exception text in logs.
        logger.warning("Webhook push failed (%s): %s", event, type(e).__name__)

# --- Initialize core components ---
# The unified error-code system. It must be configured before any business initialization,
# so that the errors.jsonl path is in effect from the start.
from core.errors import configure_errors_path, OBStartupError, write_fatal_log
configure_errors_path(config.get("buckets_dir", "buckets"))

try:
    embedding_engine = EmbeddingEngine(config)            # Embedding engine first (BucketManager depends on it)
except OBStartupError as _ob_err:
    # OB-F001 is already formatted inside OBStartupError; write the fatal log and exit.
    logger.error(str(_ob_err))
    write_fatal_log(_ob_err.error_code, _ob_err.detail, buckets_dir=config.get("buckets_dir"))
    raise
except RuntimeError as _emb_err:
    # Compatibility with older raises not yet migrated to OBStartupError; this should no
    # longer fire.
    logger.error(f"[STARTUP FAILED] {_emb_err}")
    raise SystemExit(f"Loci Brain 启动中止：{_emb_err}") from _emb_err
bucket_mgr = BucketManager(config, embedding_engine=embedding_engine)  # Bucket manager

# Library version: a new, empty library is stamped current; one that is behind is only
# reported (loud here, red on the health page). Migrating rewrites every file, so it is
# never started from here: scripts/migrate.py, with the server stopped.
from core import schema as _schema  # noqa: E402
try:
    _schema.stamp_new_library(config["buckets_dir"])
    _schema_status = _schema.status(config["buckets_dir"])
    if _schema_status["error"]:
        logger.warning(f"[schema] cannot read the library version: {_schema_status['error']}")
    elif _schema_status["behind"]:
        logger.warning(
            f"[schema] library is version {_schema_status['version']}, code expects "
            f"{_schema_status['current']}: stop the server and run scripts/migrate.py")
except OSError as _schema_err:
    logger.warning(f"[schema] cannot stamp the library version: {_schema_err}")
embedding_outbox = EmbeddingOutbox(config, bucket_mgr, embedding_engine)
bucket_mgr.attach_embedding_outbox(embedding_outbox)
dehydrator = Dehydrator(config)                      # Dehydrator
decay_engine = DecayEngine(config, bucket_mgr)       # Decay engine
import_engine = ImportEngine(config, bucket_mgr, dehydrator, embedding_engine)  # Import engine
migrate_engine = MigrateEngine(config, bucket_mgr, embedding_engine)              # Memory-pack migration engine

# --- Create MCP server instance ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
#
# One connector: every tool is registered on `mcp` and served on its single /mcp route, and
# every HTTP custom_route (panel and API) hangs off it too.
mcp = FastMCP(
    "Loci Brain",
    host=_BIND_HOST,
    port=LOCI_PORT,
)


# =============================================================
# Panel auth: the password and rate-limit helpers live in
# web/_shared.py, and the /auth/* routes in web/panel_auth.py. This block imports the web
# package and injects the config into it. No HTTP route handler lives in this file; every
# one is under web/.
# =============================================================
import web as _web
import web._shared as _wsh
_wsh.init(config)
# Memory persistence self-check: if the memory directory in a container is not on a
# persistent volume, a rebuild loses everything. Warn conspicuously at boot rather than
# letting the user believe something was saved when it was not. Warn only, never block —
# blocking would make deployment miserable.
try:
    _dp = _wsh.data_dir_persistence(config.get("buckets_dir", ""))
    if not _dp["persistent"]:
        logger.warning(
            "=" * 60 + "\n"
            "⚠️  记忆目录未挂载到持久卷：" + str(config.get("buckets_dir", "")) + "\n"
            "    " + _dp["note"] + "\n"
            "    （记忆比代码金贵：代码能重部署，记忆丢了找不回。请尽快修正挂载。）\n"
            + "=" * 60
        )
    else:
        logger.info(f"记忆目录持久性：{_dp['mode']} — {_dp['note']}")
except Exception as _dpe:
    logger.warning(f"数据目录持久性自检失败（不影响启动）：{_dpe}")
# Inject the engines, version and repository root into the web layer (the core/runtime
# pattern).
# Note: embedding_engine is replaced by hot reload. Whoever replaces it must also write
# _wsh.embedding_engine, or the web layer keeps handing out the old instance.
_wsh.init_runtime(
    version=__version__,
    repo_root=os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    migrate_engine=migrate_engine,
)
# There are no dashboard cookie sessions to load: /api/* is not authenticated at this
# layer. Read the header of web/_shared.py before adding anything session-like here.

# Register every web/ route module; see web/__init__.register_all.
_web.register_all(mcp)


# =============================================================
# The panel page and its assets (/loci, /loci/panel/*, /loci/vendor/*) are served by web/loci.py.
# =============================================================


# =============================================================
# The call wrapper (server_call.py) reads who is calling from this instance's request
# context, and the poke line from the store.
# =============================================================
_call.bind(mcp, bucket_mgr)


# _fire_webhook is defined here and the retired hard-delete notice shims in server_call;
# web/'s hooks and buckets routes need them, so they are injected into web._shared, where
# the routes reach them as sh.fire_webhook and friends.


_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# There is no /breath-hook or /dream-hook route. Dreaming is not an obligation, so
# nothing outside triggers it.
# =============================================================


# =============================================================
# Wire the shared runtime context
# Inject every shared object into core.runtime, so the tools/* submodules and the engine
# pieces under core/ can reach them.
# =============================================================
_core_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    logger=logger,
    fire_webhook=_fire_webhook,
)


# =============================================================
# MCP tools — the mount table
# Each face is server_tools/<tool>.py: its signature and description, and one call that
# forwards to tools/<tool>/. Registration order is the order a client lists them in; each
# face's adapt() runs right after it is mounted.
# =============================================================
breath = mcp.tool()(_face_breath.breath)
_face_breath.adapt(mcp)
grow = mcp.tool()(_face_grow.grow)
_face_grow.adapt(mcp)
recall = mcp.tool()(_face_recall.recall)
_face_recall.adapt(mcp)
fold = mcp.tool()(_face_fold.fold)
_face_fold.adapt(mcp)
muse = mcp.tool()(_face_muse.muse)
_face_muse.adapt(mcp)
regrow = mcp.tool()(_face_regrow.regrow)
_face_regrow.adapt(mcp)
trace = mcp.tool()(_face_trace.trace)
_face_trace.adapt(mcp)


# `pulse` is not on the MCP tool surface.
#    The reasoning: the other tools are all "what am I doing to a memory", and this one
#    alone is "is this machine healthy" — a health check is not a memory action, and should
#    not occupy a tool slot. The implementation is in `tools/pulse/`, reached through the
#    panel's read-only `GET /api/loci/pulse` (see web/loci_health.py).


# --- Every registered tool refuses removed parameters ------------------------------------
# History: forbid was added to breath/grow/recall/trace, missing fold and muse; the next
#       pass added fold and muse and missed regrow.
# Every time, the fix was "patch whichever names came to mind at the time". So the block
#    below **iterates the tool registry** and installs the gate on anything that lacks it,
#    so **a tool added later gets it automatically**.
#    The rule: **a list kept by human memory will be missed eventually.**
try:
    for _tool_name in ("breath", "grow", "recall", "regrow", "fold", "muse", "trace"):
        _t = mcp._tool_manager.get_tool(_tool_name)
        if _t is None:
            continue
        # ⚠️ Go through harden(), never an inline flip: flipping without the re-publish
        #    step leaves a tool rejecting unknown arguments while still advertising
        #    that it accepts them. harden() is the only thing that keeps the two halves
        #    from drifting apart.
        if _harden_tool(_t):
            logger.info("strict-argument adapter installed for %s", _tool_name)
except (AttributeError, RuntimeError, TypeError, ValueError) as _strict_all_exc:
    logger.warning("strict-argument sweep unavailable: %s", _strict_all_exc)


# **There is no `seed` tool** (the thirteen emotional roots).
#    Reasoning: events are episodic memories, so the **original words** for "what did
#    this feel like at the time" are right there in the text. **What was actually said then
#    beats a word looked up in a dictionary.** And **a tool that needs a warning label to
#    get used was never really in hand at all.**
#    WARNING: what this gives up is the cross-memory emotional index. "When was I ever
#       afraid" has no tag to search by; only vector search reaches it.
#       **Judged acceptable.**
#    The roots' buckets on disk are kept. The `domain[0]=="seed"` filter in
#       `core/visibility.timeline_kind()` keeps them off the timeline.
#    The thirteen roots' buckets are on disk; the only way to them is a direct id lookup
#    through `recall`.
#    WARNING: do not casually register one. Read the reasoning above first, especially "a
#    tool that needs a warning label was never really in hand".


# =============================================================
# The panel's HTTP routes are all under web/, registered by web.register_all (above):
#   web/config_api.py  /api/config, /api/test/dehydration, /api/test/embedding, /api/models,
#                      /api/loci/ollama
#   web/import_api.py  /api/import/*
#   web/panel_auth.py  /auth/*
#   web/loci.py        the page (/loci, /loci/panel/*, /loci/vendor/*), /api/loci/*, /api/v2/*, /api/logs,
#                      /api/dream/current, /api/muse/pending — its header lists every one
# =============================================================


# ============================================================
# OAuth 2.0 — MCP remote auth, in bridge/oauth.py: this is not a panel route, it is how a
# remote client authenticates to /mcp itself. "Auth is on by default" is a settled position
# for the open-source build, and it must not depend on the panel (bridge/__init__.py carries
# the full reasoning).
# The two validators the start-time MCP auth middleware needs are imported back here:
# mcp_auth_mode=="oauth" (the default) uses _is_valid_mcp_token, mcp_auth_mode=="token" uses
# _is_valid_static_mcp_token, and one of the two is injected into the middleware.
# ============================================================
from bridge.oauth import _is_valid_mcp_token, _is_valid_static_mcp_token  # noqa: F401


# --- Entry point ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Loci Brain starting | transport: {transport}")

    from server_app import (
        HTTPRuntimeSettings,
        RuntimeLifecycle,
        build_http_app,
    )

    if transport in ("sse", "streamable-http"):
        import uvicorn
        from bridge import ollama_child as _ollama_child
        from web.panel_auth import KEEPALIVE_PATH

        _http_settings = HTTPRuntimeSettings.from_config(config)
        _runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            decay_engine=decay_engine,
            embedding_outbox=embedding_outbox,
            ensure_ollama_child=_ollama_child.ensure_child_on_boot,
            stop_ollama_child=_ollama_child.stop_child,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
            # Explicit IPv4 avoids localhost resolving to ::1 in Proot/Termux. The path is
            # a public route: a gated one would answer 401 once the panel is locked.
            keepalive_url=f"http://127.0.0.1:{LOCI_PORT}{KEEPALIVE_PATH}",
        )
        _mcp_token_validator = (
            _is_valid_static_mcp_token
            if _http_settings.auth_mode == "token"
            else _is_valid_mcp_token
        )
        _app = build_http_app(
            mcp,
            transport,
            settings=_http_settings,
            token_validator=_mcp_token_validator,
            lifecycle=_runtime_lifecycle,
            host_resolver=_hosts,
        )
        # (The tool count is not reported here. The line above, about folding N secondary
        #  tools into the primary instance for M exposed in total, reports the real number;
        #  a hardcoded count here goes stale the moment a tool is added or withdrawn.)
        logger.info("CORS middleware enabled for remote transport / 已启用 CORS 中间件")
        logger.info(
            "MCP request body limit: %s",
            "disabled"
            if _http_settings.max_request_bytes == 0
            else f"{_http_settings.max_request_bytes} bytes",
        )

        _mcp_auth_required = _http_settings.auth_required
        if _mcp_auth_required and _http_settings.auth_mode == "token":
            logger.info(
                "MCP 静态 Token 鉴权已启用（OAuth 端点已关闭）/ "
                "MCP static-token auth enabled (OAuth endpoints disabled)"
            )
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 静态 Token 等同万能密钥：拿到它的人能读写你的全部记忆。\n"
                "    该模式与 OAuth 互斥，本进程不再提供 OAuth 授权流程；请勿把本服务\n"
                "    直接暴露到公网，仅在可信内网或自带鉴权的隧道场景使用，并妥善保管、\n"
                "    定期轮换该 Token。\n"
                + "=" * 60
            )
        elif _mcp_auth_required:
            logger.info("MCP OAuth middleware enabled / MCP OAuth 中间件已启用")
        else:
            # Turning auth off means /mcp is completely open: anyone who can reach the port
            # can read and write every memory. This was raised from info to a conspicuous
            # WARNING so that nobody exposes their memory to the internet without noticing.
            logger.warning(
                "=" * 60 + "\n"
                "⚠️  MCP 认证已关闭 (mcp_require_auth: false)：/mcp 无需任何令牌即可直连，\n"
                "    所有记忆工具全部对外开放——任何能访问本端口的人都能读写你的全部记忆。\n"
                "    本服务监听 0.0.0.0，若端口暴露到局域网/公网，请务必用反代鉴权、防火墙\n"
                "    或仅绑定 127.0.0.1 保护；仅在可信内网/本机自有前端场景才建议关闭鉴权。\n"
                + "=" * 60
            )
        # Clarify which port is which; users report confusing the Docker port with the
        # bare-metal one. Inside a container the listen port is fixed at 8000 and the
        # externally visible port comes from the host mapping (e.g. 18001:8000), so changing
        # host_port does not affect what the container listens on. Bare metal listens on
        # this port directly (18001 by default).
        if _wsh.in_docker():
            logger.info(
                f"Listening on :{LOCI_PORT} INSIDE the container. "
                f"外部访问端口由 host 映射决定（compose 里的 18001:{LOCI_PORT}），"
                f"改前端 host_port 不影响容器内监听。"
            )
        else:
            logger.info(f"Listening on :{LOCI_PORT} (bare-metal / 裸机默认 18001)")
        # Print, explicitly, how a client should connect — for non-technical users of mobile
        # or self-hosted front-ends who need to debug this. The endpoint path and the auth
        # switch are both visible at a glance; a local bridge must use 127.0.0.1.
        logger.info(
            "MCP endpoint ready | transport=%s | 本机连接 URL: http://127.0.0.1:%s/mcp "
            "（远程走你的域名/隧道，末尾同样是 /mcp）| 鉴权: %s",
            transport,
            LOCI_PORT,
            (
                "开启(需静态 Token)" if _http_settings.auth_mode == "token"
                else "开启(需 OAuth Bearer)"
            ) if _mcp_auth_required
            else "关闭(免 token 直连，仅限可信内网/本机)",
        )
        # Forwarded headers are validated inside the application against
        # LOCI_TRUSTED_PROXY_CIDRS.  Uvicorn's default proxy middleware rewrites
        # scope["client"] before our guards run, which discards the immediate
        # proxy address and makes that trust decision impossible.
        uvicorn.run(
            _app,
            host=_BIND_HOST,
            port=LOCI_PORT,
            proxy_headers=False,
        )
    else:
        # stdio: the tools were already folded into mcp at the entry point above, so
        # everything is exposed; just run.
        mcp.run(transport=transport)
