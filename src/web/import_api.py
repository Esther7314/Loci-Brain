"""
========================================
web/import_api.py — the import routes
========================================

The doorway to core/import_memory.py: an uploaded export is stored as a source (step ①,
inside the request), then drafted by the side model (step ②, in the background unless the
request asks to wait). Nothing here writes a memory.

    POST /api/import/preflight   preview — read-only, writes nothing
    POST /api/import/upload      store the file as an import source, then draft it
    GET  /api/import/status      progress of the job (or ?batch=imp_…) — the panel polls it
    GET  /api/import/batches     every import batch and where it stands
    POST /api/import/pause       stop drafting after the current conversation
    POST /api/import/withdraw    withdraw one whole batch: memories resting on it are
                                 cleared like any withdrawn source's, its drafts and its
                                 text are deleted

/api/import/upload takes multipart/form-data (the panel sends a FormData):

    file       the export (optional only with resume=1 and batch)
    same_self  是不是同一个他: 1 / 是 (the default) = the AI in the file is the model
               reading it (its side is 我, EVENT/SELF); 0 / 否 = another AI (EVENT/WORLD)
    resume     1 = go on drafting an unfinished batch: the one named by `batch`, else the
               one this same file was stored as
    batch      imp_… (with resume)
    wait       1 = draft inside this request and answer with how it went

 -> 200 {ok, batch, source: {system: "import", instance}, status, resumed, filename,
         format, same_self, conversations, lines, drafted, drafts, pending, drafting:
         "background" | "done", failures: [{container, title, error}], errors, note}
    400 not a file it can read · 404 no such batch to resume · 409 an import is running,
    this file is already a batch (its id in `batch`), or the batch to resume is being
    withdrawn · 503 the engine is not up

/api/import/withdraw takes JSON {batch} -> 200 {ok, status: "withdrawn" | "incomplete",
batch, conversations, entries, derived_pending, drafts_deleted, text_deleted, changes};
404 / 409 (still drafting) as above.

Writes check the same origin as every panel write (loci._origin_reject). The panel gate
wraps every route here (web/_Gated).

Public surface: register(mcp)
========================================
"""

import asyncio
import hashlib
import json
import logging

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from bridge.request_limits import IMPORT_UPLOAD_BYTES
from utils import parse_bool

logger = logging.getLogger("loci_brain.web.import")

# Both upload routes read the whole file into memory, so there has to be a ceiling: a
# few-hundred-megabyte export would burst the container and everything running in it. The
# body as a whole is held to the same number while it streams
# (bridge/request_limits.UPLOAD_CEILINGS).
_MAX_UPLOAD = IMPORT_UPLOAD_BYTES
# The background drafting tasks: the event loop keeps only a weak reference to a task, so
# one nobody holds can be collected mid-run.
_TASKS: set = set()
_YES, _NO = {"是", "对"}, {"否", "不是", "不"}


def _flag(value, default: bool) -> bool:
    text = str(value or "").strip()
    if text in _YES:
        return True
    if text in _NO:
        return False
    return parse_bool(text, default=default)


def _engine():
    """The import engine: server.py constructs it and injects it onto core/runtime."""
    from core import runtime as rt
    return getattr(rt, "import_engine", None)


async def _read_form(request: Request, need_file: bool = True):
    """The multipart body -> (fields, text or None, filename). Raising PermissionError
    means 403, ValueError 400."""
    from .loci import _origin_reject
    why = _origin_reject(request)
    if why:
        raise PermissionError(why)
    try:
        form = await request.form(max_files=1, max_fields=16)
    except Exception as e:                          # noqa: BLE001
        raise ValueError("读不出上传的表单：" + str(e))
    fields = {k: str(v) for k, v in form.items() if not hasattr(v, "read")}
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        if need_file:
            raise ValueError("没收到文件（表单里要有一个叫 file 的字段）")
        return fields, None, ""
    raw = await f.read()
    if len(raw) > _MAX_UPLOAD:
        raise ValueError("文件 " + str(len(raw) // 1024 // 1024) + " MB，超过 "
                         + str(_MAX_UPLOAD // 1024 // 1024) + " MB 的上限")
    if not raw:
        raise ValueError("文件是空的")
    # errors="replace": one bad byte in an export must not fail the whole import.
    return fields, raw.decode("utf-8", "replace"), str(getattr(f, "filename", "") or "")


def _hosts():
    from core._originals import deployment_hosts
    return deployment_hosts()


def register(mcp) -> None:

    @mcp.custom_route("/api/import/preflight", methods=["POST"])
    async def api_import_preflight(request: Request) -> Response:
        """Preview: the format, how many conversations and lines, how many side-model
        calls drafting would cost. Writes nothing."""
        from core.import_memory import preview_import
        try:
            _fields, raw, name = await _read_form(request)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        try:
            return JSONResponse(preview_import(raw, name))
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] preflight failed: " + str(e))
            return JSONResponse({"error": str(e)}, status_code=500)

    @mcp.custom_route("/api/import/upload", methods=["POST"])
    async def api_import_upload(request: Request) -> Response:
        """Store the file as an import source (in this request), then draft it."""
        from core.import_memory import ImportDuplicate, ImportRefused
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来（服务刚启动？等几秒再试）"},
                                status_code=503)
        try:
            fields, raw, name = await _read_form(request, need_file=False)
        except PermissionError as e:
            return JSONResponse({"error": str(e)}, status_code=403)
        except ValueError as e:
            return JSONResponse({"error": str(e)}, status_code=400)
        resume = _flag(fields.get("resume"), False)
        wait = _flag(fields.get("wait"), False)
        same_self = _flag(fields.get("same_self"), True)
        batch = str(fields.get("batch") or "").strip()
        if raw is None and not (resume and batch):
            return JSONResponse({"error": "没收到文件（表单里要有一个叫 file 的字段；"
                                          "接着起草可以只带 resume=1 和 batch）"},
                                status_code=400)

        job_id = eng.reserve_start()
        if job_id is None:
            return JSONResponse(
                {"error": "已经有一份导入在跑了", "job_id": eng.active_job_id,
                 "batch": eng.active_batch or None}, status_code=409)
        background = False
        try:
            meta = None
            if resume:
                sha = (hashlib.sha256(raw.encode("utf-8", errors="surrogatepass")).hexdigest()
                       if raw is not None else "")
                meta = eng.resumable(batch, sha)
                if meta is None and batch:
                    return JSONResponse({"error": f"没有这一批导入：{batch[:40]}"},
                                        status_code=404)
                from core.import_memory import draft_refusal
                refusal = draft_refusal(meta) if meta is not None else ""
                if refusal:
                    return JSONResponse({"error": refusal, "batch": meta.get("batch")},
                                        status_code=409)
            resumed = meta is not None
            if meta is None:
                try:
                    meta = await eng.take(raw, name, same_self=same_self)
                except ImportDuplicate as e:
                    return JSONResponse({"error": str(e), "batch": e.batch}, status_code=409)
                except ImportRefused as e:
                    return JSONResponse({"error": str(e)}, status_code=400)
            batch = str(meta["batch"])
            if wait:
                out = await eng.draft(batch, job_id=job_id)
                drafting = "done"
            else:
                async def _run():
                    try:
                        await eng.draft(batch, job_id=job_id)
                    except Exception as e:          # noqa: BLE001
                        # Nothing awaits a background task: the log and the batch's
                        # status are the record of it.
                        logger.error("[import] drafting " + batch + " failed: "
                                     + type(e).__name__ + ": " + str(e))
                    finally:
                        eng.release_start_reservation(job_id)
                eng.working_on(job_id, batch)
                task = asyncio.create_task(_run())
                _TASKS.add(task)
                task.add_done_callback(_TASKS.discard)
                background = True
                out = eng.get_status(batch)
                out["status"] = "drafting"
                drafting = "background"
        except Exception as e:                      # noqa: BLE001
            logger.error("[import] upload failed: " + type(e).__name__ + ": " + str(e))
            return JSONResponse({"error": f"导入失败：{type(e).__name__}: {e}"},
                                status_code=500)
        finally:
            if not background:
                eng.release_start_reservation(job_id)
        from core.import_memory import source_string
        first = (meta.get("conversations") or [{}])[0]
        note = ("原话存好了，当天能翻能搜：recall(query=\""
                + source_string(batch, first.get("container", ""), first.get("first", ""),
                                first.get("last", ""))
                + "\", view=\"original\")；搜词就 recall(query=\"词\", view=\"original\")。")
        if drafting == "background":
            note += "候选在后台起草，进度看 /api/import/status。"
        elif out.get("failures") or out.get("errors"):
            note += "起草有失败的（见 failures / errors），带 resume=1 重传接着起草。"
        return JSONResponse({"ok": True, **out, "resumed": resumed, "drafting": drafting,
                             "note": note})

    @mcp.custom_route("/api/import/status", methods=["GET"])
    async def api_import_status(request: Request) -> Response:
        """Progress. The panel polls it and reads `is_running` to tell whether it has
        finished; `status` says how drafting came out (a failure is never 「完成」)."""
        eng = _engine()
        if eng is None:
            return JSONResponse({"is_running": False, "status": "engine_not_ready"})
        try:
            st = dict(eng.get_status(str(request.query_params.get("batch") or "").strip()))
            st["is_running"] = bool(eng.is_running)
            return JSONResponse(st)
        except Exception as e:                      # noqa: BLE001
            logger.warning("[import] status failed: " + str(e))
            return JSONResponse({"error": str(e), "is_running": False}, status_code=500)

    @mcp.custom_route("/api/import/batches", methods=["GET"])
    async def api_import_batches(request: Request) -> Response:
        """Every import batch, newest first, as the status describes one."""
        eng = _engine()
        if eng is None:
            return JSONResponse({"batches": [], "status": "engine_not_ready"})
        return JSONResponse({"batches": eng.batches()})

    @mcp.custom_route("/api/import/pause", methods=["POST"])
    async def api_import_pause(request: Request) -> Response:
        """Pause: drafting stops once the current conversation is drafted."""
        from .loci import _origin_reject
        why = _origin_reject(request)
        if why:
            return JSONResponse({"error": why}, status_code=403)
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来"}, status_code=503)
        eng.pause()
        return JSONResponse({"ok": True, "note": "这一段对话起草完就停。"})

    @mcp.custom_route("/api/import/withdraw", methods=["POST"])
    async def api_import_withdraw(request: Request) -> Response:
        """Withdraw one whole import batch (core/import_memory.ImportEngine.withdraw)."""
        from .loci import _origin_reject
        why = _origin_reject(request)
        if why:
            return JSONResponse({"error": why}, status_code=403)
        eng = _engine()
        if eng is None:
            return JSONResponse({"error": "导入引擎还没起来"}, status_code=503)
        try:
            body = json.loads((await request.body()) or b"{}")
        except ValueError:
            return JSONResponse({"error": "要 JSON：{\"batch\": \"imp_…\"}"}, status_code=400)
        batch = str((body or {}).get("batch") or "").strip() if isinstance(body, dict) else ""
        if not batch:
            return JSONResponse({"error": "要带 batch（imp_…）"}, status_code=400)
        try:
            status, out = await eng.withdraw(batch, hosts=_hosts())
        except Exception as e:                      # noqa: BLE001
            logger.error("[import] withdrawing " + batch + " failed: "
                         + type(e).__name__ + ": " + str(e))
            return JSONResponse({"error": f"撤回失败：{type(e).__name__}: {e}。"
                                          "再撤回一次会接着清。"}, status_code=500)
        return JSONResponse(out, status_code=status)
