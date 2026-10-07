"""
========================================
web/loci_verdicts.py — the write routes the user drives by hand on the panel
========================================

    POST /api/loci/want/resolve       -> close something that was wanted (trace status)
    POST /api/loci/want/asked         -> record that it was asked about (trace)
    POST /api/loci/event/correct      -> regrow: writes a NEW VERSION of a memory (the
                                         content kind of entry/fix, web/loci_detail.py)
    POST /api/loci/subjects/action    -> edits the alias table in the data volume (the
                                         names/action handler, web/loci_names.py)

Every one goes through `_write_body` (the same-origin check).
========================================
"""

from starlette.requests import Request
from starlette.responses import Response

from . import _shared as sh
from ._guards import _write_body

logger = sh.logger


# ---------------------------------------------------------
# Three write endpoints the user drives by hand:
# 1. The close button — the user is the one who knows whether something is finished, and
#    should not have to wait to be asked.
# 2. The "asked about this" ping — the stamp is only applied at the moment the question
#    is actually shown to them.
# 3. Event correction — only an event can be corrected this way; mind has no such route
#    by design. The original text is left untouched, and a correction is stored as a new
#    entry pointing back through `from`. Nothing is ever really deleted.
# All three go through `_write_body` (the same-origin check) plus a direct in-process
# call to `trace_core` / `bucket_mgr`, following the similar/action precedent rather than
# going out through the MCP layer.
# ---------------------------------------------------------
async def api_loci_want_resolve(request: Request) -> Response:
    """The close button: one click sets status to resolved or abandoned and records that
    the user closed it.

    Open only to something wanted that is still open (telic, not closed) — closing is not
    an action that applies to anything else. A repeat click on something already closed is caught
    by trace's "no fields need changing" path, so it neither errors nor overwrites
    closed_by a second time.
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    bucket_id = str(body.get("id") or "").strip()
    new_status = str(body.get("status") or "").strip().lower()
    if not bucket_id:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    if new_status not in ("resolved", "abandoned"):
        return JSONResponse(
            {"error": f'status 只能是 "resolved"（放下了）或 "abandoned"（不做了），收到：{new_status}'},
            status_code=400)

    target = await sh.bucket_mgr.get(bucket_id)
    if not target:
        return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)
    tmeta = target.get("metadata", {}) or {}
    from utils import is_closed, is_telic
    if not is_telic(tmeta) or is_closed(tmeta):
        return JSONResponse(
            {"error": "这条不是还开着的想要（telic 且没关），没有结案这个动作"},
            status_code=409)

    try:
        from tools.trace.core import trace_core
        # `closed_by` records that a PERSON closed this, rather than that I noticed it
        # myself — see the note in tools/trace/core.py. Nothing compares this value; it
        # is free text that exists to be read. It names no specific person, or every
        # install would write that name into its own data.
        msg = str(await trace_core(bucket_id=bucket_id, status=new_status, closed_by="user"))
        return JSONResponse({"ok": True, "id": bucket_id, "status": new_status, "msg": msg})
    except Exception as e:
        logger.warning(f"[loci] 结案失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_want_asked(request: Request) -> Response:
    """The "last asked" stamp, applied once at the moment the panel **actually renders**
    the question line where the user can see it.

    This does not decide whether the entry is the longest-standing one — that is already
    computed as `heavy_question_id` in `/api/loci/profile`. This endpoint only applies
    the stamp, and trusts whoever calls it.
    """
    from starlette.responses import JSONResponse
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)

    bucket_id = str(body.get("id") or "").strip()
    if not bucket_id:
        return JSONResponse({"error": "缺 id"}, status_code=400)
    target = await sh.bucket_mgr.get(bucket_id)
    if not target:
        return JSONResponse({"error": f"查无此桶：{bucket_id}"}, status_code=404)

    try:
        from tools.trace.core import trace_core
        await trace_core(bucket_id=bucket_id, mark_asked=True)
        fresh = await sh.bucket_mgr.get(bucket_id)
        last_asked = str((fresh or {}).get("metadata", {}).get("last_asked") or "")
        return JSONResponse({"ok": True, "id": bucket_id, "last_asked": last_asked})
    except Exception as e:
        logger.warning(f"[loci] 记「问过了」失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)


async def api_loci_event_correct(request: Request) -> Response:
    """The user corrects an event: the content kind of POST /api/loci/entry/fix, under the
    path the panel has always posted to. The rules and the write are
    web/loci_detail.correct_event's: the original is left untouched, the correction is a
    new event pointing back at it with the 人改的 tag, and a MIND entry is refused."""
    from starlette.responses import JSONResponse
    from .loci_detail import correct_event
    try:
        body = await _write_body(request)
    except PermissionError as e:
        return JSONResponse({"error": str(e)}, status_code=403)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    try:
        status, out = await correct_event(str(body.get("id") or "").strip(),
                                          str(body.get("text") or ""))
    except Exception as e:
        logger.warning(f"[loci] event 改错失败: {e}")
        return JSONResponse({"error": str(e)}, status_code=500)
    return JSONResponse(out, status_code=status)


async def api_loci_subjects_action(request: Request) -> Response:
    """The subjects screen's actions: the same handler as POST /api/loci/names/action
    (web/loci_names.py; the words and the dispatch are core/census.name_action)."""
    from .loci_names import api_loci_names_action
    return await api_loci_names_action(request)
