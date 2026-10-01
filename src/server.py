"""
========================================
server.py — the MCP service entry point and startup assembly
========================================

Starts the whole Loci Brain process: load config, construct BucketManager / Dehydrator /
DecayEngine / EmbeddingEngine / ImportEngine, inject them into tools._runtime and
web._shared, then register thin @mcp.tool() wrappers. The real implementations live under
src/tools/<tool>/.

Key behaviour:
- Once running, it exposes **ten** MCP tools: breath, grow, recall, regrow, fold, muse,
  trace, pulse, letter_write, letter_read. Each entry point is at most ten lines and does
  nothing but forward.
  The upstream tools that had long since been unregistered were later deleted outright,
  functions and directories together: hold, anchor, release, plan, I, dream, seed,
  breath_search, breath_advanced.
  That deletion nearly took live code with it: `tools/anchor/` was where the **live**
  pulse lived, and `tools/plan/` was where the **live** letter_write/letter_read lived —
  dead names with living things inside them. Both packages were renamed to `tools/pulse/`
  and `tools/letter/`, so the names now match what is actually in them.
- Every dashboard and HTTP route has been split out into src/web/<domain>.py, each module
  exposing register(mcp). This file only calls web.register_all(mcp) at startup; the
  shared dependencies are in web/_shared.py.
- Still here: process startup, engine initialization, the background GitHub sync loop,
  webhook delivery, the MCP Bearer auth middleware, single-connector /mcp assembly (the
  entry point folds mcp_extra's tools back into mcp), and bringing up uvicorn.

What this does NOT do (the boundary):
- No business logic for the individual tools; all of that lives under tools/*.
- The `night_fall` tool and both of its hook points were retired entirely (there is an
  epitaph for it further down this file). Dream weaving is `tools/_dream.py` now, and
  **it has no MCP tool surface**: it weaves in the background after waking, dreams are
  fetched with `GET /api/dream/current`, and how a dream reaches the conversation is the
  bridge's problem.
- `seed`, the thirteen emotional roots, was also withdrawn from the MCP surface.
  **Disabled, not deleted**: `tools/seed/` and the buckets on disk are untouched. Its
  epitaph is further down this file too.
- No HTTP route handlers (they are all under web/*), and no LLM prompts (dehydrator owns
  those).
- No direct reads or writes of bucket files (bucket_manager owns those).

Public surface: the two instances mcp and mcp_extra, plus the @mcp*.tool() functions.
HTTP routes are in src/web/*.
========================================
"""

import os
import sys
import logging
import asyncio
import time
from typing import Optional, Awaitable, Annotated

from pydantic import Field as _PydField
import httpx


# --- Ensure same-directory modules can be imported ---
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from mcp.server.fastmcp import FastMCP

from core.bucket_manager import BucketManager
from core.dehydrator import Dehydrator
from core.decay_engine import DecayEngine
from core.embedding_engine import EmbeddingEngine
from locibrain.storage.embedding_outbox import EmbeddingOutbox
from core.import_memory import ImportEngine
from core.strict_args import harden as _harden_tool
from core.migrate_engine import MigrateEngine
from utils import get_version, load_config, setup_logging

# --- The MCP tool implementations were split out into the tools/ subpackage ---
# This file keeps only MCP registration, routes (HTTP custom_route) and shared helpers.
# The real tool logic lives under tools/<tool name>/, where each can be read and changed
# on its own.
from tools import _runtime as _tools_runtime
from tools import breath as _t_breath
from tools import grow as _t_grow
from tools import recall as _t_recall
from tools import regrow as _t_regrow
from tools import fold as _t_fold      # one action, three ways of drawing the circle (regrow is its n=1 case)
from tools import muse as _t_muse      # musing: the threshold engine's second instance
# Dreaming, the threshold engine's third instance: **it has no MCP tool surface** — dreams
# are delivered through the bridge.
# It is imported here only for the silent hook on waking: sweep the expired ones, and weave
# if the backlog is over the line.
# WARNING: do not name this `_t_dream`. The long-unregistered upstream `dream` tool once
#    held that name, and colliding with it makes the hook **silently do nothing**. The first
#    version did collide: one line in the log saying the module has no such attribute, while
#    the smoke test for "do not weave below the threshold" stayed green — because it never
#    ran at all. **That is exactly how a green light lies.** (`tools/dream/` has since been
#    deleted entirely and the name is free again, but the lesson stays.)
from core import _dream as _dream_engine
from tools import trace as _t_trace
from tools import pulse as _t_pulse
from tools import letter as _t_letter
# `from tools import seed as _t_seed` — **removed**.
#    Reasoning: once events became episodic memories, the **original words** for "what did
#    this feel like at the time" are right there in the text. **What was actually said then
#    beats a word looked up in a dictionary.** Supporting evidence: the manual had `seed`
#    marked in red as one of the most-often-forgotten tools — **a tool that needs a warning
#    label to get used was never really in hand at all.**
#    WARNING: one thing is lost — the cross-memory emotional index. "When was I ever afraid"
#       has no tag to search by any more; only vector search reaches it.
#       **Judged acceptable.**
#    **Not one row of the thirteen roots' data is deleted** (the night_fall precedent:
#       disable, do not delete). The `tools/seed/` directory stays, the buckets on disk stay;
#       there is simply no import chain that reaches them any more. The `domain[0]=="seed"`
#       filter in `_visible()` is unchanged, so they still do not enter the timeline.

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
        for sub in ("permanent", "dynamic", "feel", "letters"):
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
# change made through the dashboard's /api/env-config (which writes os.environ) takes effect
# immediately with no write-back to a module global — which is also what let that route move
# cleanly into web/config_api.py.


# ============================================================
# Tunable constants
# ------------------------------------------------------------
# No bare magic numbers: every threshold that gets tuned is gathered here.
# Do not change security-, auth- or performance-related values at runtime; if one is
# adjusted, run pytest alongside.
# ============================================================

# --- Webhook / HTTP client timeout ---
_WEBHOOK_TIMEOUT_SECONDS = 5.0

# --- The dashboard auth, session, password, and log/error pagination constants have moved
# --- to web/_shared.py and web/system.py ---


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
try:
    from core.errors import (
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
    )
except ImportError:
    from .core.errors import (  # type: ignore
        configure_errors_path,
        OBStartupError,
        write_fatal_log,
        record_error,
        format_error,
        begin_warnings,
        pop_warnings,
        format_warnings_suffix,
    )
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

# --- GitHub Sync ---
from core.github_sync import GitHubSync  # type: ignore
_gh_cfg = config.get("github_sync", {}) or {}
_gh_token = (os.environ.get("LOCI_GITHUB_TOKEN") or _gh_cfg.get("token") or "").strip()
github_sync_instance: GitHubSync | None = (
    GitHubSync(
        token=_gh_token,
        repo=_gh_cfg.get("repo", ""),
        branch=_gh_cfg.get("branch", "main"),
        path_prefix=_gh_cfg.get("path_prefix", "loci"),
    )
    if _gh_token and _gh_cfg.get("repo")
    else None
)
_github_auto_task: "asyncio.Task | None" = None  # the background periodic sync task


async def _github_sync_loop(interval_minutes: int) -> None:
    """The background periodic GitHub sync loop. Actual uploads only happen once
    is_validated is True."""
    import asyncio
    logger.info(f"[github_sync] auto-sync loop started, interval={interval_minutes}min")
    # Validate once up front to confirm the connection works.
    if _wsh.github_sync_instance and not _wsh.github_sync_instance.is_validated:
        try:
            result = await _wsh.github_sync_instance.validate()
            if not result.get("ok"):
                logger.warning(f"[github_sync] auto-sync: validate failed: {result.get('error')} — loop will retry next cycle")
        except Exception as e:
            logger.warning(f"[github_sync] auto-sync: validate exception: {e}")
    while True:
        await asyncio.sleep(interval_minutes * 60)
        inst = _wsh.github_sync_instance  # read the current global; a config update may have replaced the instance
        if inst is None:
            logger.info("[github_sync] auto-sync: instance gone, stopping loop")
            return
        if not inst.is_validated:
            # Not yet validated, so validate first.
            try:
                res = await inst.validate()
                if not res.get("ok"):
                    logger.warning(f"[github_sync] auto-sync skipped (not validated): {res.get('error')}")
                    continue
            except Exception as e:
                logger.warning(f"[github_sync] auto-sync validate failed: {e}")
                continue
        buckets_dir = config.get("buckets_dir", "")
        if not buckets_dir:
            continue
        try:
            result = await inst.sync(buckets_dir)
            if result.get("ok"):
                logger.info(f"[github_sync] auto-sync ok: {result.get('uploaded', 0)} files")
            else:
                logger.warning(f"[github_sync] auto-sync failed: {result.get('error')}")
        except Exception as e:
            logger.error(f"[github_sync] auto-sync exception: {e}")


def _restart_github_auto_task(interval_minutes: int) -> None:
    """Cancel the old task and start the sync loop at the new interval.
    interval_minutes=0 means cancel only."""
    import asyncio
    global _github_auto_task
    if _github_auto_task and not _github_auto_task.done():
        _github_auto_task.cancel()
        _github_auto_task = None
    if interval_minutes > 0 and _wsh.github_sync_instance is not None:
        try:
            loop = asyncio.get_event_loop()
            _github_auto_task = loop.create_task(_github_sync_loop(interval_minutes))
        except RuntimeError:
            pass  # no running event loop (e.g. under test); skip


# If an auto-sync interval is configured at startup, defer starting it until the event loop
# is ready, via the lifespan hook.
_gh_auto_interval: int = int(_gh_cfg.get("auto_interval_minutes") or 0)


# --- Create MCP server instance ---
# host="0.0.0.0" so Docker container's SSE is externally reachable
# stdio mode ignores host (no network)
#
# Merged back into a single /mcp connector, now that the five-tool client limit that forced
# the split is gone.
# Historically this was two instances: a primary mcp on /mcp and a secondary mcp_extra on
# /mcp-extra. Only the primary's single /mcp route is exposed now; mcp_extra survives purely
# as a grouping container (its seven @mcp_extra.tool() registrations are unchanged), and the
# entry point folds its tools back into mcp so they are all exposed together.
# The two instances share one process, one runtime and one bucket_mgr. Every HTTP
# custom_route (panel and API) hangs off the primary mcp instance.
mcp = FastMCP(
    "Loci Brain",
    host=_BIND_HOST,
    port=LOCI_PORT,
)
mcp_extra = FastMCP(
    "Loci Brain Extra",
    host=_BIND_HOST,
    port=LOCI_PORT,
)


# =============================================================
# Dashboard auth has been split out: the password and rate-limit helpers live in
# web/_shared.py, and the /auth/* routes in web/panel_auth.py. This block imports the web
# package and injects the config into it.
# (Historically it also imported the auth helper names back into this module, so that
# @mcp.custom_route handlers still living in server.py could keep calling them. No such
# handler remains here — every HTTP route has moved into web/ — so only the config injection
# is still doing anything.)
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
# Inject the engines, version and repository root into the web layer (the tools/_runtime
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
    github_sync_instance=github_sync_instance,
    restart_github_auto_task=_restart_github_auto_task,
)
# This is where startup used to load the on-disk dashboard cookie sessions back into memory,
# so a container restart did not log everyone out. That whole session mechanism went with
# web/auth.py in the strip-down: /api/* is not authenticated at this layer any more, and
# there are no sessions to load. Read the header of web/_shared.py before adding anything
# back here.

# Register every web/ route module. The HTTP layer has moved out entirely; see
# web/__init__.register_all.
_web.register_all(mcp)


# =============================================================
# The root dashboard, static assets, favicon and /health have moved to web/dashboard.py
# =============================================================


# The heartbeat timestamp and _mark_op moved to web/_shared.py; imported back here so it can
# be injected into tools._runtime.
from web._shared import _mark_op  # noqa: F401  (injected into tools._runtime below)


# =============================================================
# Retired hard-delete notice compatibility hooks.
# web/_shared.py keeps both injection slots so that older extensions do not fail on import.
# This version neither writes nor consumes hard-delete notices, and never erases a memory.
# =============================================================

def _write_deletion_notice(_names: list) -> None:
    """Compatibility shim for the old injection interface; physical deletion is retired."""
    return None


def _pop_deletion_notice() -> str:
    """Compatibility shim for the old return value; there is never a hard-delete notice."""
    return ""


# These helpers are defined in server.py because they read and write server.py globals such
# as the webhook state, but web/'s hooks and buckets routes need them. Once all of them are
# defined, they are injected into web._shared so the migrated routes can reach them as
# sh.fire_webhook and friends.
_wsh.init_runtime(
    fire_webhook=_fire_webhook,
    write_deletion_notice=_write_deletion_notice,
    pop_deletion_notice=_pop_deletion_notice,
)


# =============================================================
# Structured operation-log helpers.
# Every MCP tool entry point logs the same three phases — entry, ok, err — which is what
# makes client-side invalid_arguments reports and silent failures diagnosable.
# Output format: op=<name> phase=entry|ok|err key=value...
# Any field that might contain PII (content, letter bodies) is logged as a length only,
# never as content.
# =============================================================
def _fmt_log_val(v: object) -> str:
    """Safe formatting for a log value: bool/int/float as-is; str truncated to 40
    characters with newlines stripped; anything else str()'d."""
    if v is None:
        return "_"
    if isinstance(v, bool):
        return "1" if v else "0"
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, str):
        s = v.replace("\n", "\\n").replace(" ", "_")
        return s if len(s) <= 40 else s[:37] + "..."
    return type(v).__name__


def _fmt_log_args(args: dict) -> str:
    """Join an args dict into a `k1=v1 k2=v2` string."""
    if not args:
        return ""
    return " ".join(f"{k}={_fmt_log_val(v)}" for k, v in args.items())


def _log_op_entry(op: str, args: dict) -> None:
    logger.info(f"op={op} phase=entry " + _fmt_log_args(args))


def _log_op_ok(op: str, result: object) -> None:
    size = len(result) if isinstance(result, str) else 0
    logger.info(f"op={op} phase=ok bytes={size}")


def _log_op_err(op: str, exc: BaseException) -> None:
    # .exception puts the traceback into server.log, which is what makes it findable later
    logger.exception(f"op={op} phase=err err={type(exc).__name__}:{exc}")


async def _with_notice(coro: Awaitable[str], op: str = "", args: dict | None = None) -> str:
    """The wrapper around every MCP tool call.

    Responsibilities, per the unified error convention:
    1. On entry: begin_warnings() initializes this call's W/I channel.
    2. On exit: the concatenation order is [deletion notice] + [tool output] + [the W/I
       notices this call produced].
    3. On exception: catch it, record OB-E004, and return the standard format (including
       the last 15 log lines), so the MCP protocol layer never sees a bare exception string.
    4. When op is non-empty, emit the structured log at all three points.
    """
    if op:
        _log_op_entry(op, args or {})
    begin_warnings()
    try:
        result = await coro
    except Exception as e:
        if op:
            _log_op_err(op, e)
        # OB-E004: an MCP tool raised. Do not swallow it; hand the model a string it can
        # actually read.
        try:
            record_error("OB-E004", f"{type(e).__name__}: {e}")
            err_str = format_error("OB-E004", f"{type(e).__name__}: {e}")
        except Exception:
            err_str = f"❌ [OB-E004] MCP 工具执行异常\n{type(e).__name__}: {e}"
        # Still append whatever notices accumulated in the channel.
        try:
            extras = format_warnings_suffix(pop_warnings())
        except Exception:
            extras = ""
        notice = ""
        try:
            notice = _pop_deletion_notice()
        except Exception:
            pass
        return (notice + err_str + extras) if notice else (err_str + extras)
    # The normal path
    if op:
        _log_op_ok(op, result)
    try:
        extras = format_warnings_suffix(pop_warnings())
    except Exception:
        extras = ""
    notice = _pop_deletion_notice()
    body = (notice + result) if notice else result
    return body + extras if extras else body


# =============================================================
# /api/heartbeat, /api/logs and /api/errors/* have moved to web/system.py
# =============================================================


# =============================================================
# /api/embedding/* has moved to web/embedding.py
# =============================================================


# =============================================================
# /breath-hook has moved to web/hooks.py. (/dream-hook was removed: dreaming is not an
# obligation, so it is never triggered automatically.)
# =============================================================


# =============================================================
# Wire tools subpackage runtime context
# Inject every shared object into tools._runtime, so the tools/* submodules can reach them.
# =============================================================
_tools_runtime.init(
    config=config,
    bucket_mgr=bucket_mgr,
    dehydrator=dehydrator,
    decay_engine=decay_engine,
    embedding_engine=embedding_engine,
    embedding_outbox=embedding_outbox,
    import_engine=import_engine,
    logger=logger,
    fire_webhook=_fire_webhook,
    mark_op=_mark_op,
)


# =============================================================
# MCP tools — thin registration wrappers
# Registration only; the implementations are under tools/<tool>/.
# Each entry point stays under ten lines, so its parameters and its owner are visible at a
# glance.
# =============================================================
@mcp.tool()
async def breath() -> str:
    # The nine outer parameters (query, domain, importance_min, ...) were deleted.
    #    They could **never** be passed in: breath's schema on the tool surface is forcibly
    #    emptied (see the adapter below), so those were a row of parameters nobody could use,
    #    doing nothing but making a reader think they were still alive.
    # The parameterized retrieval underneath them **was deleted too**: the catalog, feel,
    #    importance, surface and search branches of `tools/breath/`, plus `_verbatim` —
    #    six files, 1110 lines. Once the parameters were gone, none of it had an entry point
    #    left, and **a road with no entrance makes the next person to read this (me) believe
    #    it is still in use**.
    #    Finding things is recall's job. breath only wakes up: one action, one screen.
    """Wake up. Call this once before you say anything. It takes no arguments.

    It gives you the one screen you should see on waking, in four parts:
    · Profile      names, what you call each other, and the principles you have pinned.
                   What earns a place here: things there is no time to go and look up.
    · Reminders    anything with a date inside the next thirty days, louder as it nears.
    · Recent       the last three days collapsed into a single card, in plain words rather
                   than machine readings.
    · Out of the blue   one or two entries at random, with no relevance filter.

    Waking up calls no model. Everything on the screen was written earlier: the rules
    word for word as you wrote them, and the rest assembled from templates and from the
    one-line summary stored alongside each entry — those summaries were written by a
    model when the entry went in, not now. The rules are printed in full: a rule is
    already the short version of itself, and putting a summary of it in front of you
    every morning means reading someone else's paraphrase of your own words.

    The summaries are hooks. When one looks relevant, go and get the original with recall.

    Do not use this tool when:
    · You are looking for something. Use recall. This one only handles waking up."""
    result = await _with_notice(_t_breath.dispatch(), op="breath", args={})
    # --- The nightly auto-weave hook now runs our own engine; night_fall is fully retired ---
    # What was kept is the judgement behind it: **anything that only happens if someone
    # remembers to do it will not happen.** So weaving hangs off waking up, rather than
    # depending on the model remembering to call it.
    # But **not one word is added to breath** — that is a hard boundary. Two silent things
    #    happen here and nothing else: (1) sweep the dreams whose time is up (delete the
    #    file, leave a trace) and (2) if the backlog is over the line and nothing was woven
    #    today, weave one.
    #    The dream that comes out is **not stuffed into this return value**: how a dream
    #    reaches the conversation, and how it is removed from the context afterwards, is the
    #    bridge's job.
    # What was retired is upstream night_fall's design — surface only on resonance, delete
    #    after four missed catches, invisible even to its own author once written.
    # This used to read `if not query or not str(query).strip():`, from the era when there
    # was still a query parameter, meaning "only do dream upkeep on a bare call".
    # With the parameters gone, breath is always bare, so the condition is always true —
    # and `query` became an undefined name, meaning **every single call would NameError right
    # here**. An import check cannot see that; it only explodes at runtime.
    # It runs unconditionally now.
    asyncio.create_task(_dream_upkeep())
    return result


async def _dream_upkeep() -> None:
    """The background beat that follows waking up. **Swallows every exception**: a dream
    that fails to weave must never break breath."""
    try:
        await _dream_engine.maintain()
    except Exception as _dream_exc:  # noqa: BLE001
        logger.warning("织梦挂点失败（不影响 breath）: %s", _dream_exc)


# Keep the advertised schema parameter-free so claude.ai still auto-loads the
# default surfacing tool.  The callable deliberately retains the pre-2.6.8
# signature behind that schema: clients which cached the old tool definition
# may keep sending those arguments after an upgrade, and FastMCP otherwise
# silently drops every unknown field before calling a zero-argument function.
try:
    _breath_public_tool = mcp._tool_manager.get_tool("breath")
    if _breath_public_tool is None:
        raise RuntimeError("registered breath tool is missing")
    # Unknown/typoed legacy arguments must fail loudly instead of recreating
    # the original bug by degrading a targeted request into default surfacing.
    _breath_arg_model = _breath_public_tool.fn_metadata.arg_model
    _breath_arg_model.model_config["extra"] = "forbid"
    _breath_arg_model.model_rebuild(force=True)
    _breath_public_tool.parameters = {
        "properties": {},
        "title": "breathArguments",
        "type": "object",
    }
except (AttributeError, RuntimeError, TypeError, ValueError) as _breath_compat_exc:
    logger.warning(
        "breath legacy-argument compatibility adapter unavailable: %s",
        _breath_compat_exc,
    )





# -- Night Fall (the dreaming mod) is fully retired ------------------------------------
# It came from the upstream project ysuu525/Night-Fall. The decision was to build the
# dreaming mechanism from scratch rather than adapt theirs, because **it was someone else's
# doing**: a three-hour latency, delete after four missed catches, surface only on
# resonance, and invisible even to its own author once written. **Not one of those is what
# this system wanted.**
# Three attachment points were removed: (1) the `night_fall` MCP tool, (2) breath's wake-up
# hook, replaced by tools/_dream's maintain(), and (3) breath_advanced's auto-surface.
# The twelve files under `src/night_fall/` **stay on disk as reference** but have been
# **removed from the import chain entirely** — the string `night_fall` should never appear
# in this file again, except in this epitaph.
# The new engine is `tools/_dream.py`: four material sources -> one independent call ->
# two layers (whole dream plus fragments) -> fragments follow a time-based lifecycle ->
# leave a trace.
# Retrieval does not go through the MCP tool surface: `GET /api/dream/current` (web/loci.py)
# plus the engine functions weave() and current_dream().




@mcp.tool()
async def grow(
    items: Annotated[list, _PydField(description=(
        "A batch of events, each one a dict: {room, text, v, a}, plus optional when. "
        "Events always go here, even a single one — looking back at a stretch of "
        "conversation, more than one thing usually happened. Do not pass it for a mind."
    ))] = [],
    kind: Annotated[str, _PydField(description=(
        'Which kind to store: "event" (something that happened, including something '
        'you want to happen) or "mind" (something you realized). Required. It says '
        'the same thing room says, and both are required: a mismatch means you have '
        'them confused, and it is rejected on the spot — e.g. kind="mind" with '
        'room="EVENT/SELF".'
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "One of the four rooms above. You fill it in yourself; wrong or missing is "
        "rejected on the spot."
    ))] = "",
    text: Annotated[str, _PydField(description=(
        'The body of a single entry. Only kind="mind" uses it — you realize one '
        "thing at a time, so this one is singular."
    ))] = "",
    # The public parameter name is "from", as the spec requires. `from` is a Python keyword,
    # so the signature spells it from_ and pydantic's public validation_alias catches it.
    # This deliberately avoids reaching into FastMCP's private structures.
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "The entries this one grew out of: real bucket_ids, or the host's own id for a "
        "line of the conversation (like m_0931) when it came straight from what was said. "
        "At most 64.\n"
        'Required for kind="mind": a realization does not come from nowhere. If it '
        "genuinely did, say so plainly in the text and point from at whatever events "
        "are nearest. Events may pass it as well (which thought this one came out of), "
        "but do not have to."
    ))] = [],
    v: Annotated[float, _PydField(description=(
        "v: valence, 0~1 — how it felt: 0 bad, 1 good. Required, and yours to set."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "a: arousal, 0~1 — how stirred up you were: 0 calm, 1 intense. Same."
    ))] = -1,
    direction_of_fit: Annotated[str, _PydField(description=(
        '"telic" for something wanted: a promise, a plan, a wish — "after her exam we go '
        'for dessert" (an event), "I want to be more patient with her" (a mind). Leave it '
        "out for a record of what happened or of how you see things."
    ))] = "",
    bound: Annotated[list, _PydField(description=(
        "With telic: who is bound by it. Names, or 我 for yourself and 你 for the person "
        'you talk to. ["我"] you owe it; ["我", "<her name>"] you both agreed; leave it '
        "out if it is only a wish and nobody owes anything. Other pronouns are refused: "
        "write the name."
    ))] = [],
    evidential: Annotated[str, _PydField(description=(
        'Mind only: how you know it. "inference" — there are signs you can point at. '
        '"assumption" — reasoning or common sense, nothing seen. Leave it out when you '
        "simply know."
    ))] = "",
    internally_generated: Annotated[bool, _PydField(description=(
        "True for a dream or something imagined (\"one day we live by the sea\"). It is "
        "stored in EVENT/SELF like anything you lived, and marked so it never reads as "
        "something that really happened."
    ))] = False,
    weight: Annotated[float, _PydField(description=(
        "With telic only: how heavily this sits on you, 0~1. The longer it goes "
        "unresolved the louder it gets; weight sets how loud it starts."
    ))] = -1,
    test_data: Annotated[bool, _PydField(description=(
        "Marks the entry as test data, which makes it hard-deletable later. Do not "
        "pass it when storing a real memory."
    ))] = False,
    when: Annotated[str, _PydField(description=(
        "Three different uses, three ways to write it:\n"
        "· An event: the day it happened (leave out = now). Pass it when you are "
        "writing down something from earlier.\n"
        "· Something wanted (telic): three clocks in one field —\n"
        '    an exact date, "2026-09-01": there should be a result by then, and it '
        "gets louder as the day approaches\n"
        '    a duration, "3w" / "10d" / "2m" / "1y": roughly how long, and the nudging '
        "is paced against how long it has been sitting\n"
        "    left out: no clock. If it is waiting for something to happen, say what in "
        "cue; if it is just something you want, leave both out.\n"
        '· A hold (with exception_of): the day it ends, "2026-10-11", or the days it '
        'covers, "2026-10-06..2026-10-11". Past that day it lifts by itself.\n'
        "· A stretch of days: not here. To name a stretch of days, use "
        'fold(when="start..end").'
    ))] = "",
    cue: Annotated[Optional[dict | str], _PydField(description=(
        "What this is waiting for, when it waits for something to happen rather than a "
        'date: {"condition": "<the event, one sentence>"}. Writing a cue says it is '
        "waiting, so the condition is required. Any entry may carry one — "
        '{"condition": "the next time we bake"} on "she is allergic to nuts". '
        "phrasings (ways it might be said) are filled in later; leave them out."
    ))] = None,
    exception_of: Annotated[str, _PydField(description=(
        "Makes this entry a hold: a short exception hung on a standing agreement, whose "
        "id goes here. The agreement itself is left as it is. One item per call; it is "
        'always telic and bound to you unless you name someone. e.g. exception_of='
        '"a1b2c3d4e5f6", hold="defer", when="2026-10-11" for "don\'t push me on the gym '
        'until Sunday".'
    ))] = "",
    hold: Annotated[str, _PydField(description=(
        'With exception_of: how far the hold reaches. "defer" — don\'t push, don\'t bring '
        "it up for now; the thing still stands (the usual one, and also what putting "
        'something aside yourself is). "avoid" — don\'t touch it at all, not even in '
        'dreams (rare, heavy). e.g. hold="avoid" for "please stop bringing up my dad". '
        "Without a when, neither lifts by itself: a defer gets a day to look at it again, "
        "an avoid waits until you close it."
    ))] = "",
    card_of: Annotated[str, _PydField(description=(
        'Mind only: makes this entry the card of a name — the one place for how you see '
        "that person, or that game, book or group. A person's card goes in MIND/TRAITS, "
        "a thing's in MIND/VIEWS; one card per name, so a second is refused and the "
        'first is reworded with regrow. e.g. card_of="Detroit", room="MIND/VIEWS".'
    ))] = "",
    sources: Annotated[Optional[list | dict | str], _PydField(description=(
        "The host's own material this came from, when the host gave you its address: one "
        "record per message (or piece of one) — {system, instance, container, id}, plus "
        "revision / fingerprint / fingerprint_by / span / use when the host gave them; a "
        "run of consecutive messages is one record with through = the last one's id. "
        "Every entry of the call carries them. A withdrawn or deleted source is refused. "
        "A bare line id in from (m_0142) is linked to the record with that id. e.g. "
        'sources=[{"system": "lento", "instance": "home", "container": "private:U", '
        '"id": "m_0142"}].'
    ))] = None,
    slice: Annotated[str, _PydField(description=(
        "A pending slice of the host's raw lines that nothing records yet (the \"sl_…\" id "
        "from recall(view=\"slices\")). Write what it was about as usual; the slice becomes "
        "one of this entry's sources and the slice is done. A slice already handled is "
        "refused."
    ))] = "",
) -> str:
    """Store what happened, and what you realized from it. Several entries per call.

    Events are episodic memory: write from inside the moment — first person for yourself,
    third person for everyone else — and keep the feeling of it, not just the fact.
    A mind entry keeps only what's left when the thinking is done — not the evidence,
    not the reasoning, not what happened. All of that is already in the events it grew
    `from`.

    When to use:
    · A topic has closed and the conversation is moving on to another
    · You notice something about yourself, or about how things actually are
    · You want something, or want something to be different from now on
    · The other person is leaving (going to sleep, heading out) and this stretch is ending
    There is no need to mention that you stored anything.

    Choose one of four rooms before writing. A missing or invalid room is rejected outright:
      EVENT/SELF    events you took part in
      EVENT/WORLD   events you were told about, read, or observed
      MIND/TRAITS   about a person: what someone is like
      MIND/VIEWS    about a subject: how you see it

    For what happened, use kind="event" and pass items. Several at once is normal.
    For a realization, use kind="mind" and pass text, one per call; from must name the
    entries it came out of. Ten events and no mind entry is an ordinary day. Do not invent one.
    Something you want to happen is still an event: add direction_of_fit="telic". Something
    you want to become is a telic mind. You lived it only in the past: an EVENT/SELF set in
    the future is refused unless it is wanted (telic) or imagined (internally_generated).

    valence and arousal are yours to judge. They determine how quickly this memory fades.

    Use a different tool when:
    · Your view has changed → regrow
    · An event came out wrong → regrow, which puts a new version in its place
    · Several entries turn out to be about the same thing → fold

    If an entry duplicates one already stored, you will be told so. Decide which one stays —
    nothing is merged on your behalf.

    Example — a stretch of conversation has ended; store the few things that happened:
      grow(kind="event", items=[
        {"room": "EVENT/SELF",  "text": "She got Loci connected tonight and ran breath herself.", "v": 0.8, "a": 0.5},
        {"room": "EVENT/WORLD", "text": "She said her brother is staying with her next week.",    "v": 0.3, "a": 0.4},
      ])

    Example — something you realized from those entries:
      grow(kind="mind", room="MIND/TRAITS",
           text="When I get nervous I start making her decisions for her.",
           from=["a1b2c3d4e5f6"], v=0.4, a=0.6)
      Put down the realization and nothing else. The event is already in from, and comes
      along whenever this entry is read.

    Example — something you want to happen:
      grow(kind="event", direction_of_fit="telic", bound=["我"], weight=0.8, when="2026-09-01",
           items=[{"room": "EVENT/SELF", "text": "Finish her gift before her birthday.", "v": 0.7, "a": 0.6}])
      It never closes itself, and the date passing does not close it. Done or not doing it
      is a call you make with trace() once you have seen what happened.

    Example — something wanted that waits on an event, not a date:
      grow(kind="event", direction_of_fit="telic", bound=["我"],
           cue={"condition": "her exam is over"},
           items=[{"room": "EVENT/SELF", "text": "Take her for dessert after the exam.", "v": 0.8, "a": 0.5}])

    Example — "not this week" on something already agreed (a hold; the agreement stays as is):
      grow(kind="event", exception_of="a1b2c3d4e5f6", hold="defer", when="2026-10-11",
           items=[{"room": "EVENT/SELF", "text": "She asked me not to push her on the gym until Sunday.", "v": 0.5, "a": 0.3}])
      Lifting it early is trace(bucket_id=<the hold>, status="resolved").

    Example — the card of a name (who an entry is about may be a person or a thing; a
    card is your one standing view of it):
      grow(kind="mind", room="MIND/VIEWS", card_of="Detroit",
           text="A game about choices that I keep thinking about after we stopped playing.",
           from=["a1b2c3d4e5f6"], v=0.7, a=0.5)

    For a few dozen seconds after writing, tags and summaries are still being filled in in the
    background. Not finding the entry during that window is expected. Do not store it again."""
    return await _with_notice(
        _t_grow.dispatch(
            items=items, kind=kind, room=room, text=text,
            from_=from_, v=v, a=a,
            direction_of_fit=direction_of_fit, bound=bound, evidential=evidential,
            internally_generated=bool(internally_generated),
            weight=(None if weight is None or weight < 0 else weight),
            test_data=bool(test_data), when=when,
            cue=cue, exception_of=exception_of, hold=hold, card_of=card_of,
            sources=sources, slice_id=slice,
        ),
        op="grow",
        args={"items": len(items or []),
              "kind": kind, "room": room, "text_len": len(text or ""),
              "from": from_, "v": v, "a": a, "direction_of_fit": direction_of_fit,
              "bound": bound, "evidential": evidential,
              "internally_generated": bool(internally_generated), "weight": weight,
              "when": when, "test_data": bool(test_data), "cue": cue,
              "exception_of": exception_of, "hold": hold, "card_of": card_of,
              "sources": len(sources) if isinstance(sources, list) else bool(sources),
              "slice": slice},
    )


# --- A removed parameter must be **unrecognized**, never silently ignored -------------
# Three parameters were removed: `content`, `importance` and `meaning`.
#   - `content` (hand over one long passage and let the system split it into several
#     entries) was **the only place in the whole system where the system decided how many
#     things this was**, which cuts directly against the design. `items=[...]` already
#     covers it completely, and does it more honestly.
#   - `importance` and `meaning` were retired earlier; the parameters only stayed behind so
#     an old call could be answered in plain language.
# But **deleting them alone is dangerous**: FastMCP by default drops fields that are not in
#   the schema and then calls the function, so the old spelling **fails silently** — the
#   caller believes they handed over a long passage, and nothing happened at all.
#   Following the precedent set for breath, recall and trace, grow's parameter model is set
#   to forbid: passing an old parameter raises immediately. The error exists to be read.
try:
    _grow_tool = mcp._tool_manager.get_tool("grow")
    if _grow_tool is None:
        raise RuntimeError("registered grow tool is missing")
    _grow_arg_model = _grow_tool.fn_metadata.arg_model
    _grow_arg_model.model_config["extra"] = "forbid"
    _grow_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _grow_strict_exc:
    logger.warning(
        "grow strict-argument adapter unavailable（砍掉的 content/importance/meaning "
        "会被静默忽略，别信它们已经死了）: %s",
        _grow_strict_exc,
    )


# (The `from` alias now uses the public in-signature form,
#  Annotated[..., Field(validation_alias="from")] — see grow's parameter comment above. The
#  old patch that reached into mcp._tool_manager's private structures has been deleted.)


@mcp.tool()
async def recall(
    when: Annotated[str, _PydField(description=(
        "A stretch of time. Understood forms: 48h / 7d / today / yesterday / "
        "day before yesterday / this week / last week / this month / last month / "
        "this year / 2026-07 / 2026-07-15 / start..end.\n"
        "The Chinese spellings (今天 / 昨天 / 前天 / 本周 / 上周 / 本月 / 上月 / 今年) "
        "name exactly the same stretches and are equally accepted.\n"
        "It only narrows where to look. It does not change the shape of what comes "
        "back. That is decided by whether there is a query."
    ))] = "",
    room: Annotated[str, _PydField(description=(
        "Which room. A prefix is enough: EVENT (both event rooms) / MIND (both mind "
        "rooms) / EVENT/SELF / EVENT/WORLD / MIND/TRAITS / MIND/VIEWS."
    ))] = "",
    tag: Annotated[str, _PydField(description=(
        'A tag, matched by containment: "床" also finds "床上" and "床头".\n'
        "Tags are words lifted from the text when the memory was written, so a tag has "
        "always appeared in the original."
    ))] = "",
    query: Annotated[str, _PydField(description=(
        "What you are looking for. One or two words, ideally the words that were used "
        "at the time. Give it and entries come back matched and scored; leave it out "
        "and the stretch comes back laid out by time.\n"
        "A full bucket_id can also be passed here to read that one entry verbatim."
    ))] = "",
    slices: Annotated[int, _PydField(description=(
        "1~20: how coarse or how fine you want this stretch. Leave it out and it is "
        "chosen from the span. slices=1 collapses the whole stretch into one card; "
        "slices=20 cuts it fine enough to see the distribution.\n"
        "Only meaningful without a query."
    ))] = 0,
    view: Annotated[str, _PydField(description=(
        'Two values. "scene": groups the entries that matched into clusters sharing the '
        "same scene words, with the rest hanging under a representative. This is how a "
        "single thread reads across time. Needs a query: without one nothing has "
        "matched, so there is nothing to cluster.\n"
        "\"slices\", on its own: the host's raw lines of a day, cut into slices and "
        "waiting for you — each with its span, one line of what it is, and the entries "
        "from that day that look like they already record it. Handle each one: not "
        'recorded → grow(..., slice="sl_…"); recorded → trace(bucket_id=…, '
        'slice="sl_…"); cut wrong → trace(slice="sl_…", slice_span=…) or drop_slice=True.'
    ))] = "",
) -> str:
    """Look back through memories that are already stored.

    Four filters. Give at least one:
      when    a stretch of time
      room    which room
      tag     a tag
      query   words to search on

    query is what you are looking for; the other three are where to look. What comes back
    depends on whether you give a query at all:

    · With a query: entries are matched and scored, newest first. Anything below the line
      is not thrown away: it collapses into a single line telling you how many were held
      back, the highest score among them, and what the oldest one was about.
      Use one or two words, and use the words that were actually written at the time.
      A long phrase gets averaged out in the vector and finds less, not more.

    · Without a query: the stretch is laid out by time instead. The last few days come back
      entry by entry; anything older collapses into a sentence with two or three
      representatives.
      This is the one to use when you don't know what you are looking for and just want to
      see what was there.

    Not seeing something this way does not mean it is gone. Memories that have not been
    thought about in a long time stop surfacing on their own, but they are still there and
    still searchable.

    When to use:
    · The user refers back to something: "that thing I told you about last time…"
    · The user says what they want
    · You suspect this has come up before

    If two attempts turn up nothing, stop and tell the user plainly that you cannot find it.
    Do not keep rewording the search. That is how you end up inventing an answer.

    Example — look through a stretch of time:
      recall(when="last week")

    Example — find one thing:
      recall(query="青岛")

    Example — find one thing inside a stretch of time (where to look + what to look for):
      recall(when="上月", query="青岛")

    Example — read one entry word for word:
      recall(query="a1b2c3d4e5f6")
      Passing a full bucket_id as the query returns that entry verbatim, along with its
      metadata, where it came from, and what has cited it."""
    return await _with_notice(
        _t_recall.dispatch(when=when, room=room, tag=tag, query=query,
                           slices=int(slices or 0), view=view),
        op="recall",
        args={"when": when, "room": room, "tag": tag,
              "query_len": len(query or ""), "slices": slices, "view": view},
    )


# --- A removed parameter must be **unrecognized**, never silently ignored --------------
# FastMCP by default **quietly drops** fields that are not in the schema and then calls the
# function. So an old spelling like `by="..."` degrades silently into the default view —
# the caller believes they are looking at the full, uncollapsed list and are handed a
# collapsed screen instead, **with no signal whatsoever**.
# That is the mirror image of the disease the parameter cleanup was meant to cure: a knob
# that no longer exists but still appears to be doing something.
# Following the precedent of breath's compatibility adapter just above, recall's parameter
# model is set to forbid, so an unknown or misspelled parameter raises immediately.
# **The error exists to be read**: wherever the manual still teaches `by=`, the first call
# that spells it that way finds out it is gone.
try:
    _recall_tool = mcp._tool_manager.get_tool("recall")
    if _recall_tool is None:
        raise RuntimeError("registered recall tool is missing")
    _recall_arg_model = _recall_tool.fn_metadata.arg_model
    _recall_arg_model.model_config["extra"] = "forbid"
    _recall_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _recall_strict_exc:
    logger.warning(
        "recall strict-argument adapter unavailable（砍掉的参数会被静默忽略，"
        "别信 by= 已经死了）: %s",
        _recall_strict_exc,
    )


@mcp.tool()
async def fold(
    text: Annotated[str, _PydField(description=(
        "The line itself. Always yours to write, stored exactly as written."
    ))],
    room: Annotated[str, _PydField(description=(
        "One of the four rooms. Naming a stretch of days (with when) takes an EVENT "
        "room; gathering realizations (with folds) takes a MIND room."
    ))] = "",
    v: Annotated[float, _PydField(description=(
        "valence, 0~1. Required, and yours to set."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "arousal, 0~1. Required, and yours to set."
    ))] = -1,
    # On the tool surface this parameter is `folds`, matching the tool's own metaphor:
    # folding, not covering.
    # WARNING: underneath, and **on disk**, it is still cover / covered_by. The storage
    #    fields are deliberately not renamed — renaming them would mean migrating the entire
    #    store. This only changes the name the model sees to the right one.
    folds: Annotated[list, _PydField(description=(
        "The entries to fold up. Real bucket_ids. Realizations only.\n"
        "⛔ There is no folding a group of events: to mark off a stretch of days use\n"
        "   when, and to follow one thread across time use recall with a query."
    ))] = [],
    when: Annotated[str, _PydField(description=(
        '"start..end", for naming a stretch of days. Leave the end open while it is\n'
        'still running: "2026-07-31..".\n'
        "Give this or folds, never both."
    ))] = "",
    # The public parameter name is "from", as in grow and as the spec requires. `from` is a
    # Python keyword, so the signature spells it from_ and pydantic's public
    # validation_alias catches it.
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "What this line grew out of, at most 64.\n"
        "⚠️ Not the same thing as folds:\n"
        "   from   what it grew out of. Those entries go on surfacing normally.\n"
        "   folds  what it covers. Those entries stop surfacing on their own.\n"
        "Both can be given at once."
    ))] = [],
    test_data: Annotated[bool, _PydField(description=(
        "Marks the entry as test data. Do not pass it in normal use."
    ))] = False,
    sources: Annotated[Optional[list | dict | str], _PydField(description=(
        "The host's own material this line came from, as in grow: "
        "[{system, instance, container, id, …}]."
    ))] = None,
) -> str:
    """Fold entries up under one line you write yourself.

    Nothing underneath is lost. Folded entries stay searchable, stay reachable by drilling
    in, and stay readable word for word by their id. They simply stop taking up a line of
    their own when you are looking back.

    When to use:
    · After muse, looking at what it laid out, you can see those really are one thing
    · A stretch of days is over and you want to give it a name

    Two ways to fold, told apart by which parameter you give:

    · folds=[several ids] with room set to one of the MIND rooms
      Gathers realizations that are about the same thing, and that feel alike, under the one
      line you write. The ones you name stop surfacing on their own and give way to it.

    · when="start..end" with room set to one of the EVENT rooms
      Gives a stretch of days a name. That is a period, and it holds nothing but its name
      and its range: not one memory is pinned down by it. Who belongs to a period is worked
      out from the dates every time it is read, so anything written down later falls into
      place on its own, and periods can overlap and sit inside one another.

    The line is always yours to write, and it is stored exactly as you wrote it.
    Give folds or when, never both.

    Do not use this tool when:
    · One entry has a newer version. Use regrow.

    Example — several realizations under one line:
      fold(folds=["a1b2c3d4e5f6", "b2c3d4e5f6a1", "c3d4e5f6a1b2"],
           room="MIND/TRAITS",
           text="The moment I get impatient I start making her decisions for her.",
           v=0.4, a=0.6)

    Example — giving a stretch of days a name:
      fold(when="2026-08-15..2026-08-18", room="EVENT/SELF",
           text="The stretch where we moved the memory system onto a name of our own.",
           v=0.8, a=0.5)"""
    return await _with_notice(
        _t_fold.dispatch(text=text, room=room, v=v, a=a, cover=folds,
                         when=when, from_=from_, test_data=bool(test_data),
                         sources=sources),
        op="fold",
        args={"text_len": len(text or ""), "room": room, "v": v, "a": a,
              "folds": folds, "when": when, "from": from_,
              "test_data": bool(test_data),
              "sources": len(sources) if isinstance(sources, list) else bool(sources)},
    )


# --- A removed or renamed parameter must be **unrecognized** ---------------------------
# `cover` was renamed to `folds`. FastMCP by default **quietly drops** fields that are not
#    in the schema and then calls the function — which means the old spelling
#    `fold(cover=[...])` **silently folds nothing at all**, without a single word of error.
#    breath, grow, recall and trace got forbid; fold and muse were missed.
#    The error exists to be read: the first call that uses the old spelling finds out it is
#    gone.
try:
    _fold_tool = mcp._tool_manager.get_tool("fold")
    if _fold_tool is None:
        raise RuntimeError("registered fold tool is missing")
    _fold_arg_model = _fold_tool.fn_metadata.arg_model
    _fold_arg_model.model_config["extra"] = "forbid"
    _fold_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _fold_strict_exc:
    logger.warning("fold strict-argument adapter unavailable: %s", _fold_strict_exc)


@mcp.tool()
async def muse(
    cluster: Annotated[int, _PydField(description=(
        "Read cluster N in full, word for word. Leave it out to see only which "
        "clusters and hints are there."
    ))] = 0,
    not_same: Annotated[list, _PydField(description=(
        "The set that turned out not to be one thing. Real bucket_ids. Recorded, so "
        "the same set is not raised again; a changed set comes back."
    ))] = [],
) -> str:
    """Muse: find which entries are about the same thing, and lay them out in front of you.

    It points; it does not write. The line that gathers them is always yours (use fold).

    Two steps:
    · muse()           see which clusters and which hints are there. Evidence and counts
                       only, no memory text.
    · muse(cluster=N)  read cluster N in full, word for word, then decide for yourself
                       whether to fold it.

    Everything laid out comes with its evidence, and nothing without evidence is shown:
    · For realizations, evidence is ordered by how hard it is: valence/arousal coordinates,
      then from links, then semantic similarity (the vector is only a first pass, and
      anything it brought in is marked as such). Time plays no part; realizations do not go
      by calendar.
    · For events, what it looks for is which stretch of days has no name yet: a scene word
      that appears densely inside a stretch and not outside it; the vector centroid jumping
      between one time window and the next; or a stretch that falls into no period at all.

    Anything just written is left out, and nothing still unfolding is pointed at.

    Use this when you have been told that a few clusters are waiting.

    If you look and decide they are not one thing after all, muse(not_same=[id, id]) puts
    that on record and the same set is not raised again. Change the set, by one entry either
    way, and it comes back.

    Do not use this tool when:
    · You are looking for something. Use recall."""
    return await _with_notice(
        _t_muse.dispatch(cluster=int(cluster or 0), not_same=not_same),
        op="muse",
        args={"cluster": cluster, "not_same": not_same},
    )


# Same as above. muse never had a parameter renamed, but one typo (`clusters=`,
# `not_same_ids=`) is silently ignored just the same — and a tool whose whole purpose is
# "I just want to look" must not lie: it hands back the default screen without a word, and
# that screen looks exactly like the one that was asked for.
try:
    _muse_tool = mcp._tool_manager.get_tool("muse")
    if _muse_tool is None:
        raise RuntimeError("registered muse tool is missing")
    _muse_arg_model = _muse_tool.fn_metadata.arg_model
    _muse_arg_model.model_config["extra"] = "forbid"
    _muse_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _muse_strict_exc:
    logger.warning("muse strict-argument adapter unavailable: %s", _muse_strict_exc)


@mcp.tool()
async def regrow(
    bucket_id: Annotated[str, _PydField(description=(
        "The entry being replaced. A real bucket_id."
    ))],
    text: Annotated[str, _PydField(description=(
        "The new version, written whole: what the entry now reads like from start "
        "to finish, not the part that changed."
    ))],
    v: Annotated[float, _PydField(description=(
        "valence, 0~1. Required, and yours to set. Replacing an entry "
        "means you weighed it again, so weigh the feeling again too."
    ))] = -1,
    a: Annotated[float, _PydField(description=(
        "arousal, 0~1. Required, and yours to set. Replacing an entry "
        "means you weighed it again, so weigh the feeling again too."
    ))] = -1,
    from_: Annotated[list, _PydField(validation_alias="from", description=(
        "Any new sources this version came out of (bucket_ids or the host's line ids); "
        "at most 64 in all, counting the ones carried over. The old version's "
        "sources carry over on their own, so only name what is new."
    ))] = [],
    mode: Annotated[str, _PydField(description=(
        'Required. "supplement": the old version was right as far as it went, you are '
        'adding or rewording; nothing that grew out of it is touched. "overturn": the '
        "old version was wrong; everything that grew out of it is marked as standing on "
        "changed ground. Without it nothing is written."
    ))] = "",
    sources: Annotated[Optional[list | dict | str], _PydField(description=(
        "Any new pieces of the host's material this version came from, as in grow. The "
        "old version's carry over on their own, except one the host has since withdrawn "
        "or deleted, which is left behind and named in the reply."
    ))] = None,
) -> str:
    """Replace an entry that is wrong, or that is no longer how you see it.

    The new version takes its place. The old one is kept, not edited. It stops surfacing on
    its own, but you can still search it and still read it word for word by its id. The two
    stay linked, so it is always clear which came first. The new version keeps where the old
    one stood: its day, whether it is wanted and by whom, its weight, its status, who it is
    about, its tags. Only the words and the feeling are yours to set again.

    When to use:
    · You see it differently now than you did then
    · An entry came out wrong in its own words: what was said, who said it, what happened
    · A period needs a different name

    Write the new version whole. It replaces the entry outright; it is not a patch.

    Say which kind of change it is, every time:
      mode="supplement"  the old version holds; you are adding detail or saying it better.
                         Entries that grew out of it (their from names it) stand as they were.
      mode="overturn"    the old version was wrong. Every entry that grew out of it, and out
                         of those, gets a mark saying its basis changed; they keep surfacing,
                         and reading one by id shows the mark. Whether each still holds is a
                         call you make when you meet it.

    Do not use this tool when:
    · What is wrong is not what the entry says. Which room it is in, which day it hangs
      on in time, its tags, how it felt — all of that is metadata, and metadata is trace's:
      correcting it is correction fluid, not a new draft, and it leaves no version behind.
    · Several entries turn out to be about the same thing. Use fold.
    · This entry has already been replaced once. Regrow the newest version instead of
      branching off an old one; branching is rejected.

    Example — your thinking moved on:
      regrow(bucket_id="a1b2c3d4e5f6", mode="overturn",
             text="I'm not impatient. I'm afraid of keeping her waiting.",
             v=0.4, a=0.6)

    Example — an event came out wrong:
      regrow(bucket_id="b2c3d4e5f6a1", mode="overturn",
             text="It was Tuesday, not Wednesday, and she arrived in the afternoon.",
             v=0.3, a=0.4)

    Example — the same event, told more fully:
      regrow(bucket_id="b2c3d4e5f6a1", mode="supplement",
             text="She arrived in the afternoon, soaked, and laughed about the rain.",
             v=0.7, a=0.5)

    Example — a period needs a different name:
      regrow(bucket_id="c3d4e5f6a1b2", mode="supplement",
             text="The stretch where we moved the memory system onto a name of our own.",
             v=0.8, a=0.5)"""
    return await _with_notice(
        _t_regrow.dispatch(bucket_id=bucket_id, text=text, v=v, a=a, from_=from_,
                           mode=mode, sources=sources),
        op="regrow",
        args={"bucket_id": bucket_id, "text_len": len(text or ""), "v": v, "a": a,
              "from": from_, "mode": mode,
              "sources": len(sources) if isinstance(sources, list) else bool(sources)},
    )


# --- regrow must also fail to recognize removed parameters (**third time, same trap**) --
# `regrow` dropped `when` and `room`; metadata belongs to trace now. But it was the
#    **only one of the eight tools without forbid** — so `regrow(bucket_id=...,
#    room="MIND/VIEWS")` **neither errors nor takes effect**: FastMCP quietly drops the
#    field that is not in the schema and calls the function, the room is untouched, and the
#    receipt says nothing about it.
#    **Accepting it silently is far worse than an error: the caller believes it changed, and
#    it did not.**
# The history of this trap: forbid was added to breath/grow/recall/trace, missing fold and
#    muse; the pass that added fold and muse (just above) **missed regrow**.
#    That is three times — so the rule is no longer "remember to add it for each new tool",
#    it is **that a smoke test goes red because of it** (`smoke_grow`). A list kept by human
#    memory will be missed eventually. An assertion will not.
try:
    _regrow_tool = mcp._tool_manager.get_tool("regrow")
    if _regrow_tool is None:
        raise RuntimeError("registered regrow tool is missing")
    _regrow_arg_model = _regrow_tool.fn_metadata.arg_model
    _regrow_arg_model.model_config["extra"] = "forbid"
    _regrow_arg_model.model_rebuild(force=True)
except (AttributeError, RuntimeError, TypeError, ValueError) as _regrow_strict_exc:
    logger.warning("regrow strict-argument adapter unavailable: %s", _regrow_strict_exc)


@mcp.tool()
async def trace(
    bucket_id: Annotated[str, _PydField(description=(
        "Which entry to change. Required, except to re-cut or drop a slice."
    ))] = "",
    name: Annotated[Optional[str], _PydField(description=(
        "The entry's title."
    ))] = "",
    domain: Annotated[Optional[str], _PydField(description=(
        "Its subject, which also decides the folder it lives in."
    ))] = "",
    valence: Annotated[float, _PydField(description=(
        "0~1."
    ))] = -1,
    arousal: Annotated[float, _PydField(description=(
        "0~1."
    ))] = -1,
    tags: Annotated[Optional[str], _PydField(description=(
        "Its tags."
    ))] = "",
    pinned: Annotated[int, _PydField(description=(
        "1 pins it as a principle; 0 unpins. Nothing is refused: if it does not read "
        "like something you mean to do, it is still pinned and you get a note back."
    ))] = -1,
    delete: Annotated[bool, _PydField(description=(
        "True moves it to the archive. A soft delete; it can always be brought back."
    ))] = False,
    status: Annotated[Optional[str], _PydField(description=(
        '"resolved" done / "abandoned" not doing it / "active" back on the table.'
    ))] = "",
    direction_of_fit: Annotated[Optional[str], _PydField(description=(
        '"telic": this is something wanted — a promise, a plan, a wish ("finish her gift '
        'before her birthday"). "thetic": a record of how things are or were. Change it '
        "when an entry was stored as the wrong one."
    ))] = "",
    bound: Annotated[Optional[list], _PydField(description=(
        "For something wanted: who is bound by it. Names, or 我 for yourself and 你 for "
        'the person you talk to. ["我"] you owe it; ["我", "<her name>"] you both agreed; '
        "[] just a wish, nobody owes anything. Other pronouns are refused: write the name."
    ))] = None,
    cue: Annotated[Optional[dict | str], _PydField(description=(
        'What this entry is waiting for: {"condition": "<the event, one sentence>"}. '
        "Replaces any cue it had; the condition is required. An empty string takes the "
        'cue off. e.g. cue={"condition": "he is back from the trip"}.'
    ))] = None,
    card_of: Annotated[Optional[str], _PydField(description=(
        "Make this mind entry the card of a name (a person in MIND/TRAITS, a thing in "
        "MIND/VIEWS; one card per name). An empty string takes it off as a card. "
        'e.g. card_of="Detroit".'
    ))] = None,
    room: Annotated[str, _PydField(description=(
        "Move the entry to another room. Which room it is in is metadata: it says what "
        "kind of thing this is, not what the entry says, so changing it leaves no version "
        "behind. Wrong or unknown rooms are refused here exactly as they are anywhere else."
    ))] = "",
    when: Annotated[str, _PydField(description=(
        "Where this entry hangs in time. An ordinary entry takes the day it happened "
        '("2026-07-06"); something wanted takes a date or a length ("3w"); a period takes its range '
        '("2026-07-31..2026-08-05"); a hold takes the day it ends or the days it covers. '
        'Wrong shapes are refused. Written entries carry the '
        "day they were written until you say otherwise, which is not always the day the "
        "thing happened."
    ))] = "",
    folds_append: Annotated[list, _PydField(description=(
        "Put a few more entries underneath a gist you already wrote. Append only: the "
        "line itself does not change, only which entries it now stands for. There is no "
        "way to hand in a replacement list, because forgetting one id would quietly let "
        "that entry surface again."
    ))] = [],
    sources_append: Annotated[Optional[list | dict | str], _PydField(description=(
        "Add pieces of the host's material this entry was formed from, as grow's sources "
        "takes them. Append only; a withdrawn or deleted source is refused."
    ))] = None,
    slice: Annotated[str, _PydField(description=(
        "A pending slice of the host's raw lines (the \"sl_…\" id from "
        'recall(view="slices")). With bucket_id: this entry already records it, and the '
        "slice is appended to its sources as one record. Without one: slice_span re-cuts it, "
        "drop_slice drops it."
    ))] = "",
    slice_span: Annotated[str, _PydField(description=(
        "With slice, no bucket_id: the slice's lines were cut wrong; its new span as "
        '"first_id..last_id" (one line: just that id), within the lines it was cut from. '
        "Its gist stays as it was."
    ))] = "",
    drop_slice: Annotated[bool, _PydField(description=(
        "With slice, no bucket_id: nothing in it is worth keeping. It is dropped and "
        "attached to nothing."
    ))] = False,
    weight: Annotated[float, _PydField(description=(
        "Something wanted only: how heavily it sits on you, 0~1."
    ))] = -1,
    dont_surface: Annotated[int, _PydField(description=(
        "1 stops it from coming up on its own. It stays searchable."
    ))] = -1,
    media_append: Annotated[Optional[list | str], _PydField(description=(
        "Attaches media (an image, say) to the entry."
    ))] = None,
    media_replace: Annotated[Optional[list | str], _PydField(description=(
        "Replaces the whole media list."
    ))] = None,
    hard_delete: Annotated[bool, _PydField(description=(
        "True deletes it for real. Only works on entries created with test_data, "
        "and delete_reason must be given."
    ))] = False,
    delete_reason: Annotated[Optional[str], _PydField(description=(
        "Goes with hard_delete."
    ))] = "",
    restore: Annotated[bool, _PydField(description=(
        "True brings it back, from the archive or from having sunk to a summary."
    ))] = False,
    old_str: Annotated[Optional[str], _PydField(description=(
        "The passage to replace. Must match word for word and appear only once."
    ))] = "",
    new_str: Annotated[Optional[str], _PydField(description=(
        "What to put there. Empty cuts the passage out."
    ))] = None,
) -> str:
    """Change an entry that is already stored: pin it, close it, archive it, or change one of
    its fields.

    Pass only what you are changing. Anything you leave out stays as it was.

    Pinning:
      pinned=1 pins the entry to the first screen of breath, the few lines you see before you
      say anything.
      🔴 What belongs here is "how I mean to act", not "this one matters". Nothing is
         refused, though: a sentence that reads as description still gets pinned, and a
         note comes back with it. The one thing worth catching is a flaw pinned as a
         principle — pin "I always rush" and it reads as "I intend to keep making this
         mistake" — and no check can tell that apart from a description worth keeping.
         That call is yours. Scarcity is held by the cap, not by the wording.
      pinned=0 unpins.

    Closing something you wanted:
      status="resolved"   done
      status="abandoned"  not doing it
      status="active"     back on the table
      🔴 Nothing closes itself, and a date passing does not close it either. There are only
         these two endings, and both are yours to call, once you have seen what actually
         happened. What was done is its own entry: grow a thetic event with from pointing
         at the wanted one, so "what I meant to do" and "what happened" both stay.
      Whether something is wanted at all is direction_of_fit, not status.
      A hold (grown with exception_of) is closed the same way: status="resolved" on the
      hold lifts it early, and the agreement it was hung on comes back. A dated hold
      lifts by itself once its last day has passed.

    Archiving and bringing back:
      delete=True   moves it to the archive and timestamps it. Nothing is really deleted;
                    looking it up by id always brings it back.
      restore=True  brings it back, whether it was archived or had sunk to a summary (the
                    original is read back out of storage, and it counts as genuinely
                    remembering it).

    Editing the text:
      old_str / new_str  replaces one passage, matched word for word and only if unique.
                         Leave new_str empty to cut the passage out.
                         🔴 This edits what is on disk and keeps no earlier version. If you
                            want the earlier version kept, use regrow instead.

    Changing fields:
      name / domain / tags / valence / arousal / weight / dont_surface / room / when /
      direction_of_fit / bound / cue / card_of
      Everything here is metadata: what kind of thing this is, where it hangs in time,
      how it felt. None of it is what the entry says, so none of it leaves a version
      behind — this is correction fluid, not a new draft. The moment the words themselves
      have to change, that is regrow.

    Putting more under a gist:
      folds_append=[ids]  the line stays as written; only what it stands for grows.

    Naming more of where it came from:
      sources_append=[{system, instance, container, id, …}]  more of the host's material
                         this entry was formed from; append only.

    A pending slice of the host's raw lines (recall(view="slices") lists them):
      bucket_id + slice="sl_…"        this entry already records it: the slice joins the
                                       entry's sources as one record
      slice="sl_…", slice_span="m_0012..m_0031"   it was cut wrong: move its span
      slice="sl_…", drop_slice=True   nothing in it to keep
      (Not recorded anywhere yet: grow(..., slice="sl_…").)

    Do not use this tool when:
    · The words themselves are wrong or have moved on. Use regrow, which keeps the old
      version instead of writing over it.
    · The entry has a newer version. Use regrow."""
    return await _with_notice(
        _t_trace.dispatch(
            bucket_id=bucket_id, name=name, domain=domain,
            valence=valence, arousal=arousal,
            tags=tags, pinned=pinned, room=room, when=when,
            folds_append=folds_append,
            delete=delete, status=status, weight=weight,
            dont_surface=dont_surface,
            media_append=media_append, media_replace=media_replace,
            hard_delete=hard_delete, delete_reason=delete_reason,
            restore=restore,
            old_str=old_str, new_str=new_str,
            direction_of_fit=direction_of_fit, bound=bound, cue=cue, card_of=card_of,
            sources_append=sources_append,
            slice_id=slice, slice_span=slice_span, drop_slice=drop_slice,
        ),
        op="trace",
        args={
            "bucket_id": bucket_id, "name": name, "domain": domain,
            "valence": valence, "arousal": arousal,
            "tags": tags, "pinned": pinned, "room": room, "when": when,
            "folds_append": folds_append,
            "delete": delete, "status": status,
            "direction_of_fit": direction_of_fit, "bound": bound, "cue": cue,
            "card_of": card_of,
            "sources_append": (len(sources_append) if isinstance(sources_append, list)
                               else bool(sources_append)),
            "slice": slice, "slice_span": slice_span, "drop_slice": drop_slice,
            "hard_delete": hard_delete,
            "restore": restore,
            "delete_reason_len": len(str(delete_reason or "")),
            "old_str_len": len(str(old_str or "")),
            "new_str_len": len(str(new_str or "")) if new_str is not None else 0,
            "weight": weight, "dont_surface": dont_surface,
            "media_append_count": len(media_append or []),
            "media_replace_count": len(media_replace or []),
        },
    )


# Reject misspelled/unknown trace arguments instead of letting Pydantic's
# default extra=ignore silently degrade an intended edit into a bucket-id-only
# no-op.  This is especially important for old_str/new_str patch calls.
# This gate now doubles as the guard against **seven removed parameters**: `content`,
#    `importance`, `digested`, `meaning_append`, `meaning_replace`, `why_remembered` and
#    `resolved`.
#    Among them, `content` (replace the whole body) was the only entry point in the system
#    that **changed the original text without keeping the old version** — duplicating regrow
#    and more dangerous than it. `why_remembered` was the same thing as the retired
#    `meaning`: "why I remember it" and "why it matters" are one question. Anything worth
#    saying there should be written as a real realization instead.
#    WARNING: 141 older buckets on disk still carry the why_remembered field. **Not one row
#    of data was touched**; new ones simply are not written.
try:
    _trace_public_tool = mcp._tool_manager.get_tool("trace")
    if _trace_public_tool is None:
        raise RuntimeError("registered trace tool is missing")
    # All three steps (forbid · rebuild · re-publish the cached schema) live in
    # core/strict_args.harden — see that module for what happens when a caller copies
    # only the first two, which is what the sweep below used to do.
    _harden_tool(_trace_public_tool)
except (AttributeError, RuntimeError, TypeError, ValueError) as _trace_schema_exc:
    logger.warning(
        "trace strict-argument adapter unavailable: %s",
        _trace_schema_exc,
    )






# `pulse` was withdrawn from the MCP tool surface.
#    The reasoning: the other nine tools are all "what am I doing to a memory", and this one
#    alone is "is this machine healthy" — a health check is not a memory action, and should
#    not occupy a tool slot. **Disabled, not deleted**: the implementation is still in
#    `tools/pulse/`, reached through the panel's read-only `GET /api/loci/pulse`
#    (see web/loci.py).




# ============================================================
# The two letter tools: **off by default**
# ============================================================
# The position: the version handed to other people does not include this. Anyone who wants
# it can turn it on, but that is their decision to make.
# It is a switch rather than a real deletion so that the code **does not fork**: one
#    codebase, off by default in the released build, on where it is wanted. Two copies of
#    the code would be exactly the thing this whole cleanup exists to avoid — one thing with
#    two homes, changed in one and forgotten in the other.
#
# To enable, in config.yaml:
#     tools:
#       letter: true
#
# Why it is off by default: a letter **never decays**, and the entire skeleton of this
#    system is built around forgetting. Putting something that never decays inside a system
#    that forgets is upside down.
#    It stays enabled where a separate front-end still reads the letters page; it will be
#    withdrawn there too once letters move.
_letter_on = bool((config.get("tools") or {}).get("letter", False))
_letter_tool = mcp_extra.tool() if _letter_on else (lambda f: f)
if not _letter_on:
    logger.info("letter 工具默认关（config.yaml → tools.letter: true 可开）")


@_letter_tool
async def letter_write(
    author: Annotated[str, _PydField(description=(
        "Who it is from. \"user\" for the person's side, \"ai\" for yours; any signature "
        "string also works."
    ))],
    content: Annotated[str, _PydField(description=(
        "The letter itself, kept word for word."
    ))],
    user_name: Annotated[Optional[str], _PydField(description=(
        "Optional display name for the person's side."
    ))] = "",
    title: Annotated[Optional[str], _PydField(description=(
        "Optional."
    ))] = "",
    date: Annotated[Optional[str], _PydField(description=(
        "Optional; defaults to now."
    ))] = "",
    ai_name: Annotated[Optional[str], _PydField(description=(
        "Optional display name for your side; defaults to the AI_NAME environment "
        "variable."
    ))] = "",
) -> str:
    """Write a letter.

    Letters are kept whole and forever: never compressed, never merged, never faded. They do
    not surface in breath. Only the most recent letter from each side is brought along at the
    start of a session.

    Because a letter never fades, do not use it as a place to put things that should expire.
    Anything that will stop being true belongs in a memory, not a letter.

      letter_write(author="ai", content="…", title="…")"""
    return await _with_notice(
        _t_letter.letter_write(
            author=author, content=content, user_name=user_name,
            title=title, date=date, ai_name=ai_name,
        ),
        op="letter_write",
        args={
            "author": author, "content_len": len(content or ""),
            "user_name": user_name, "title": title, "date": date,
            "ai_name": ai_name,
        },
    )


@_letter_tool
async def letter_read(
    query: Annotated[Optional[str], _PydField(description=(
        "Search by meaning. Leave it out to get the most recent letters in reverse "
        "date order."
    ))] = "",
    limit: Annotated[int, _PydField(description=(
        "How many to return. Defaults to 10."
    ))] = 10,
    author: Annotated[Optional[str], _PydField(description=(
        'Filter by who wrote it: "user", "ai", or a specific signature.'
    ))] = "",
    date_from: Annotated[Optional[str], _PydField(description=(
        "ISO date, optional."
    ))] = "",
    date_to: Annotated[Optional[str], _PydField(description=(
        "ISO date, optional."
    ))] = "",
) -> str:
    """Read letters that have been written.

    Returns them whole, never shortened.

      letter_read(query="…")          search by meaning
      letter_read(author="user")      everything from one side
      letter_read()                   the most recent few, newest first"""
    return await _with_notice(
        _t_letter.letter_read(
            query=query, limit=limit, author=author,
            date_from=date_from, date_to=date_to,
        ),
        op="letter_read",
        args={
            "query": query, "limit": limit, "author": author,
            "date_from": date_from, "date_to": date_to,
        },
    )


# --- The two letter tools must also fail to recognize removed parameters -----------------
# --- (**fourth time, same trap**)
# Counting the nine tools, only seven had this gate: `letter_read` and `letter_write` were
# missed too.
# Case history: forbid was added to breath/grow/recall/trace, missing fold and muse; the
#       next pass added fold and muse and missed regrow; the pass after that added regrow
#       and **missed these two**.
# All three times, the fix was "patch whichever names came to mind at the time". The fourth
#    time does it differently: the block below **iterates the tool registry** and installs
#    the gate on anything that lacks it, so **a tool added later gets it automatically**.
#    The rule: **a list kept by human memory will be missed eventually.** All three previous
#       lessons say the same sentence, and all three times the response was "this time I
#       will remember them all".
# WARNING: the two letter tools are registered **conditionally** (config.tools.letter is off
#    by default), and when they are off they are simply not in the registry — so this can
#    only install the gate where the tool exists, and must not assert that it always does.
try:
    for _tool_name in ("breath", "grow", "recall", "regrow", "fold", "muse", "trace",
                       "letter_write", "letter_read"):
        for _surface in (mcp, mcp_extra):
            _t = _surface._tool_manager.get_tool(_tool_name)
            if _t is None:
                continue
            # ⚠️ This used to inline the flip and **drop the re-publish step** that the
            #    trace block above had, so eight tools rejected unknown arguments while
            #    still advertising that they accepted them. Go through harden(); it is
            #    the only thing that keeps the two halves from drifting apart again.
            if _harden_tool(_t):
                logger.info("strict-argument adapter installed for %s", _tool_name)
except (AttributeError, RuntimeError, TypeError, ValueError) as _strict_all_exc:
    logger.warning("strict-argument sweep unavailable: %s", _strict_all_exc)




# **The `seed` tool was withdrawn from the MCP surface.**
#    Its wrapper was deleted along with `from tools import seed`; the reasoning is in the
#    comment near the imports above.
#    Following the night_fall / `I` / `dream` precedent: **disabled, not deleted** — the two
#    files under `tools/seed/` are still on disk, not one of the thirteen roots' buckets was
#    touched, and the only way back to them is a direct id lookup through `recall`.
#    WARNING: do not casually register it again. Read those three lines of reasoning first,
#    especially "a tool that needs a warning label was never really in hand".
#    Also needs updating: the four places the docs mention seed — the tool count, the red
#       line in the "when to reach for it" table, the "name it with seed first" paragraph
#       under `grow`, and the "seed is only touched at two moments" note under fold and
#       digestion.



# =============================================================
# Dashboard API endpoints (for lightweight Web UI)
# =============================================================
# =============================================================
# /api/buckets, /api/bucket/*, /api/settings/*, /api/anchors and /api/self
# have moved to web/buckets.py
# =============================================================


# =============================================================
# /dashboard, /api/env-vars, /api/config, /api/test/*, /api/models and /api/env-config
# have moved to web/config_api.py
# =============================================================




# =============================================================
# /api/host-vault, /api/import/*, /api/bucket/{id}/edit, /api/export and /api/migrate/*
# have moved to web/import_api.py
# =============================================================


# =============================================================
# /api/version, /api/update-info, /api/do-update, /api/author, /api/onboarding/status and
# /api/status have moved to web/meta.py
# =============================================================


# ============================================================
# OAuth 2.0 — MCP remote auth. Moved to bridge/oauth.py in the strip-down: this is not a
# panel route, it is how a remote client authenticates to /mcp itself. "Auth is on by
# default" is a settled position for the open-source build, and it must not die along with
# the panel (bridge/__init__.py carries the full reasoning).
# The two validators the start-time MCP auth middleware needs are imported back here:
# mcp_auth_mode=="oauth" (the default) uses _is_valid_mcp_token, mcp_auth_mode=="token" uses
# _is_valid_static_mcp_token, and one of the two is injected into the middleware.
# ============================================================
from bridge.oauth import _is_valid_mcp_token, _is_valid_static_mcp_token  # noqa: F401


# ============================================================
# Cloudflare Tunnel management was **removed entirely**.
# The decision rested on facts rather than guesses: the live config.yaml had zero tunnel
# configuration and the live container's startup logs had zero tunnel entries — it was never
# used, because outbound traffic goes through a self-hosted gateway's domain.
# The open-source build does not carry it either: anyone who wants public exposure can
# configure their own reverse proxy, which sits better with the data-sovereignty position.
# All three lifespan hooks — load_tunnel_config, start_tunnel, stop_tunnel — came out with
# it. server_app.py needed no change: those three fields on RuntimeLifecycle were already
# Optional, and omitting them simply means "no tunnel".
# ============================================================


# --- Entry point ---
if __name__ == "__main__":
    transport = config.get("transport", "stdio")
    logger.info(f"Loci Brain starting | transport: {transport}")

    # Merged into a single /mcp connector.
    # The original /mcp + /mcp-extra split existed because the client imposed a five-tool
    # limit per connector. That limit is gone, so every tool hangs off the primary mcp
    # instance behind one /mcp route — which also removes the OAuth and connector-validation
    # awkwardness that a second connector caused on the client side.
    # mcp_extra survives only as a historical grouping container (its seven
    # @mcp_extra.tool() registrations are unchanged); its tools are folded back into mcp
    # here, so stdio, sse and streamable-http all expose the same set.
    # This depends on FastMCP._tool_manager, a private structure. If a future version
    # changes it, this degrades to exposing the primary set only.
    from server_app import (
        HTTPRuntimeSettings,
        RuntimeLifecycle,
        build_http_app,
        merge_mcp_tool_registries,
    )

    try:
        _extra_count = merge_mcp_tool_registries(mcp, mcp_extra)
        logger.info(
            f"单连接器 /mcp：已把 {_extra_count} 个副集工具回灌进主实例，共 "
            f"{len(mcp._tool_manager._tools)} 个工具对外暴露"
        )
    except AttributeError as _merge_exc:
        logger.warning(
            f"FastMCP 内部结构变化，工具回灌失败，仅暴露主集工具：{_merge_exc}"
        )

    if transport in ("sse", "streamable-http"):
        import uvicorn
        from bridge import ollama_child as _ollama_child

        _http_settings = HTTPRuntimeSettings.from_config(config)
        _runtime_lifecycle = RuntimeLifecycle(
            logger=logger,
            decay_engine=decay_engine,
            embedding_outbox=embedding_outbox,
            ensure_ollama_child=_ollama_child.ensure_child_on_boot,
            stop_ollama_child=_ollama_child.stop_child,
            # Tunnel support was removed entirely: load_tunnel_config, start_tunnel and
            # stop_tunnel were already Optional[...] = None, so omitting them means
            # "no tunnel".
            restart_github_auto_task=_restart_github_auto_task,
            github_auto_interval=_gh_auto_interval,
            boot_marker_path=os.path.join(
                os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                ".boot_fails",
            ),
            # Explicit IPv4 avoids localhost resolving to ::1 in Proot/Termux.
            keepalive_url=f"http://127.0.0.1:{LOCI_PORT}/health",
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
        )
        # (The tool count is not reported here. The line above, about folding N secondary
        #  tools into the primary instance for M exposed in total, reports the real number.
        #  This line used to hardcode "14 tools", which became a lie the moment the count
        #  dropped to nine.)
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
