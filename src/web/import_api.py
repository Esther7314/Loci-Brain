"""
========================================
web/import_api.py — the four import routes
========================================

**The engine was alive the whole time. Only the doorway was missing.**

`core/import_memory.py` (53KB, understands Claude/ChatGPT JSON exports and Markdown) was
never touched, and `server.py` never stopped constructing an `ImportEngine` and injecting
it into `tools/_runtime`. What actually disappeared were the HTTP routes: they lived among
the twenty upstream modules removed in the strip-down and went with them. So the panel's
three buttons — preview / start import / pause — were hitting 404, getting HTML back, and
the front-end's `.json()` blew up on it. What the user saw on screen was "the response
was not JSON".

This module therefore does exactly one thing: **put the doorway back**, without rewriting
a single line of parsing logic.

The four endpoints (paths and request shapes are **copied from the existing front-end
code**, not invented here):
    POST /api/import/preflight   preview — read-only, writes nothing
    POST /api/import/upload      start   — single slot; losing the race returns 409
    GET  /api/import/status      progress — the front-end polls every 1.5s
    POST /api/import/pause       pause   — stops after the current chunk finishes

WARNING: the front-end sends **multipart/form-data** (a `file` field in a FormData), not
   JSON — so loci._write_body cannot be used here, as it only accepts application/json.
   The same-origin gate still goes through loci._origin_reject, shared by every write
   endpoint so they all apply one rule.
Not in scope: the MCP tool surface. Nothing added there, nothing removed.

Public surface: register(mcp)
========================================
"""

import asyncio
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

logger = logging.getLogger("loci_brain.web.import")

# How much may be uploaded at once. **There has to be a ceiling**: both endpoints read the
# whole file into memory, so without a gate a few-hundred-megabyte export can burst the
# container — and when the container dies, everything else running in it dies with it.
# 50MB holds a very long conversation history.
_MAX_UPLOAD = 50 * 1024 * 1024


def _engine():
    """The import engine: server.py constructs it and injects it onto tools/_runtime."""
    from tools import _runtime as rt
    return getattr(rt, "import_engine", None)


async def _take_file(request: Request):
    """Pull the file out of the multipart body; returns (text, filename).

    Raising PermissionError means "answer 403"; raising ValueError means "answer 400".
    """
    from .loci import _origin_reject
    why = _origin_reject(request)
    if why:
        raise PermissionError(why)
    try:
        form = await request.form()
    except Exception as e:                          # noqa: BLE001
        raise ValueError("读不出上传的表单：" + str(e))
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise ValueError("没收到文件（表单里要有一个叫 file 的字段）")
    raw = await f.read()
    if len(raw) > _MAX_UPLOAD:
        raise ValueError("文件 " + str(len(raw) // 1024 // 1024) + " MB，超过 "
                         + str(_MAX_UPLOAD // 1024 // 1024) + " MB 的上限")
    if not raw:
        raise ValueError("文件是空的")
    # errors="replace": one bad byte somewhere in an export must not fail the whole
    # import. That single character becomes U+FFFD and the other tens of thousands still
    # come through.
    return raw.decode("utf-8", "replace"), str(getattr(f, "filename", "") or "")


def register(mcp) -> None:

    @mcp.custom_route("/api/import/preflight", methods=["POST"])
    async def api_import_preflight(request: Request) -> Response:
        """Preview: identify the format, how many chunks it splits into, how many model
        calls that costs.

        **Writes nothing.** It goes through preview_import(), whose own comment says
        "Return a local-only preview without mutating state".
        """
        from core.import_memory import preview_import
        try:
            raw, name = await _take_file(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:
            return JSONResponse(preview_import(raw, name))
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] preflight 失败: " + str(e))
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/import/upload", methods=["POST"])
    async def api_import_upload(request: Request) -> Response:
        """Start the import. **This one really does write into the memory store.**

        Single slot: if reserve_start() cannot take it, an import is already running and
        this returns 409 — two imports pouring in at once would scramble both dedup and
        job state.
        Once the slot is taken it **does not wait for completion** (a single history can
        run for tens of minutes): it runs in the background and the front-end polls
        status.
        """
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来（服务刚启动？等几秒再试）"},
                                status_code=503)
        try:
            raw, name = await _take_file(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)

        job_id = eng.reserve_start()
        if job_id is None:
            return JSONResponse(
                {"error": "已经有一份导入在跑了", "job_id": eng.active_job_id},
                status_code=409)

        async def _run():
            try:
                await eng.start(raw, filename=name, reservation_id=job_id)
            except Exception as e:                  # noqa: BLE001
                # Nothing catches an exception thrown from a background task, so the log
                # is the only record of the scene.
                logger.error("[import] job " + job_id + " 炸了: "
                             + type(e).__name__ + ": " + str(e))
                try:
                    eng.release_start_reservation(job_id)
                except Exception:                   # noqa: BLE001
                    pass

        asyncio.create_task(_run())
        return JSONResponse({"ok": True, "job_id": job_id, "filename": name,
                             "note": "跑起来了。进度看 /api/import/status。"})

    @mcp.custom_route("/api/import/status", methods=["GET"])
    async def api_import_status(request: Request) -> Response:
        """Progress. The front-end polls every 1.5s and uses is_running to tell whether
        it has finished — so that field **must be supplied explicitly here**; the engine's
        own to_dict does not contain it."""
        eng = _engine()
        if eng is None:
            return JSONResponse({"is_running": False, "status": "engine_not_ready"})
        try:
            st = dict(eng.get_status() or {})
            st["is_running"] = bool(eng.is_running)
            return JSONResponse(st)
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] status 失败: " + str(e))
            return JSONResponse({"error": str(e), "is_running": False}, status_code=500)

    @mcp.custom_route("/api/import/pause", methods=["POST"])
    async def api_import_pause(request: Request) -> Response:
        """Pause: stop once the current chunk finishes. Not an immediate cut — cutting
        mid-chunk leaves half a memory behind."""
        from .loci import _origin_reject
        why = _origin_reject(request)
        if why:
            return JSONResponse({"error": why}, status_code=403)
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来"}, status_code=503)
        try:
            eng.pause()
            return JSONResponse({"ok": True, "note": "这一块跑完就停。"})
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] pause 失败: " + str(e))
            return JSONResponse({"error": str(e)}, status_code=500)
