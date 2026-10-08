"""
========================================
web/loci_health.py — the health check, the settings page's top block and the log tail
========================================

    GET  /api/loci/health             -> this project's own health check (not the upstream diagnostics endpoint):
                                         every check, and the setting page's five rows over them
    GET  /api/loci/setup              -> the settings page's top block: five status rows, each saying what breaks if it is left unset
    GET  /api/logs                    -> the tail of server.log

The checks behind health and setup are core/health.py's; the builders here hand in what
the web side holds (sh's config and engines, the panel lock) and the routes turn the dict
into JSON.
========================================
"""

import os

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from core import health as _health

logger = sh.logger


async def build_setup() -> dict:
    """The settings page's top block (core/health.setup), over what this process serves.

    The panel lock is web/panel_auth.py's, so it is read here and handed in: whether the
    gate is locked, whether a password is set, the bridge's key (read only when locked),
    the hosts table. Each read that fails reads as its safe empty value.
    """
    locked = False
    try:
        from . import panel_auth as _pa
        locked = bool(_pa.gate_needed())
    except Exception:                                # noqa: BLE001
        locked = False
    has_pw = False
    try:
        has_pw = sh._load_password_hash() is not None
    except Exception:                                # noqa: BLE001
        has_pw = False
    key = ""
    if locked:
        try:
            from web.panel_auth import hook_token
            key = hook_token()
        except Exception:                            # noqa: BLE001
            key = ""
    try:
        from . import panel_auth as _pa
        hs = _pa.hosts()
    except Exception:                                # noqa: BLE001
        hs = None
    panel = _health.PanelLock(locked=locked, has_password=has_pw, hook_key=key, hosts=hs)
    # The engine is read off `sh` on every call: hot reload replaces the instance by
    # assigning to `sh.embedding_engine`, and a captured reference would keep probing the
    # engine that is no longer in use.
    return await _health.setup(
        sh.config, sh.bucket_mgr, getattr(sh, "embedding_engine", None), panel,
        bool(sh.in_docker()) if hasattr(sh, "in_docker") else None)


async def build_health() -> dict:
    """Our own health check (core/health.health), over the library and config this process
    serves; `sh.data_dir_persistence` says whether the data directory survives a rebuild,
    `sh.version` is the version this process started with."""
    return await _health.health(sh.bucket_mgr, sh.config, sh.data_dir_persistence,
                                running=str(getattr(sh, "version", "") or ""))


async def api_loci_health(request: Request) -> Response:
    """Our own health check. The upstream /api/system/diagnostics checks release
    compliance, not whether this memory is doing well."""
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_health())
    except Exception as e:
        logger.warning(f"[loci] health 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_setup(request: Request) -> Response:
    """The screen at the top of the settings page: five status rows plus read-only facts.
    **Read-only.**

    Why it exists: all five failures encountered in one day were silent, and not one of
    them raised an error. This endpoint's job is not to collect settings, it is to turn
    the silent things into visible ones.
    """
    from starlette.responses import JSONResponse
    try:
        return JSONResponse(await build_setup())
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] setup 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_logs(request: Request) -> Response:
    """Logs: read the tail of server.log. **Read-only.**

    The panel's whole log section calls it; without it the section gets HTML back from a
    404 and blows up in the front-end's `.json()`. The writing side is
    utils.setup_logging, which writes to <buckets>/.logs/server.log.

    `level` filters upward by severity: choosing WARNING also returns ERROR and CRITICAL.
    Someone selecting "warnings" wants to know whether anything is wrong, not "warnings
    but please hide the errors".
    """
    from starlette.responses import JSONResponse
    q = request.query_params
    level = (q.get("level") or "WARNING").strip().upper()
    try:
        limit = max(1, min(2000, int(q.get("limit") or 200)))
    except (TypeError, ValueError):
        limit = 200
    path = os.environ.get("LOCI_LOG_FILE", "").strip()
    if not path or not os.path.exists(path):
        return JSONResponse({
            "lines": [], "log_file": path,
            "note": "还没有日志文件（LOCI_LOG_FILE 没设，或者这次启动没开文件日志）。",
        })
    rank = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
    floor = 0 if level == "ALL" else rank.get(level, 30)
    try:
        # Read only the tail: the log rotates at 5MB, and reading all of it is pure waste
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 1024 * 1024))
            raw = f.read().decode("utf-8", "replace")
        lines = raw.splitlines()
        if size > 1024 * 1024 and lines:
            lines = lines[1:]                    # drop the first line, which was cut in half
        keep = []
        for ln in lines:
            if floor:
                hit = next((lv for lv in rank if f" {lv}:" in ln or f" {lv} " in ln), "")
                if not hit or rank[hit] < floor:
                    continue
            keep.append(ln)
        return JSONResponse({
            "lines": keep[-limit:],
            "log_file": path,
            "level": level,
            "note": "" if keep else f"{level} 这一档下没有东西。",
        })
    except Exception as e:                       # noqa: BLE001
        logger.warning(f"[loci] logs 失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
